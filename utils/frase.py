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
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

GEMINI_TIMEOUT_SEC = float(os.getenv("GEMINI_TIMEOUT_SEC", "45"))
LOG_GEMINI_PROMPTS = os.getenv("LOG_GEMINI_PROMPTS", "True").lower() in ("true", "1", "yes")

# -----------------------------------------------------------------------------#
# Cache & Resources
# -----------------------------------------------------------------------------#
CACHE_DIR = "cache"
RESOURCES_DIR = "resources"
os.makedirs(CACHE_DIR, exist_ok=True)
PHRASES_CACHE_FILE = os.path.join(CACHE_DIR, "used_phrases.json")
LONG_PHRASES_CACHE_FILE = os.path.join(CACHE_DIR, "used_long_phrases.json")
PEXELS_PROMPTS_CACHE_FILE = os.path.join(CACHE_DIR, "used_pexels_prompts.json")
FALLBACK_PHRASES_FILE = os.path.join(RESOURCES_DIR, "fallback_phrases.json")
_FALLBACK_PHRASES_CACHE: Optional[Dict] = None


def load_used_phrases(cache_file: str) -> List[str]:
    """Carrega uma lista de frases de um arquivo JSON."""
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
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
# Helpers de Texto
# -----------------------------------------------------------------------------#
_PT_SW = {"a","o","os","as","um","uma","de","da","do","das","dos","em","no","na","nos","nas","e","ou","pra","para","por","que","se","com","sem","ao","à","às","aos","é","ser","ter"}
_EN_SW = {"a","an","the","and","or","of","in","on","to","for","with","that","is","it","you","your"}
LANG_NAME_MAP = {"pt": "português do Brasil", "en": "inglês", "ar": "árabe egípcio"}
IMAGE_EMPHASIS_ONLY_MARKUP = os.getenv("IMAGE_EMPHASIS_ONLY_MARKUP", "True").lower() in ("true", "1", "yes")
IMAGE_FORCE_EMPHASIS = os.getenv("IMAGE_FORCE_EMPHASIS", "False").lower() in ("true", "1", "yes")
IMAGE_EMPHASIS_LAST_WORDS = int(os.getenv("IMAGE_EMPHASIS_LAST_WORDS", "1"))
_PUNCH_WORDS = {"você","voce","vida","fé","fe","deus","foco","força","forca","coragem", "propósito","proposito","sucesso","sonho","agora","hoje","mais","nunca","sempre"}

def _idioma_norm(idioma: Optional[str]) -> str:
    s = (idioma or "pt").lower()
    if s.startswith("ar"): return "ar"
    if s.startswith("pt"): return "pt"
    return "en"

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
    elif IMAGE_FORCE_EMPHASIS:
        toks = [re.sub(r"[^\wÀ-ÖØ-öø-ÿ]", "", w).lower() for w in punch.split() if w.strip()]
        hl = toks[-IMAGE_EMPHASIS_LAST_WORDS:] if toks else _pick_highlights(punch)
    elif not IMAGE_EMPHASIS_ONLY_MARKUP:
        hl = _pick_highlights(punch)
    else:
        hl = []
        
    return intro, punch, hl

def quebrar_em_duas_linhas(frase: str) -> str:
    palavras = frase.split()
    if len(palavras) < 4: return frase
    ponto_de_quebra = (len(palavras) + 1) // 2
    return f'{" ".join(palavras[:ponto_de_quebra])}\n{" ".join(palavras[ponto_de_quebra:])}'

# -----------------------------------------------------------------------------#
# Helpers do Módulo
# -----------------------------------------------------------------------------#

def _load_prompt_template(template_name: str) -> str:
    if not _HAVE_YAML:
        raise ImportError("PyYAML é necessário. Instale com: pip install PyYAML")
    
    config_path = Path(__file__).parent / "prompts" / f"{template_name}.yaml"
    if not config_path.exists():
        config_path = Path("prompts") / f"{template_name}.yaml"

    if not config_path.exists():
        raise FileNotFoundError(f"Arquivo de template '{config_path.name}' não encontrado.")
    
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["template"].strip()

def _get_fallback_phrase(theme: str, lang: str) -> str:
    global _FALLBACK_PHRASES_CACHE
    if _FALLBACK_PHRASES_CACHE is None:
        try:
            with open(FALLBACK_PHRASES_FILE, "r", encoding="utf-8") as f:
                _FALLBACK_PHRASES_CACHE = json.load(f)
        except Exception as e:
            logger.error(f"Erro ao carregar fallback '{FALLBACK_PHRASES_FILE}': {e}")
            _FALLBACK_PHRASES_CACHE = {}
    
    phrases = _FALLBACK_PHRASES_CACHE.get(theme, {}).get(lang, [])
    return random.choice(phrases) if phrases else "Acredite nos seus sonhos."

