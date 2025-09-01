# utils/frase.py (Copie e cole este conteúdo)
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
PEXELS_PROMPTS_CACHE_FILE = os.path.join(CACHE_DIR, "used_pexels_prompts.json") # <--- NOVO CACHE
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
LANG_NAME_MAP = {"pt": "português do Brasil", "en": "inglês", "ar": "árabe egípcio"}

def _load_prompt_template(template_name: str) -> str:
    """Carrega um template de prompt de um arquivo YAML."""
    if not _HAVE_YAML:
        raise ImportError("A biblioteca PyYAML é necessária. Instale com: pip install PyYAML")
    
    config_path = Path(__file__).parent / "prompts" / f"{template_name}.yaml"
    
    if not config_path.exists():
        config_path = Path("prompts") / f"{template_name}.yaml"

    if not config_path.exists():
        raise FileNotFoundError(f"Arquivo de template de prompt '{config_path.name}' não encontrado em {config_path.resolve()} ou na pasta /prompts")
    
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

def _ask_gemini(prompt: str, response_type: type = list, temperature: float = 1.0, tries: int = 3):
    """Função genérica para fazer perguntas ao Gemini e esperar um tipo de JSON (list ou dict)."""
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

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                if raw.startswith('[') and raw.endswith(']'):
                    list_data = json.loads(raw)
                    if isinstance(list_data, list) and len(list_data) > 0 and isinstance(list_data[0], str):
                        data = json.loads(list_data[0])
                else:
                    raise

            if isinstance(data, response_type):
                if response_type == list:
                    return [c for it in data if isinstance(it, str) and (c := _clean_line(it))]
                return data
        except Exception as e:
            logger.warning("Falha ao pedir/processar JSON ao Gemini (tentativa %d/%d): %s", attempt + 1, tries, e)
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

def quebrar_em_duas_linhas(frase: str) -> str:
    palavras = frase.split()
    if len(palavras) < 4: return frase
    ponto_de_quebra = (len(palavras) + 1) // 2
    return f'{" ".join(palavras[:ponto_de_quebra])}\n{" ".join(palavras[ponto_de_quebra:])}'

# ### Funções de ênfase (centralizadas) ###
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

# ### LÓGICA DE GERAÇÃO DE CONTEÚDO DINÂMICO ###
def _gerar_contexto_dinamico(content_mode: str, lang: str) -> str:
    """ETAPA 1: Gera uma persona, tema e metáfora únicos para o vídeo do dia."""
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
        
        logger.warning("A resposta do gerador de contexto não continha as chaves esperadas.")
    except Exception as e:
        logger.error("Falha ao gerar contexto dinâmico: %s", e)

    return "PERSONA: Um narrador sábio e gentil.\nTEMA: A importância da perseverança.\nMETÁFORA: Uma pequena luz na escuridão."

# -----------------------------------------------------------------------------#
# Funções Principais (Hashtags, Prompts, Frases)
# -----------------------------------------------------------------------------#
def _get_history_for_prompt(cache_file: str, limit: int = 15) -> str:
    used_phrases = load_used_phrases(cache_file)
    if not used_phrases: return ""
    formatted_list = "\n".join([f'- "{phrase}"' for phrase in used_phrases[-limit:]])
    return f"\n{formatted_list}\n"

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
    raw_list = _ask_gemini(prompt, response_type=list, temperature=0.9, tries=3)
    tags = _dedupe_hashtags([_sanitize_hashtag(t, lang) for t in raw_list if t])
    if len(tags) < n:
        fallbacks = {"pt": ["#motivacao", "#inspiracao", "#mindset", "#disciplina", "#foco"], "ar": ["#تحفيز", "#الهام", "#انجاز", "#انضباط", "#تركيز"], "en": ["#motivation", "#inspiration", "#mindset", "#discipline", "#focus"]}
        tags.extend(f for f in fallbacks.get(lang, []) if f.lower() not in {t.lower() for t in tags} and len(tags) < n)
    return tags[:n]

