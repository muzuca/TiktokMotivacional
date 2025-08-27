# utils/frase.py
import os
import re
import unicodedata
import random
import json
import hashlib
import time
from typing import List, Set, Tuple, Optional
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
# Cache
# -----------------------------------------------------------------------------#
CACHE_DIR = "cache"
os.makedirs(CACHE_DIR, exist_ok=True)
PHRASES_CACHE_FILE = os.path.join(CACHE_DIR, "used_phrases.json")
LONG_PHRASES_CACHE_FILE = os.path.join(CACHE_DIR, "used_long_phrases.json")


def load_used_phrases(cache_file: str) -> Set[str]:
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return set(data)
        except Exception:
            pass
    return set()


def save_used_phrases(used_phrases: Set[str], cache_file: str) -> None:
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(sorted(list(used_phrases)), f, ensure_ascii=False)
    except Exception as e:
        logger.warning("Não foi possível salvar cache de frases em %s: %s", os.path.basename(cache_file), e)

# -----------------------------------------------------------------------------#
# Helpers
# -----------------------------------------------------------------------------#
_PT_SW = {
    "a","o","os","as","um","uma","de","da","do","das","dos","em","no","na","nos","nas",
    "e","ou","pra","para","por","que","se","com","sem","ao","à","às","aos","é","ser","ter"
}
_EN_SW = {
    "a","an","the","and","or","of","in","on","to","for","with","that","is","it","you","your"
}

LANG_NAME_MAP = {
    "pt": "português do Brasil",
    "en": "inglês",
    "ar": "árabe"
}

def _load_prompt_template(template_name: str) -> str:
    """Carrega um template de prompt de um arquivo YAML."""
    if not _HAVE_YAML:
        raise ImportError("A biblioteca PyYAML é necessária para carregar prompts de arquivos. Instale com: pip install PyYAML")
    
    config_path = Path(__file__).parent / "prompts" / f"{template_name}.yaml"
    
    if not config_path.exists():
        raise FileNotFoundError(f"Arquivo de template de prompt '{config_path}' não encontrado.")
    
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
        template = data.get("template")
        if not template or not isinstance(template, str):
            raise ValueError(f"O arquivo YAML '{config_path}' não contém a chave 'template' ou ela está vazia.")
        return template.strip()


def _lang_tag(idioma: Optional[str]) -> str:
    s = (idioma or "en").strip().lower()
    if s in ("pt", "pt-br", "br", "brasil", "portugues", "português"):
        return "pt"
    if s.startswith("ar"):
        return "ar"
    return "en"

def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def _clean_line(s: str) -> str:
    if not s:
        return ""
    line = s.strip()
    line = re.sub(r'^\s*(?:\d+[\).\s-]+|[-*•]\s+)', '', line).strip()
    line = line.strip(' "\'')
    line = re.sub(r'\s+', ' ', line)
    return line

def _strip_emph(s: str) -> str:
    return re.sub(r'\*\*([^*]+)\*\*', r'\1', s)

def _count_words_no_markup(s: str) -> int:
    return len(re.findall(r"\b[\w’'-]+\b", _strip_emph(s), flags=re.UNICODE))

def _ask_json_list(prompt: str, temperature: float = 1.0, tries: int = 3) -> List[str]:
    for attempt in range(tries):
        try:
            resp = model.generate_content(
                prompt,
                generation_config={
                    "response_mime_type": "application/json",
                    "temperature": temperature,
                    "top_p": 0.95,
                },
                request_options={"timeout": GEMINI_TIMEOUT_SEC},
            )
            raw = (getattr(resp, "text", "") or "").strip()
            data = json.loads(raw)
            if isinstance(data, list):
                out = []
                for it in data:
                    if isinstance(it, str):
                        c = _clean_line(it)
                        if c:
                            out.append(c)
                if out:
                    return out
        except Exception as e:
            logger.warning("Falha ao pedir lista JSON ao Gemini (tentativa %d/%d): %s", attempt + 1, tries, e)
        time.sleep(0.8 * (attempt + 1))
    return []