def _clean_line(s: str) -> str:
    if not s: return ""
    line = s.strip()
    line = re.sub(r'^\s*(?:\d+[\).\s-]+|[-*•]\s+)', '', line).strip(' "\'')
    return re.sub(r'\s+', ' ', line)

def _strip_emph(s: str) -> str:
    return re.sub(r'\*\*([^*]+)\*\*', r'\1', s)

def _count_words_no_markup(s: str) -> int:
    return len(re.findall(r"\b[\w’'-]+\b", _strip_emph(s), flags=re.UNICODE))

def _ask_gemini(prompt: str, response_type: type = list, temperature: float = 1.0, tries: int = 3):
    log_func = logger.info if LOG_GEMINI_PROMPTS else logger.debug
    
    log_func("\n=============== PROMPT ENVIADO AO GEMINI ===============\n%s\n========================================================", prompt)
    
    for attempt in range(tries):
        try:
            resp = model.generate_content(
                prompt,
                generation_config={"response_mime_type": "application/json", "temperature": temperature, "top_p": 0.95},
                request_options={"timeout": GEMINI_TIMEOUT_SEC},
            )
            raw = (getattr(resp, "text", "") or "").strip()
            log_func("\n--------------- RESPOSTA RECEBIDA DO GEMINI ----------------\n%s\n----------------------------------------------------------", raw)

            # --- MUDANÇA AQUI: Lógica de parse mais robusta ---
            data = None
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                # Tenta corrigir JSON malformado que às vezes vem dentro de uma lista de strings
                if raw.startswith('["') and raw.endswith('"]'):
                    try:
                        potential_json_str = json.loads(raw)[0]
                        data = json.loads(potential_json_str)
                    except (IndexError, json.JSONDecodeError):
                        raise e # Re-levanta o erro original se a correção falhar
                else:
                    raise e # Re-levanta o erro original

            if isinstance(data, response_type):
                if response_type == list:
                    return [_clean_line(it) for it in data if isinstance(it, str)]
                return data # Retorna o dicionário
            # Se o tipo não bate, mas podemos extrair o que queremos, fazemos isso
            elif response_type == list and isinstance(data, dict):
                 logger.debug("Resposta foi DICT, mas esperava LIST. Extraindo valores.")
                 return [_clean_line(v) for v in data.values() if isinstance(v, str)]

        except Exception as e:
            logger.warning("Falha ao pedir/processar JSON (tentativa %d/%d): %s", attempt + 1, tries, e)
        time.sleep(0.8 * (attempt + 1))
    return response_type()

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

def _gerar_contexto_dinamico(content_mode: str, lang: str) -> str:
    logger.info("Gerando contexto dinâmico para a narração...")
    try:
        content_type = "Tarot ou Oráculo" if content_mode == "tarot" else "Motivacional"
        prompt_text = _load_prompt_template("context_generator").format(
            content_type=content_type,
            target_lang_name=LANG_NAME_MAP.get(lang, "inglês")
        )
        context_data = _ask_gemini(prompt_text, response_type=dict, temperature=1.3)
        if context_data and all(k in context_data for k in ["persona_do_dia", "tema_especifico", "metafora_visual"]):
            context_str = (
                f"PERSONA: {context_data['persona_do_dia']}\n"
                f"TEMA: {context_data['tema_especifico']}\n"
                f"METÁFORA: {context_data['metafora_visual']}"
            )
            logger.info("✅ Contexto dinâmico gerado com sucesso.")
            return context_str
        logger.warning("Resposta do gerador de contexto não continha as chaves esperadas.")
    except Exception as e:
        logger.error("Falha ao gerar contexto dinâmico: %s", e)
    return "PERSONA: Um narrador sábio.\nTEMA: Perseverança.\nMETÁFORA: Uma luz na escuridão."

def _get_history_for_prompt(cache_file: str, limit: int = 15) -> str:
    used_phrases = load_used_phrases(cache_file)
    if not used_phrases: return ""
    return "\n".join([f'- "{phrase}"' for phrase in used_phrases[-limit:]])