def gerar_prompts_de_imagem_variados(tema: str, quantidade: int, idioma: str = "en", pexels_mode: bool = False) -> List[str]:
    """Gera prompts de imagem para Pexels (simples) ou IA (complexo)."""
    if not os.getenv("GEMINI_API_KEY"):
        logger.error("❌ GEMINI_API_KEY não configurada.")
        return [f"{tema}, variação {i+1}" for i in range(quantidade)]
    
    image_mode_from_env = os.getenv("IMAGE_MODE", "pexels").lower()
    if image_mode_from_env == "pexels":
        if not pexels_mode:
            logger.debug("Forçando pexels_mode=True com base no .env (IMAGE_MODE=pexels)")
        pexels_mode = True
    
    template_name = "image_prompts_pexels" if pexels_mode else "image_prompts"
    
    try:
        # <--- MUDANÇA AQUI: Lógica de cache e histórico para prompts do Pexels --->
        cache_file = PEXELS_PROMPTS_CACHE_FILE if pexels_mode else ""
        historico_prompts = ""
        used_prompts = []
        if pexels_mode and cache_file:
            used_prompts = load_used_phrases(cache_file)
            historico_prompts = "\n".join([f"- {p}" for p in used_prompts[-20:]]) # Envia os últimos 20 para o prompt

        template = _load_prompt_template(template_name)
        format_args = {
            "quantidade": quantidade,
            "tema": tema,
            "target_lang_name": LANG_NAME_MAP.get(_lang_tag(idioma), "inglês"),
            "historico_prompts": historico_prompts
        }
        prompt_text = template.format(**format_args)
        descricoes = _ask_gemini(prompt_text, response_type=list, temperature=1.3, tries=3)
        
        # Filtra e salva os novos prompts
        if descricoes:
            used_prompts_set = {p.strip().lower() for p in used_prompts}
            novos_prompts = [p for p in descricoes if p.strip().lower() not in used_prompts_set]

            if len(novos_prompts) < quantidade:
                logger.warning(f"Gerou apenas {len(novos_prompts)} prompts inéditos para Pexels. Usando-os e preenchendo com fallbacks se necessário.")

            final_prompts = novos_prompts[:quantidade]
            if len(final_prompts) < quantidade:
                 final_prompts.extend([f"{tema} cinematic variation {i+1}" for i in range(quantidade - len(final_prompts))])

            if pexels_mode and cache_file and final_prompts:
                save_used_phrases(used_prompts + final_prompts, cache_file)
            
            logger.info(f"✅ {len(final_prompts)} prompts de imagem ({'Pexels' if pexels_mode else 'IA'}) gerados para '{tema}'.")
            return final_prompts

    except Exception as e:
        logger.warning("Falha ao gerar prompts de imagem: %s", e)

    logger.warning("Não foi possível gerar prompts criativos. Usando fallback.")
    fallback_suffix = "cinematic" if pexels_mode else "cinematic, high detail, 8k --no text"
    return [f"{tema}, {fallback_suffix}, variation {i+1}" for i in range(quantidade)]

def gerar_frase_motivacional(idioma: str = "en") -> str:
    lang = _lang_tag(idioma)
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
    logger.warning("Nenhuma frase curta inédita foi gerada, usando fallback.")
    return _get_fallback_phrase("motivacional_curta", lang)