def _ensure_single_emphasis(text: str, lang: str, prefer_last_n: int = 2) -> str:
    sw = _PT_SW if lang == "pt" else _EN_SW
    base = _clean_line(text)

    spans = list(re.finditer(r'\*\*([^*]+)\*\*', base))
    if len(spans) == 1:
        inner = spans[0].group(1).strip()
        words = [w for w in re.findall(r"[\w’'-]+", inner) if w]
        if len(words) > 2:
            inner = " ".join(words[-2:])
        base_no = re.sub(r'\*\*([^*]+)\*\*', r'\1', base)
        idx = base_no.lower().rfind(inner.lower())
        if idx >= 0:
            return base_no[:idx] + "**" + base_no[idx:idx+len(inner)] + "**" + base_no[idx+len(inner):]

    base = _strip_emph(base)

    tokens = re.findall(r"\w+|\W+", base, flags=re.UNICODE)
    picks: List[int] = []
    for i in range(len(tokens)-1, -1, -1):
        if re.match(r"\w+", tokens[i], flags=re.UNICODE):
            word = tokens[i].strip(" _").lower()
            if len(word) >= 3 and word not in sw:
                picks.append(i)
                if len(picks) >= prefer_last_n:
                    break
    if not picks:
        return base
    picks = sorted(picks)
    i0, i1 = picks[0], picks[-1]
    tokens[i0] = "**" + tokens[i0]
    tokens[i1] = tokens[i1] + "**"
    return "".join(tokens)

def gerar_slug(frase: str) -> str:
    frase_limpa = re.sub(r'[\*\.:\?!,"\'`´’]', '', frase.lower())
    palavras = frase_limpa.split()
    slug = "_".join(palavras[:6])
    return slug[:40]

def quebrar_em_duas_linhas(frase: str) -> str:
    palavras = frase.split()
    if len(palavras) < 4:
        return frase
    ponto_de_quebra = (len(palavras) + 1) // 2
    linha1 = " ".join(palavras[:ponto_de_quebra])
    linha2 = " ".join(palavras[ponto_de_quebra:])
    return f"{linha1}\n{linha2}"

# -----------------------------------------------------------------------------#
# Hashtags
# -----------------------------------------------------------------------------#
def _sanitize_hashtag(tag: str, lang: str) -> str:
    if not tag:
        return ""
    t = tag.strip()
    if t.startswith("#"):
        t = t[1:]
    t = re.sub(r"\s+", "", t)
    if lang in ("pt", "en"):
        t = unicodedata.normalize("NFD", t)
        t = t.encode("ascii", "ignore").decode("ascii")
        t = re.sub(r"[^A-Za-z0-9_]", "", t)
        t = t.lower()
    else:
        t = re.sub(r"[^\w\u0600-\u06FF_]+", "", t, flags=re.UNICODE)
    t = t[:30]
    return f"#{t}" if t else ""

