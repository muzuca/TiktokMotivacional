# utils/frase.py
import os
import re
import unicodedata
import random
import json
import hashlib
import time
from typing import List, Set, Tuple, Optional

from dotenv import load_dotenv
import google.generativeai as genai
import logging

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
# NOVO CACHE PARA FRASES LONGAS (NARRAÇÃO)
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
    
    if lang == "pt":
        prompt = (f"Você é um especialista em crescimento no TikTok. Dado o conteúdo abaixo, gere EXATAMENTE {n} hashtags curtas e virais em português do Brasil, específicas para esse conteúdo. Sem explicações. Não inclua espaços, pontuação nem emojis. Evite acentos. Retorne APENAS um array JSON de strings iniciadas por #.\n\nCONTEÚDO:\n{conteudo.strip()[:1000]}")
    elif lang == "ar":
        prompt = (f"أنت خبير نمو على تيك توك. بناءً على المحتوى أدناه، أنشئ بالضبط {n} وسوماً قصيرة ورائجة ومناسبة للمحتوى، باللغة العربية. بدون شروح. لا مسافات أو علامات ترقيم أو رموز. أعد فقط مصفوفة JSON من سلاسل تبدأ بـ #.\n\nالمحتوى:\n{conteudo.strip()[:1000]}")
    else:
        prompt = (f"You are a TikTok growth expert. Given the content below, generate EXACTLY {n} short, viral, content-specific hashtags in English. No explanations. No spaces, punctuation, or emojis. Return ONLY a JSON array of strings starting with #.\n\nCONTENT:\n{conteudo.strip()[:1000]}")

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
def gerar_prompts_de_imagem_variados(tema: str, quantidade: int, idioma: str = "en") -> List[str]:
    if not os.getenv("GEMINI_API_KEY"):
        logger.error("❌ GEMINI_API_KEY não configurada.")
        return [f"{tema}, variação {i+1}" for i in range(quantidade)]

    lang = _lang_tag(idioma)
    
    if lang == 'pt':
        prompt_text = (
            f"Crie uma lista de {quantidade} descrições de imagem curtas e variadas para um gerador de imagens de IA, com o tema '{tema}'. "
            "As descrições devem ser fotorrealistas, cinematográficas, com ângulos de câmera diferentes (close-up, plano médio, etc.) e adequadas para um vídeo vertical (9:16). "
            "Cada descrição deve ter no máximo 15 palavras. Retorne APENAS um array JSON de strings."
        )
    else:
        prompt_text = (
            f"Create a list of {quantidade} short and varied image descriptions for an AI image generator, themed around '{tema}'. "
            "The descriptions should be photorealistic, cinematic, with different camera angles (e.g., close-up, medium shot), and suitable for a vertical video (9:16). "
            "Each description must be 15 words or less. Return ONLY a JSON array of strings."
        )

    descricoes = _ask_json_list(prompt_text, temperature=1.0, tries=3)
    
    if descricoes and len(descricoes) >= quantidade:
        logger.info(f"✅ {len(descricoes)} prompts de imagem gerados para o tema '{tema}'.")
        return descricoes[:quantidade]

    logger.warning("Não foi possível gerar a quantidade desejada de prompts. Usando fallback.")
    return [f"{tema}, cinematic, high detail, variation {i+1}" for i in range(quantidade)]

def gerar_prompt_paisagem(idioma: str = "en") -> str:
    prompts = gerar_prompts_de_imagem_variados("paisagem bonita", 1, idioma)
    return prompts[0] if prompts else "Montanhas ao nascer do sol"

# -----------------------------------------------------------------------------#
# Geradores de Frase Principal
# -----------------------------------------------------------------------------#
def gerar_frase_motivacional(idioma: str = "en") -> str:
    lang = _lang_tag(idioma)
    
    if lang == "pt":
        prompt_text = "Crie 3 frases motivacionais curtas e de impacto (6-12 palavras). Marque a palavra-chave principal com **negrito**. Retorne APENAS um array JSON de strings."
    elif lang == "ar":
        prompt_text = "أنشئ 3 جمل تحفيزية قصيرة ومؤثرة (6-12 كلمة). ضع الكلمة الرئيسية **بخط عريض**. أعد فقط مصفوفة JSON من السلاسل."
    else:
        prompt_text = "Create 3 short, impactful motivational phrases (6-12 words). Mark the main keyword with **bold**. Return ONLY a JSON array of strings."

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

    return "Você tem o **poder** de criar a vida que deseja." if lang == "pt" else "You have the **power** to create the life you desire."