def _gerar_e_validar_frase_longa(prompt_text: str, cache_file: str) -> Optional[str]:
    """Lógica interna para gerar e validar frases longas com fallback de contagem de palavras."""
    used_phrases = load_used_phrases(cache_file)
    used_phrases_set = {p.strip() for p in used_phrases}

    response_data = _ask_gemini(prompt_text, response_type=list, temperature=1.2, tries=2)
    
    phrases = []
    if isinstance(response_data, list):
        phrases = response_data
    elif isinstance(response_data, dict):
        phrases = [str(v) for v in response_data.values() if isinstance(v, str)]

    if not phrases:
        return None

    ideal_min, ideal_max = 70, 160
    valid = [p for p in phrases if ideal_min <= _count_words_no_markup(p) <= ideal_max and p.strip() not in used_phrases_set]
    if valid:
        chosen = random.choice(valid)
        used_phrases.append(chosen)
        save_used_phrases(used_phrases, cache_file)
        logger.info("✅ Frase longa inédita (ideal: %d palavras) selecionada.", _count_words_no_markup(chosen))
        return chosen

    logger.warning("Nenhuma frase no intervalo ideal (%d-%d palavras). Verificando em intervalo flexível...", ideal_min, ideal_max)
    flex_min, flex_max = 40, 200
    valid_flex = [p for p in phrases if flex_min <= _count_words_no_markup(p) <= flex_max and p.strip() not in used_phrases_set]
    if valid_flex:
        chosen = max(valid_flex, key=_count_words_no_markup)
        used_phrases.append(chosen)
        save_used_phrases(used_phrases, cache_file)
        logger.info("✅ Frase longa inédita (flex: %d palavras) selecionada.", _count_words_no_markup(chosen))
        return chosen

    return None

def gerar_frase_motivacional_longa(idioma: str = "en") -> str:
    lang = _lang_tag(idioma)
    
    for creative_attempt in range(1, 4):
        logger.info(f"Ciclo Criativo [{creative_attempt}/3]: Gerando novo contexto...")
        
        dynamic_context = _gerar_contexto_dinamico("motivacional", lang)
        
        prompt_text = _load_prompt_template("long_motivational").format(
            dynamic_context=dynamic_context,
            target_lang_name=LANG_NAME_MAP.get(lang, "inglês"), 
            historico_frases=_get_history_for_prompt(LONG_PHRASES_CACHE_FILE)
        )
        
        chosen = _gerar_e_validar_frase_longa(prompt_text, LONG_PHRASES_CACHE_FILE)
        if chosen:
            return chosen
        
        logger.warning(f"O contexto gerado não resultou em uma frase válida. Tentando novo ciclo criativo...")
        if creative_attempt < 3:
            time.sleep(1)

    logger.error("Falha em gerar uma frase longa válida após 3 ciclos criativos. Usando fallback.")
    return _get_fallback_phrase("motivacional_longa", lang)

def gerar_prompt_tarot(idioma: str = "en") -> str:
    """Retorna um tema simples e fixo para ser usado na busca de imagens de tarot."""
    tema_fixo = "mesa de tarô mística"
    logger.info("Usando tema base para tarot: '%s'", tema_fixo)
    return tema_fixo

def gerar_frase_tarot_curta(idioma: str = "en") -> str:
    lang = _lang_tag(idioma)
    prompt_text = _load_prompt_template("short_tarot").format(target_lang_name=LANG_NAME_MAP.get(lang, "inglês"), historico_frases=_get_history_for_prompt(PHRASES_CACHE_FILE))
    used_phrases = load_used_phrases(PHRASES_CACHE_FILE)
    used_phrases_set = {p.strip() for p in used_phrases}
    for _ in range(3):
        phrases = _ask_gemini(prompt_text, response_type=list, temperature=1.2)
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

    for creative_attempt in range(1, 4):
        logger.info(f"Ciclo Criativo [{creative_attempt}/3]: Gerando novo contexto...")

        dynamic_context = _gerar_contexto_dinamico("tarot", lang)

        prompt_text = _load_prompt_template("long_tarot").format(
            dynamic_context=dynamic_context,
            target_lang_name=LANG_NAME_MAP.get(lang, "inglês"), 
            historico_frases=_get_history_for_prompt(LONG_PHRASES_CACHE_FILE)
        )

        chosen = _gerar_e_validar_frase_longa(prompt_text, LONG_PHRASES_CACHE_FILE)
        if chosen:
            return chosen

        logger.warning(f"O contexto gerado não resultou em uma frase válida. Tentando novo ciclo criativo...")
        if creative_attempt < 3:
            time.sleep(1)

    logger.error("Falha em gerar uma frase longa válida após 3 ciclos criativos. Usando fallback.")
    return _get_fallback_phrase("tarot_longa", lang)