def gerar_hashtags_virais(conteudo: str, idioma: str = "auto", n: int = 3) -> List[str]:
    lang = _idioma_norm(idioma)
    try:
        prompt = _load_prompt_template("hashtags").format(n=n, target_lang_name=LANG_NAME_MAP.get(lang, "inglês"), conteudo=conteudo.strip()[:1000])
        raw_list = _ask_gemini(prompt, response_type=list, temperature=0.9, tries=3)
        tags = sorted(list(set(_sanitize_hashtag(t, lang) for t in raw_list if t)), key=len)
        if len(tags) < n:
            fallbacks = {"pt": ["#motivacao", "#inspiracao", "#mindset"], "ar": ["#تحفيز", "#الهام", "#عقلية"], "en": ["#motivation", "#inspiration", "#mindset"]}
            tags.extend(f for f in fallbacks.get(lang, []) if f not in tags and len(tags) < n)
        return tags[:n]
    except Exception as e:
        logger.warning(f"Falha ao gerar hashtags: {e}")
        return []

def _sanitize_hashtag(tag: str, lang: str) -> str:
    t = tag.strip().lstrip("#")
    t = re.sub(r"\s+", "", t)
    if lang in ("pt", "en"):
        t = unicodedata.normalize("NFD", t).encode("ascii", "ignore").decode("ascii")
        t = re.sub(r"[^A-Za-z0-9_]", "", t)
    return f"#{t}" if t else ""

def gerar_prompts_de_imagem_variados(tema: str, quantidade: int, idioma: str = "en", pexels_mode: bool = False) -> List[str]:
    image_mode_from_env = os.getenv("IMAGE_MODE", "pexels").lower()
    if image_mode_from_env == "pexels":
        pexels_mode = True
    
    template_name = "image_prompts_pexels" if pexels_mode else "image_prompts"
    try:
        cache_file = PEXELS_PROMPTS_CACHE_FILE if pexels_mode else ""
        used_prompts = load_used_phrases(cache_file) if pexels_mode else []
        historico_prompts = _get_history_for_prompt(cache_file) if pexels_mode else ""
        
        template = _load_prompt_template(template_name)
        prompt_text = template.format(quantidade=quantidade, tema=tema, historico_prompts=historico_prompts)
        descricoes = _ask_gemini(prompt_text, response_type=list, temperature=1.3, tries=3)
        
        if descricoes:
            used_prompts_set = {p.strip().lower() for p in used_prompts}
            novos_prompts = [p for p in descricoes if p.strip().lower() not in used_prompts_set]
            final_prompts = novos_prompts[:quantidade]
            if len(final_prompts) < quantidade:
                final_prompts.extend([f"{tema} cinematic variation {i+1}" for i in range(quantidade - len(final_prompts))])
            
            if pexels_mode:
                save_used_phrases(used_prompts + final_prompts, cache_file)
            
            logger.info(f"✅ {len(final_prompts)} prompts de imagem ({'Pexels' if pexels_mode else 'IA'}) gerados.")
            return final_prompts
    except Exception as e:
        logger.warning("Falha ao gerar prompts de imagem: %s", e)
    
    return [f"{tema}, cinematic variation {i+1}" for i in range(quantidade)]

def gerar_frase_motivacional(idioma: str = "en") -> str:
    lang = _idioma_norm(idioma)
    prompt_text = _load_prompt_template("short_motivational").format(target_lang_name=LANG_NAME_MAP.get(lang, "inglês"), historico_frases=_get_history_for_prompt(PHRASES_CACHE_FILE))
    used_phrases = load_used_phrases(PHRASES_CACHE_FILE)
    used_phrases_set = {p.strip() for p in used_phrases}
    for _ in range(3):
        phrases = _ask_gemini(prompt_text, response_type=list, temperature=1.2, tries=2)
        valid = [p for p in phrases if 6 <= _count_words_no_markup(p) <= 12 and p.strip() not in used_phrases_set]
        if valid:
            chosen = _ensure_single_emphasis(random.choice(valid), lang)
            used_phrases.append(chosen)
            save_used_phrases(used_phrases, PHRASES_CACHE_FILE)
            return chosen
    return _get_fallback_phrase("motivacional_curta", lang)

