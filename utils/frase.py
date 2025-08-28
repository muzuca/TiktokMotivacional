# utils/frase.py
import os
import re
import unicodedata
import random
import json
import hashlib
import time
from typing import List, Set, Tuple, Optional, Dict
from pathlib import Path

from dotenv import load_dotenv
import google.generativeai as genai
import logging

try:
    import yaml
    _HAVE_YAML = True
except ImportError:
    _HAVE_YAML = False

# -----------------------------------------------------------------------------#
# Config & logging
# -----------------------------------------------------------------------------#
load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

model = genai.GenerativeModel("models/gemini-1.5-pro-latest")

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

GEMINI_TIMEOUT_SEC = float(os.getenv("GEMINI_TIMEOUT_SEC", "45"))

# -----------------------------------------------------------------------------#
# Cache & Resources
# -----------------------------------------------------------------------------#
CACHE_DIR = "cache"
RESOURCES_DIR = "resources"
os.makedirs(CACHE_DIR, exist_ok=True)
PHRASES_CACHE_FILE = os.path.join(CACHE_DIR, "used_phrases.json")
LONG_PHRASES_CACHE_FILE = os.path.join(CACHE_DIR, "used_long_phrases.json")
FALLBACK_PHRASES_FILE = os.path.join(RESOURCES_DIR, "fallback_phrases.json")
_FALLBACK_PHRASES_CACHE: Optional[Dict] = None