# --- FUNÇÃO ATUALIZADA ---
def gerar_frase_motivacional_longa(idioma: str = "en") -> str:
    """Gera uma frase longa para narração, garantindo que seja inédita."""
    lang = _lang_tag(idioma)

    if lang == "pt":
        prompt_text = "Crie 3 frases motivacionais longas para narração (15 a 25 palavras), com tom inspirador. Retorne APENAS um array JSON de strings."
        fallback = "Acredite no poder dos seus sonhos e na força da sua determinação para alcançar o impossível."
    elif lang == "ar":
        prompt_text = "أنشئ 3 جمل تحفيزية طويلة للتعليق الصوتي (15 إلى 25 كلمة)، بنبرة ملهمة. أعد فقط مصفوفة JSON من السلاسل."
        fallback = "آمن بقوة أحلامك وبقوة عزيمتك لتحقيق المستحيل."
    else:
        prompt_text = "Create 3 long motivational phrases for narration (15 to 25 words), with an inspiring tone. Return ONLY a JSON array of strings."
        fallback = "Believe in the power of your dreams and the strength of your determination to achieve the impossible."
    
    used_long_phrases = load_used_phrases(LONG_PHRASES_CACHE_FILE)
    phrases = _ask_json_list(prompt_text, temperature=1.1, tries=4)
    
    valid_phrases = [
        p for p in phrases if 15 <= _count_words_no_markup(p) <= 25 and _md5(p) not in used_long_phrases
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
    lang = _lang_tag(idioma)
    if lang == 'pt':
        return 'Uma mesa de tarô com cartas espalhadas, iluminação mística, cristais e velas, close-up'
    elif lang == 'ar':
        return 'طاولة تاروت مع بطاقات منتشرة، إضاءة غامضة، بلورات وشموع، لقطة مقربة'
    return 'A tarot table with cards spread out, mystical lighting, crystals and candles, close-up shot'

def gerar_frase_tarot_curta(idioma: str = "en") -> str:
    lang = _lang_tag(idioma)
    if lang == "pt":
        prompt_text = "Crie 3 frases curtas (6-10 palavras) no estilo de uma cartomante revelando um conselho. Use palavras como 'as cartas revelam', 'o destino mostra'. Marque a palavra-chave com **negrito**. Retorne um array JSON de strings."
    elif lang == "ar":
        prompt_text = "أنشئ 3 جمل قصيرة (6-10 كلمات) بأسلوب قارئة تاروت تكشف عن نصيحة. استخدم كلمات مثل 'الكروت تكشف'، 'القدر يظهر'. ضع الكلمة الرئيسية **بخط عريض**. أعد مصفوفة JSON من السلاسل."
    else:
        prompt_text = "Create 3 short phrases (6-10 words) in the style of a fortune teller revealing advice. Use words like 'the cards reveal', 'destiny shows'. Mark the keyword with **bold**. Return a JSON array of strings."
    
    phrases = _ask_json_list(prompt_text, temperature=1.1)
    if phrases:
        return _ensure_single_emphasis(random.choice(phrases), lang)
    return "As cartas revelam um novo **caminho** para você."

# --- FUNÇÃO ATUALIZADA ---
def gerar_frase_tarot_longa(idioma: str = "en") -> str:
    """Gera uma frase longa de tarot para narração, garantindo que seja inédita."""
    lang = _lang_tag(idioma)
    if lang == "pt":
        prompt_text = "Crie 3 mensagens enigmáticas e inspiradoras de 15 a 25 palavras, como uma cartomante falando. Retorne APENAS um array JSON de strings."
        fallback = "A energia do universo conspira a seu favor, ouça os sussurros do destino que te guiam."
    elif lang == "ar":
        prompt_text = "أنشئ 3 رسائل غامضة وملهمة من 15 إلى 25 كلمة، كأنها من قارئة تاروت. أعد فقط مصفوفة JSON من السلاسل."
        fallback = "طاقة الكون تتآمر لصالحك، استمع إلى همسات القدر التي ترشدك."
    else:
        prompt_text = "Create 3 enigmatic and inspiring messages of 15 to 25 words, like a fortune teller speaking. Return ONLY a JSON array of strings."
        fallback = "The energy of the universe conspires in your favor; listen to the whispers of fate that guide you."
    
    used_long_phrases = load_used_phrases(LONG_PHRASES_CACHE_FILE)
    phrases = _ask_json_list(prompt_text, temperature=1.1, tries=4)
    
    valid_phrases = [
        p for p in phrases if 15 <= _count_words_no_markup(p) <= 25 and _md5(p) not in used_long_phrases
    ]
    
    if valid_phrases:
        chosen = random.choice(valid_phrases)
        used_long_phrases.add(_md5(chosen))
        save_used_phrases(used_long_phrases, LONG_PHRASES_CACHE_FILE)
        logger.info("Frase longa de tarot inédita selecionada para narração.")
        return _clean_line(chosen)

    logger.warning("Nenhuma frase longa de tarot inédita foi gerada, usando fallback.")
    return fallback