def _dedupe_hashtags(tags: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for t in tags:
        key = t.lower()
        if t and key not in seen:
            seen.add(key)
            out.append(t)
    return out

def gerar_hashtags_virais(conteudo: str, idioma: str = "auto", n: int = 3, plataforma: str = "tiktok") -> List[str]:
    lang = _lang_tag(idioma)
    n = max(1, int(n or 3))

    if not os.getenv("GEMINI_API_KEY"):
        logger.warning("GEMINI_API_KEY ausente — usando fallback de hashtags.")
        if lang == "pt": base = ["#motivacao", "#inspiracao", "#mindset"]
        elif lang == "ar": base = ["#تحفيز", "#الهام", "#عقلية"]
        else: base = ["#motivation", "#inspiration", "#mindset"]
        return base[:n]
    
    target_lang_name = LANG_NAME_MAP.get(lang, "inglês")
    
    template = _load_prompt_template("hashtags")
    prompt = template.format(
        n=n,
        target_lang_name=target_lang_name,
        conteudo=conteudo.strip()[:1000]
    )

    raw_list = _ask_json_list(prompt, temperature=0.9, tries=3)
    tags = _dedupe_hashtags([_sanitize_hashtag(t, lang) for t in raw_list if t])
    
    if len(tags) < n:
        if lang == "pt": fallback = ["#motivacao", "#inspiracao", "#mindset", "#disciplina", "#foco"]
        elif lang == "ar": fallback = ["#تحفيز", "#الهام", "#انجاز", "#انضباط", "#تركيز"]
        else: fallback = ["#motivation", "#inspiration", "#mindset", "#discipline", "#focus"]
        for f in fallback:
            if len(tags) >= n: break
            if f.lower() not in {x.lower() for x in tags}: tags.append(f)
    return tags[:n]

# -----------------------------------------------------------------------------#
# Geradores de Prompt de Imagem
# -----------------------------------------------------------------------------#
def gerar_prompts_de_imagem_variados(tema: str, quantidade: int, idioma: str = "en") -> List[str]: # A assinatura mantém 'idioma' por consistência, mas não será usado.
    if not os.getenv("GEMINI_API_KEY"):
        logger.error("❌ GEMINI_API_KEY não configurada.")
        return [f"{tema}, variação {i+1}" for i in range(quantidade)]

    # --- ALTERAÇÃO PRINCIPAL AQUI ---
    # Ignora o idioma de entrada e força a geração dos prompts de imagem em português
    # para maximizar a criatividade na ideação.
    target_lang_name = LANG_NAME_MAP["pt"]
    # --- FIM DA ALTERAÇÃO ---

    template = _load_prompt_template("image_prompts")
    prompt_text = template.format(
        tema=tema,
        quantidade=quantidade,
        target_lang_name=target_lang_name
    )

    descricoes = _ask_json_list(prompt_text, temperature=1.3, tries=3)
    
    if descricoes and len(descricoes) >= quantidade:
        logger.info(f"✅ {len(descricoes)} prompts de imagem criativos gerados para o tema '{tema}'.")
        return descricoes[:quantidade]

    logger.warning("Não foi possível gerar a quantidade desejada de prompts criativos. Usando fallback.")
    return [f"{tema}, cinematic, high detail, variation {i+1}" for i in range(quantidade)]

def gerar_prompt_paisagem(idioma: str = "en") -> str:
    prompts = gerar_prompts_de_imagem_variados("paisagem bonita", 1, idioma)
    return prompts[0] if prompts else "Montanhas ao nascer do sol"

# -----------------------------------------------------------------------------#
# Geradores de Frase Principal
# -----------------------------------------------------------------------------#
def gerar_frase_motivacional(idioma: str = "en") -> str:
    lang = _lang_tag(idioma)
    target_lang_name = LANG_NAME_MAP.get(lang, "inglês")
    
    template = _load_prompt_template("short_motivational")
    prompt_text = template.format(target_lang_name=target_lang_name)

    used_phrases = load_used_phrases(PHRASES_CACHE_FILE)
    phrases = _ask_json_list(prompt_text, temperature=1.2, tries=5)
    
    valid_phrases = [
        _ensure_single_emphasis(p, lang) for p in phrases if 6 <= _count_words_no_markup(p) <= 12 and _md5(p) not in used_phrases
    ]
    
    if valid_phrases:
        chosen = random.choice(valid_phrases)
        used_phrases.add(_md5(chosen))
        save_used_phrases(used_phrases, PHRASES_CACHE_FILE)
        return _clean_line(chosen)

    logger.warning("Nenhuma frase curta inédita foi gerada, usando fallback.")
    return "Você tem o **poder** de criar a vida que deseja." if lang == "pt" else "You have the **power** to create the life you desire."

def gerar_frase_motivacional_longa(idioma: str = "en") -> str:
    """Gera uma frase longa para narração, garantindo que seja inédita."""
    lang = _lang_tag(idioma)
    target_lang_name = LANG_NAME_MAP.get(lang, "inglês")

    template = _load_prompt_template("long_motivational")
    prompt_text = template.format(target_lang_name=target_lang_name)
    
    fallback = (
        "Lembre-se que cada passo que você dá, por menor que seja, é um movimento poderoso na direção da vida extraordinária que você não só merece, mas é capaz de construir."
        if lang == "pt" else
        "Remember that every step you take, no matter how small, is a powerful movement towards the extraordinary life you not only deserve, but are capable of building."
    )
    if lang == "ar":
        fallback = "تذكر أن كل خطوة تخطوها، مهما كانت صغيرة، هي حركة قوية نحو الحياة الاستثنائية التي لا تستحقها فحسب، بل أنت قادر على بنائها."

    used_long_phrases = load_used_phrases(LONG_PHRASES_CACHE_FILE)
    phrases = _ask_json_list(prompt_text, temperature=1.2, tries=4)
    
    valid_phrases = [
        p for p in phrases if 25 <= _count_words_no_markup(p) <= 40 and _md5(p) not in used_long_phrases
    ]
    
    if valid_phrases:
        chosen = random.choice(valid_phrases)
        used_long_phrases.add(_md5(chosen))
        save_used_phrases(used_long_phrases, LONG_PHRASES_CACHE_FILE)
        logger.info("Frase longa inédita selecionada para narração.")
        return _clean_line(chosen)

    logger.warning("Nenhuma frase longa inédita foi gerada, usando fallback.")
    return fallback

# -----------------------------------------------------------------------------#
# Tarot
# -----------------------------------------------------------------------------#
def gerar_prompt_tarot(idioma: str = "en") -> str:
    prompts = gerar_prompts_de_imagem_variados("mesa de tarô mística", 1, idioma)
    return prompts[0] if prompts else 'Uma mesa de tarô com cartas espalhadas, iluminação mística, cristais e velas, close-up'

def gerar_frase_tarot_curta(idioma: str = "en") -> str:
    lang = _lang_tag(idioma)
    target_lang_name = LANG_NAME_MAP.get(lang, "inglês")
    
    template = _load_prompt_template("short_tarot")
    prompt_text = template.format(target_lang_name=target_lang_name)
    
    phrases = _ask_json_list(prompt_text, temperature=1.2)
    if phrases:
        return _ensure_single_emphasis(random.choice(phrases), lang)
    
    logger.warning("Nenhuma frase curta de tarot foi gerada, usando fallback.")
    return "As cartas revelam um novo **caminho** para você." if lang == "pt" else "The cards reveal a new **path** for you."

def gerar_frase_tarot_longa(idioma: str = "en") -> str:
    """Gera uma frase longa de tarot para narração, garantindo que seja inédita."""
    lang = _lang_tag(idioma)
    target_lang_name = LANG_NAME_MAP.get(lang, "inglês")

    template = _load_prompt_template("long_tarot")
    prompt_text = template.format(target_lang_name=target_lang_name)
    
    fallback = (
        "As energias cósmicas se alinham para iluminar sua jornada, revelando verdades ocultas nas sombras do tempo e oferecendo a clareza que sua alma anseia para evoluir."
        if lang == "pt" else
        "The cosmic energies align to illuminate your journey, revealing hidden truths in the shadows of time and offering the clarity your soul craves to evolve."
    )
    if lang == "ar":
        fallback = "الطاقات الكونية تتراصف لتنير رحلتك، كاشفة عن حقائق خفية في ظلال الزمن ومانحة الوضوح الذي تتوق إليه روحك للتطور."

    used_long_phrases = load_used_phrases(LONG_PHRASES_CACHE_FILE)
    phrases = _ask_json_list(prompt_text, temperature=1.2, tries=4)
    
    valid_phrases = [
        p for p in phrases if 25 <= _count_words_no_markup(p) <= 40 and _md5(p) not in used_long_phrases
    ]
    
    if valid_phrases:
        chosen = random.choice(valid_phrases)
        used_long_phrases.add(_md5(chosen))
        save_used_phrases(used_long_phrases, LONG_PHRASES_CACHE_FILE)
        logger.info("Frase longa de tarot inédita selecionada para narração.")
        return _clean_line(chosen)

    logger.warning("Nenhuma frase longa de tarot inédita foi gerada, usando fallback.")
    return fallback