def _gerar_e_validar_frase_longa(prompt_text: str, cache_file: str) -> Optional[str]:
    used_phrases = load_used_phrases(cache_file)
    used_phrases_set = {p.strip().lower() for p in used_phrases}
    
    # Pede uma lista, mas está preparado para receber um dicionário
    response_data = _ask_gemini(prompt_text, response_type=list, temperature=1.2, tries=2)
    
    # A função _ask_gemini já foi melhorada para extrair frases de um dict se necessário
    phrases = response_data if isinstance(response_data, list) else []

    if not phrases: return None

    phrases = [_clean_line(p) for p in phrases]
    
    ideal_min, ideal_max = 70, 160
    valid = [p for p in phrases if ideal_min <= _count_words_no_markup(p) <= ideal_max and p.strip().lower() not in used_phrases_set]
    if valid:
        chosen = random.choice(valid)
        used_phrases.append(chosen); save_used_phrases(used_phrases, cache_file)
        logger.info("✅ Frase longa inédita (ideal: %d palavras) selecionada.", _count_words_no_markup(chosen))
        return chosen

    logger.warning("Nenhuma frase no intervalo ideal (%d-%d). Verificando em intervalo flexível...", ideal_min, ideal_max)
    flex_min, flex_max = 40, 200
    valid_flex = [p for p in phrases if flex_min <= _count_words_no_markup(p) <= flex_max and p.strip().lower() not in used_phrases_set]
    if valid_flex:
        chosen = max(valid_flex, key=_count_words_no_markup)
        used_phrases.append(chosen); save_used_phrases(used_phrases, cache_file)
        logger.info("✅ Frase longa inédita (flex: %d palavras) selecionada.", _count_words_no_markup(chosen))
        return chosen

    return None

def gerar_frase_motivacional_longa(idioma: str = "en") -> str:
    lang = _idioma_norm(idioma)
    for creative_attempt in range(1, 4):
        logger.info(f"Ciclo Criativo [{creative_attempt}/3]: Gerando novo contexto...")
        dynamic_context = _gerar_contexto_dinamico("motivacional", lang)
        prompt_text = _load_prompt_template("long_motivational").format(
            dynamic_context=dynamic_context,
            target_lang_name=LANG_NAME_MAP.get(lang, "inglês"), 
            historico_frases=_get_history_for_prompt(LONG_PHRASES_CACHE_FILE)
        )
        chosen = _gerar_e_validar_frase_longa(prompt_text, LONG_PHRASES_CACHE_FILE)
        if chosen: return chosen
        logger.warning(f"Contexto não resultou em frase válida. Tentando novo ciclo...")
        if creative_attempt < 3: time.sleep(1)
    
    logger.error("Falha em gerar frase longa após 3 ciclos. Usando fallback.")
    return _get_fallback_phrase("motivacional_longa", lang)

def gerar_prompt_tarot(idioma: str = "en") -> str:
    return "mesa de tarô mística"

def gerar_frase_tarot_curta(idioma: str = "en") -> str:
    lang = _idioma_norm(idioma)
    prompt_text = _load_prompt_template("short_tarot").format(target_lang_name=LANG_NAME_MAP.get(lang, "inglês"), historico_frases=_get_history_for_prompt(PHRASES_CACHE_FILE))
    used_phrases = load_used_phrases(PHRASES_CACHE_FILE)
    used_phrases_set = {p.strip() for p in used_phrases}
    for _ in range(3):
        phrases = _ask_gemini(prompt_text, response_type=list, temperature=1.2)
        valid = [p for p in phrases if p.strip() not in used_phrases_set]
        if valid:
            chosen = _ensure_single_emphasis(random.choice(valid), lang)
            used_phrases.append(chosen); save_used_phrases(used_phrases, PHRASES_CACHE_FILE)
            return chosen
    return _get_fallback_phrase("tarot_curta", lang)

def gerar_frase_tarot_longa(idioma: str = "en") -> str:
    lang = _idioma_norm(idioma)
    for creative_attempt in range(1, 4):
        logger.info(f"Ciclo Criativo [{creative_attempt}/3]: Gerando novo contexto...")
        dynamic_context = _gerar_contexto_dinamico("tarot", lang)
        prompt_text = _load_prompt_template("long_tarot").format(
            dynamic_context=dynamic_context,
            target_lang_name=LANG_NAME_MAP.get(lang, "inglês"), 
            historico_frases=_get_history_for_prompt(LONG_PHRASES_CACHE_FILE)
        )
        chosen = _gerar_e_validar_frase_longa(prompt_text, LONG_PHRASES_CACHE_FILE)
        if chosen: return chosen
        logger.warning(f"Contexto não resultou em frase válida. Tentando novo ciclo...")
        if creative_attempt < 3: time.sleep(1)

    logger.error("Falha em gerar frase longa após 3 ciclos. Usando fallback.")
    return _get_fallback_phrase("tarot_longa", lang)