def load_used_phrases(cache_file: str) -> List[str]:
    """Carrega uma lista de frases de um arquivo JSON."""
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and all(isinstance(item, str) for item in data):
                return data
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def save_used_phrases(used_phrases: List[str], cache_file: str) -> None:
    """Salva uma lista de frases em um arquivo JSON, removendo duplicatas."""
    try:
        unique_phrases = sorted(list(set(used_phrases)))
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(unique_phrases, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Não foi possível salvar cache de frases em %s: %s", os.path.basename(cache_file), e)

# -----------------------------------------------------------------------------#
# Helpers
# -----------------------------------------------------------------------------#
_PT_SW = {"a","o","os","as","um","uma","de","da","do","das","dos","em","no","na","nos","nas","e","ou","pra","para","por","que","se","com","sem","ao","à","às","aos","é","ser","ter"}
_EN_SW = {"a","an","the","and","or","of","in","on","to","for","with","that","is","it","you","your"}
LANG_NAME_MAP = {"pt": "português do Brasil", "en": "inglês", "ar": "árabe"}

def _load_prompt_template(template_name: str) -> str:
    """Carrega um template de prompt de um arquivo YAML."""
    if not _HAVE_YAML:
        raise ImportError("A biblioteca PyYAML é necessária. Instale com: pip install PyYAML")
    
    config_path = Path(__file__).parent / "prompts" / f"{template_name}.yaml"
    
    if not config_path.exists():
        raise FileNotFoundError(f"Arquivo de template de prompt '{config_path}' não encontrado.")
    
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
        template = data.get("template")
        if not template or not isinstance(template, str):
            raise ValueError(f"O arquivo YAML '{config_path}' não contém a chave 'template' ou ela está vazia.")
        return template.strip()

def _get_fallback_phrase(theme: str, lang: str) -> str:
    """Carrega o JSON de fallbacks e escolhe uma frase aleatória."""
    global _FALLBACK_PHRASES_CACHE
    if _FALLBACK_PHRASES_CACHE is None:
        try:
            with open(FALLBACK_PHRASES_FILE, "r", encoding="utf-8") as f:
                _FALLBACK_PHRASES_CACHE = json.load(f)
        except Exception as e:
            logger.error(f"Erro fatal ao carregar arquivo de fallback '{FALLBACK_PHRASES_FILE}': {e}")
            _FALLBACK_PHRASES_CACHE = {}
    
    phrases = _FALLBACK_PHRASES_CACHE.get(theme, {}).get(lang, [])
    if phrases:
        return random.choice(phrases)
    
    logger.error(f"Nenhuma frase de fallback encontrada para tema='{theme}' e idioma='{lang}'.")
    return "Acredite no poder dos seus sonhos."

def _lang_tag(idioma: Optional[str]) -> str:
    s = (idioma or "en").strip().lower()
    if s.startswith("pt"): return "pt"
    if s.startswith("ar"): return "ar"
    return "en"

def _clean_line(s: str) -> str:
    if not s: return ""
    line = s.strip()
    line = re.sub(r'^\s*(?:\d+[\).\s-]+|[-*•]\s+)', '', line).strip(' "\'')
    return re.sub(r'\s+', ' ', line)

def _strip_emph(s: str) -> str:
    return re.sub(r'\*\*([^*]+)\*\*', r'\1', s)

def _count_words_no_markup(s: str) -> int:
    return len(re.findall(r"\b[\w’'-]+\b", _strip_emph(s), flags=re.UNICODE))

def _ask_json_list(prompt: str, temperature: float = 1.0, tries: int = 3) -> List[str]:
    print("\n" + "="*20 + " PROMPT ENVIADO AO GEMINI " + "="*20)
    print(prompt)
    print("="*66 + "\n")
    
    for attempt in range(tries):
        try:
            resp = model.generate_content(
                prompt,
                generation_config={"response_mime_type": "application/json", "temperature": temperature, "top_p": 0.95},
                request_options={"timeout": GEMINI_TIMEOUT_SEC},
            )
            raw = (getattr(resp, "text", "") or "").strip()

            print("\n" + "-"*20 + " RESPOSTA RECEBIDA DO GEMINI " + "-"*20)
            print(raw)
            print("-"*68 + "\n")

            data = json.loads(raw)
            if isinstance(data, list):
                out = [c for it in data if isinstance(it, str) and (c := _clean_line(it))]
                if out: return out
        except Exception as e:
            logger.warning("Falha ao pedir/processar lista JSON ao Gemini (tentativa %d/%d): %s", attempt + 1, tries, e)
        time.sleep(0.8 * (attempt + 1))
    return []

def _ensure_single_emphasis(text: str, lang: str, prefer_last_n: int = 2) -> str:
    base = _clean_line(text)
    if len(list(re.finditer(r'\*\*([^*]+)\*\*', base))) == 1: return base
    base = _strip_emph(base)
    tokens = re.findall(r"\w+|\W+", base, flags=re.UNICODE)
    sw = _PT_SW if lang == "pt" else _EN_SW
    picks = [i for i, token in enumerate(tokens) if re.match(r"\w+", token) and len(token) >= 3 and token.lower() not in sw]
    if not picks: return base
    target_indices = picks[-prefer_last_n:]
    i0, i1 = target_indices[0], target_indices[-1]
    tokens[i0] = "**" + tokens[i0]
    tokens[i1] = tokens[i1] + "**"
    return "".join(tokens)

def gerar_slug(frase: str) -> str:
    frase_limpa = re.sub(r'[\*\.:\?!,"\'`´’]', '', frase.lower())
    return "_".join(frase_limpa.split()[:6])[:40]

def quebrar_em_duas_linhas(frase: str) -> str:
    palavras = frase.split()
    if len(palavras) < 4: return frase
    ponto_de_quebra = (len(palavras) + 1) // 2
    return f'{" ".join(palavras[:ponto_de_quebra])}\n{" ".join(palavras[ponto_de_quebra:])}'

# ### INÍCIO DA ADIÇÃO: Funções movidas para cá para serem centralizadas ###
_PUNCH_WORDS = {"você","voce","vida","fé","fe","deus","foco","força","forca","coragem", "propósito","proposito","sucesso","sonho","agora","hoje","mais","nunca","sempre"}

def _parse_highlights_from_markdown(s: str) -> Tuple[str, List[str]]:
    segs = re.findall(r"\*\*(.+?)\*\*", s, flags=re.S)
    clean = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    words: List[str] = [tok.strip().lower() for seg in segs for tok in re.split(r"[^\wÀ-ÖØ-öø-ÿ]+", seg) if tok.strip()]
    return clean, words

def _pick_highlights(line: str) -> List[str]:
    words = [re.sub(r"[^\wÀ-ÖØ-öø-ÿ]", "", w).lower() for w in line.split()]
    for w in words:
        if w in _PUNCH_WORDS:
            return [w]
    for w in reversed(words):
        if len(w) >= 3:
            return [w]
    return words[-1:] if words else []

def _split_for_emphasis(frase: str) -> Tuple[str, str, List[str]]:
    # Dependências de ambiente que esta função usa (copiadas de imagem.py)
    IMAGE_EMPHASIS_ONLY_MARKUP = os.getenv("IMAGE_EMPHASIS_ONLY_MARKUP", "True").lower() in ("true", "1", "yes")
    IMAGE_FORCE_EMPHASIS = os.getenv("IMAGE_FORCE_EMPHASIS", "False").lower() in ("true", "1", "yes")
    IMAGE_EMPHASIS_LAST_WORDS = int(os.getenv("IMAGE_EMPHASIS_LAST_WORDS", "1"))

    clean, explicit_words = _parse_highlights_from_markdown(frase.strip())
    s = clean.strip()
    parts = [p.strip() for p in re.split(r"(?:\.\.\.|[.!?:;—–-])", s) if p and p.strip()]
    if len(parts) >= 2:
        intro, punch = parts[0], " ".join(parts[1:]).strip()
    else:
        ws = s.split()
        if len(ws) >= 6:
            cut = max(3, min(len(ws)-2, len(ws)//2))
            intro, punch = " ".join(ws[:cut]), " ".join(ws[cut:])
        else:
            intro, punch = "", s
    if explicit_words:
        hl = explicit_words
    else:
        if IMAGE_EMPHASIS_ONLY_MARKUP:
            hl = []
        elif IMAGE_FORCE_EMPHASIS:
            toks = [re.sub(r"[^\wÀ-ÖØ-öø-ÿ]", "", w).lower() for w in punch.split() if w.strip()]
            hl = toks[-IMAGE_EMPHASIS_LAST_WORDS:] if toks else _pick_highlights(punch)
        else:
            hl = _pick_highlights(punch)
    return intro, punch, hl
# ### FIM DA ADIÇÃO ###


# -----------------------------------------------------------------------------#
# Funções Principais (Hashtags, Prompts, Frases)
# -----------------------------------------------------------------------------#
def _get_history_for_prompt(cache_file: str, limit: int = 30) -> str:
    used_phrases = load_used_phrases(cache_file)
    if not used_phrases: return ""
    formatted_list = "\n".join([f'- "{phrase}"' for phrase in used_phrases[-limit:]])
    return f"---\nHISTÓRICO DE FRASES RECENTES (EVITE CRIAR ALGO SIMILAR A ISTO):\n{formatted_list}\n---"

def _dedupe_hashtags(tags: List[str]) -> List[str]:
    seen, out = set(), []
    for t in tags:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out

def _sanitize_hashtag(tag: str, lang: str) -> str:
    if not tag: return ""
    t = tag.strip()
    if t.startswith("#"): t = t[1:]
    t = re.sub(r"\s+", "", t)
    if lang in ("pt", "en"):
        t = unicodedata.normalize("NFD", t).encode("ascii", "ignore").decode("ascii")
        t = re.sub(r"[^A-Za-z0-9_]", "", t).lower()
    else:
        t = re.sub(r"[^\w\u0600-\u06FF_]+", "", t, flags=re.UNICODE)
    return f"#{t[:30]}" if t else ""

def gerar_hashtags_virais(conteudo: str, idioma: str = "auto", n: int = 3, plataforma: str = "tiktok") -> List[str]:
    lang = _lang_tag(idioma)
    if not os.getenv("GEMINI_API_KEY"):
        logger.warning("GEMINI_API_KEY ausente — usando fallback de hashtags.")
        return {"pt": ["#motivacao", "#inspiracao", "#mindset"], "ar": ["#تحفيز", "#الهام", "#عقلية"]}.get(lang, ["#motivation", "#inspiration", "#mindset"])[:n]
    try:
        prompt = _load_prompt_template("hashtags").format(n=n, target_lang_name=LANG_NAME_MAP.get(lang, "inglês"), conteudo=conteudo.strip()[:1000])
    except FileNotFoundError as e:
        logger.warning(f"Falha ao gerar hashtags (seguirei sem): {e}")
        return []
    raw_list = _ask_json_list(prompt, temperature=0.9, tries=3)
    tags = _dedupe_hashtags([_sanitize_hashtag(t, lang) for t in raw_list if t])
    if len(tags) < n:
        fallbacks = {"pt": ["#motivacao", "#inspiracao", "#mindset", "#disciplina", "#foco"], "ar": ["#تحفيز", "#الهام", "#انجاز", "#انضباط", "#تركيز"], "en": ["#motivation", "#inspiration", "#mindset", "#discipline", "#focus"]}
        tags.extend(f for f in fallbacks.get(lang, []) if f.lower() not in {t.lower() for t in tags} and len(tags) < n)
    return tags[:n]

def gerar_prompts_de_imagem_variados(tema: str, quantidade: int, idioma: str = "en") -> List[str]:
    if not os.getenv("GEMINI_API_KEY"):
        logger.error("❌ GEMINI_API_KEY não configurada.")
        return [f"{tema}, variação {i+1}" for i in range(quantidade)]
    target_lang_name = LANG_NAME_MAP.get(_lang_tag(idioma), "inglês")
    template = _load_prompt_template("image_prompts")
    prompt_text = template.format(tema=tema, quantidade=quantidade, target_lang_name=target_lang_name)
    descricoes = _ask_json_list(prompt_text, temperature=1.3, tries=3)
    if descricoes and len(descricoes) >= quantidade:
        logger.info(f"✅ {len(descricoes)} prompts de imagem criativos gerados para o tema '{tema}'.")
        return descricoes[:quantidade]
    logger.warning("Não foi possível gerar prompts criativos. Usando fallback.")
    return [f"{tema}, cinematic, high detail, variation {i+1}" for i in range(quantidade)]

def gerar_frase_motivacional(idioma: str = "en") -> str:
    lang = _lang_tag(idioma)
    prompt_text = _load_prompt_template("short_motivational").format(target_lang_name=LANG_NAME_MAP.get(lang, "inglês"), historico_frases=_get_history_for_prompt(PHRASES_CACHE_FILE))
    used_phrases = load_used_phrases(PHRASES_CACHE_FILE)
    used_phrases_set = {p.strip() for p in used_phrases}
    for _ in range(3):
        phrases = _ask_json_list(prompt_text, temperature=1.2, tries=2)
        valid = [p for p in phrases if 6 <= _count_words_no_markup(p) <= 12 and p.strip() not in used_phrases_set]
        if valid:
            chosen = _ensure_single_emphasis(random.choice(valid), lang)
            used_phrases.append(chosen)
            save_used_phrases(used_phrases, PHRASES_CACHE_FILE)
            return chosen
    logger.warning("Nenhuma frase curta inédita foi gerada, usando fallback.")
    return _get_fallback_phrase("motivacional_curta", lang)

def gerar_frase_motivacional_longa(idioma: str = "en") -> str:
    lang = _lang_tag(idioma)
    prompt_text = _load_prompt_template("long_motivational").format(target_lang_name=LANG_NAME_MAP.get(lang, "inglês"), historico_frases=_get_history_for_prompt(LONG_PHRASES_CACHE_FILE))
    used_long_phrases = load_used_phrases(LONG_PHRASES_CACHE_FILE)
    used_long_phrases_set = {p.strip() for p in used_long_phrases}
    for attempt in range(1, 4):
        logger.info(f"Tentando gerar frase longa inédita (tentativa {attempt}/3)...")
        phrases = _ask_json_list(prompt_text, temperature=1.2, tries=2)
        valid = [p for p in phrases if 25 <= _count_words_no_markup(p) <= 40 and p.strip() not in used_long_phrases_set]
        if valid:
            chosen = random.choice(valid)
            used_long_phrases.append(chosen)
            save_used_phrases(used_long_phrases, LONG_PHRASES_CACHE_FILE)
            logger.info("✅ Frase longa inédita selecionada para narração.")
            return chosen
        logger.warning(f"Tentativa {attempt} não encontrou frases novas. Re-gerando prompt com histórico atualizado.")
        prompt_text = _load_prompt_template("long_motivational").format(target_lang_name=LANG_NAME_MAP.get(lang, "inglês"), historico_frases=_get_history_for_prompt(LONG_PHRASES_CACHE_FILE))
        if attempt < 3: time.sleep(1)
    logger.warning("Nenhuma frase longa inédita foi gerada após 3 tentativas, usando fallback.")
    return _get_fallback_phrase("motivacional_longa", lang)

def gerar_prompt_tarot(idioma: str = "en") -> str:
    return gerar_prompts_de_imagem_variados("mesa de tarô mística", 1, idioma)[0]

def gerar_frase_tarot_curta(idioma: str = "en") -> str:
    lang = _lang_tag(idioma)
    prompt_text = _load_prompt_template("short_tarot").format(target_lang_name=LANG_NAME_MAP.get(lang, "inglês"), historico_frases=_get_history_for_prompt(PHRASES_CACHE_FILE))
    used_phrases = load_used_phrases(PHRASES_CACHE_FILE)
    used_phrases_set = {p.strip() for p in used_phrases}
    for _ in range(3):
        phrases = _ask_json_list(prompt_text, temperature=1.2)
        valid = [p for p in phrases if p.strip() not in used_phrases_set]
        if valid:
            chosen = _ensure_single_emphasis(random.choice(valid), lang)
            used_phrases.append(chosen)
            save_used_phrases(used_phrases, PHRASES_CACHE_FILE)
            return chosen
    logger.warning("Nenhuma frase curta de tarot foi gerada, usando fallback.")
    return _get_fallback_phrase("tarot_curta", lang)

def gerar_frase_tarot_longa(idioma: str = "en") -> str:
    lang = _lang_tag(idioma)
    prompt_text = _load_prompt_template("long_tarot").format(target_lang_name=LANG_NAME_MAP.get(lang, "inglês"), historico_frases=_get_history_for_prompt(LONG_PHRASES_CACHE_FILE))
    used_long_phrases = load_used_phrases(LONG_PHRASES_CACHE_FILE)
    used_long_phrases_set = {p.strip() for p in used_long_phrases}
    for attempt in range(1, 4):
        logger.info(f"Tentando gerar frase longa de tarot inédita (tentativa {attempt}/3)...")
        phrases = _ask_json_list(prompt_text, temperature=1.2, tries=2)
        valid = [p for p in phrases if 25 <= _count_words_no_markup(p) <= 40 and p.strip() not in used_long_phrases_set]
        if valid:
            chosen = random.choice(valid)
            used_long_phrases.append(chosen)
            save_used_phrases(used_long_phrases, LONG_PHRASES_CACHE_FILE)
            logger.info("✅ Frase longa de tarot inédita selecionada para narração.")
            return chosen
        if attempt < 3: time.sleep(1)
    logger.warning("Nenhuma frase longa de tarot inédita foi gerada, usando fallback.")
    return _get_fallback_phrase("tarot_longa", lang)