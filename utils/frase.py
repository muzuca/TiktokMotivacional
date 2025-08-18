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


def load_used_phrases() -> Set[str]:
    if os.path.exists(PHRASES_CACHE_FILE):
        try:
            with open(PHRASES_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return set(data)
        except Exception:
            pass
    return set()


def save_used_phrases(used_phrases: Set[str]) -> None:
    try:
        with open(PHRASES_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(list(used_phrases)), f, ensure_ascii=False)
    except Exception as e:
        logger.warning("Não foi possível salvar cache de frases: %s", e)

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
        except Exception:
            pass
        time.sleep(0.8 * (attempt + 1))
    return []

def _ensure_single_emphasis(text: str, lang: str, prefer_last_n: int = 2) -> str:
    """
    Garante UM único **destaque** de 1–2 palavras.
    Se não houver, marca automaticamente últimas 1–2 palavras de conteúdo.
    Se houver muitos, remove todos e aplica só um no final.
    """
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

# -----------------------------------------------------------------------------#
# Geradores (Motivacional – existentes)
# -----------------------------------------------------------------------------#
def gerar_prompt_paisagem(idioma: str = "en") -> str:
    if not os.getenv("GEMINI_API_KEY"):
        logger.error("❌ GEMINI_API_KEY não configurada.")
        return "Mountains at sunrise" if _lang_tag(idioma) == "en" else "Montanhas ao nascer do sol"

    used = load_used_phrases()
    lang = _lang_tag(idioma)

    for attempt in range(3):
        try:
            logger.info("Gerando prompt de paisagem (%s). Tentativa %d/3", lang, attempt+1)
            prompt_text = (
                "Create 14 short descriptions of beautiful landscapes. "
                "Each line ≤ 7 words. Return ONLY a JSON array of strings."
                if lang == "en" else
                "Crie 14 descrições curtas de paisagens bonitas. "
                "Cada linha ≤ 7 palavras. Retorne APENAS um array JSON de strings."
            )
            descricoes = _ask_json_list(prompt_text, temperature=0.9, tries=3)
            descricoes = [d for d in descricoes if len(d.split()) <= 7]
            novas = [d for d in descricoes if _md5(d) not in used]
            if novas:
                escolha = random.choice(novas)
                used.add(_md5(escolha)); save_used_phrases(used)
                logger.info("📷 Prompt gerado para imagem: %s", escolha)
                return escolha
            time.sleep(0.6 * (attempt + 1))
        except Exception as e:
            logger.warning("Falha ao gerar prompt: %s", e)
            time.sleep(0.6 * (attempt + 1))

    return "Mountains at sunrise" if lang == "en" else "Montanhas ao nascer do sol"


def gerar_frase_motivacional(idioma: str = "en") -> str:
    """
    Curta, porém um pouco maior (9–20 palavras), com 1 destaque **...**
    e uma pausa natural (vírgula/reticências/traço) perto do meio.
    """
    if not os.getenv("GEMINI_API_KEY"):
        logger.error("❌ GEMINI_API_KEY não configurada.")
        return "Você é mais forte do que imagina." if _lang_tag(idioma) == "pt" else "You are stronger than you think."

    used = load_used_phrases()
    lang = _lang_tag(idioma)

    prompt_text = (
        "Write 16 motivational short sentences in English. "
        "Each must have 9–20 words, natural and non-cliché. "
        "Include EXACTLY ONE emphasis span using **double asterisks** around 1–2 impactful words near the end. "
        "Optionally include a comma, dash (—) or ellipsis (…) to suggest a break. "
        "No hashtags or quotes. Return ONLY a JSON array of strings."
        if lang == "en" else
        "Escreva 16 frases motivacionais em português. "
        "Cada uma deve ter entre 9 e 20 palavras, naturais e sem clichês. "
        "Inclua EXATAMENTE UM destaque usando **duas-asteriscos** envolvendo 1–2 palavras marcantes, perto do final. "
        "Opcionalmente use uma vírgula, travessão (—) ou reticências (…) para sugerir pausa. "
        "Sem hashtags ou aspas. Retorne APENAS um array JSON de strings."
    )

    items = _ask_json_list(prompt_text, temperature=0.95, tries=3)

    pool: List[str] = []
    for s in items:
        s = _clean_line(s)
        s = _ensure_single_emphasis(s, lang, prefer_last_n=2)
        wc = _count_words_no_markup(s)
        if 9 <= wc <= 20:
            pool.append(s)

    if not pool:
        base = "A vida é curta demais — faça hoje o que aproxima dos seus sonhos" \
            if lang == "pt" else \
            "Life is short — do today what moves you closer to your dreams"
        pool = [_ensure_single_emphasis(base, lang)]

    random.shuffle(pool)
    for cand in pool:
        key = _md5(_strip_emph(cand))
        if key not in used:
            used.add(key); save_used_phrases(used)
            logger.info("🧠 Frase motivacional escolhida: %s", cand)
            return cand

    return pool[0]


# Longa (motivacional – mantida)
def _gen_call(prompt: str, generation_config: dict) -> Optional[str]:
    try:
        resp = model.generate_content(
            prompt,
            generation_config=generation_config,
            request_options={"timeout": GEMINI_TIMEOUT_SEC},
        )
        return getattr(resp, "text", None)
    except TypeError:
        try:
            resp = model.generate_content(prompt, generation_config=generation_config)
            return getattr(resp, "text", None)
        except Exception:
            return None
    except Exception:
        return None

def _ask_gemini_list(prompt: str, temperature: float = 1.05, tries: int = 3, sleep_base: float = 1.0) -> List[str]:
    for attempt in range(tries):
        try:
            raw = _gen_call(prompt, {"response_mime_type":"application/json","temperature":temperature,"top_p":0.95}) or ""
            lst = []
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    lst = [ _clean_line(x) for x in data if isinstance(x, str) and _clean_line(x)]
            except Exception:
                pass
            if lst:
                return lst
            txt = _gen_call(prompt, {"temperature":temperature,"top_p":0.95}) or ""
            lines = [_clean_line(l) for l in txt.split("\n") if _clean_line(l)]
            if lines:
                return lines
        except Exception:
            pass
        time.sleep(sleep_base*(2**attempt))
    return []

def _collect_long_candidates(lang: str, target_total: int = 12, batch_size: int = 4, max_calls: int = 4) -> List[str]:
    prompts = {
        "en": ("Write {n} distinct motivational mini-speeches in English. "
               "Each MUST have 40–55 words (~20 seconds narration). "
               "Avoid clichés and previous phrasing; vary imagery, rhythm, and structure. "
               "Return ONLY a JSON array of strings."),
        "pt": ("Escreva {n} mini-discursos motivacionais em português. "
               "Cada um DEVE ter entre 40 e 55 palavras (~20 segundos de narração). "
               "Evite clichês e repetições; varie imagens, ritmo e estrutura. "
               "Retorne APENAS um array JSON de strings.")
    }
    prompt_tpl = prompts["en" if lang == "en" else "pt"]
    all_items: List[str] = []
    calls = min(max_calls, max(1, (target_total + batch_size - 1) // batch_size))
    for _ in range(calls):
        items = _ask_gemini_list(prompt_tpl.format(n=batch_size), temperature=1.05, tries=3)
        for it in items:
            itc = _clean_line(it)
            if itc and itc not in all_items:
                all_items.append(itc)
        if len(all_items) >= target_total:
            break
    return all_items

def _pick_new(pool: List[str], used: Set[str], prefix: str, min_words: int, max_words: int) -> Optional[str]:
    cand = [p for p in pool if min_words <= len(p.split()) <= max_words]
    random.shuffle(cand)
    for c in cand:
        key = f"{prefix}{_md5(c)}"
        if key not in used:
            return c
    return None

def gerar_frase_motivacional_longa(idioma: str = "en") -> str:
    if not os.getenv("GEMINI_API_KEY"):
        logger.error("❌ GEMINI_API_KEY não configurada.")
        return ("Você é mais forte do que imagina. Respire fundo e siga em frente todos os dias, superando desafios para realizar seus sonhos."
                if _lang_tag(idioma) == "pt"
                else "You are stronger than you think. Take a deep breath and push forward every day, overcoming challenges to achieve your dreams.")

    used = load_used_phrases()
    prefix = "LONG::"
    lang = _lang_tag(idioma)
    if lang == "ar":
        return "استقبل رسائل الكون بقلب هادئ. تذكّر: كل اختيار اليوم يغيّر مصيرك. ثق بحدسك وامضِ بخطوات واثقة."

    for round_idx in range(1, 4):
        try:
            logger.info("Gerando frases motivacionais longas (%s). Rodada %d/3", "EN" if lang == "en" else "PT-BR", round_idx)
            candidates = _collect_long_candidates(lang=lang, target_total=12, batch_size=4, max_calls=4)
            candidates = [_clean_line(c) for c in candidates if _clean_line(c)]
            ideal = [c for c in candidates if 40 <= len(c.split()) <= 55]
            tolerant = [c for c in candidates if 35 <= len(c.split()) <= 65]

            def pick(pool: List[str]) -> Optional[str]:
                return _pick_new(pool, used, prefix=prefix, min_words=35, max_words=65)

            chosen = pick(ideal) or pick(tolerant)
            if not chosen:
                extra = _collect_long_candidates(lang=lang, target_total=4, batch_size=4, max_calls=1)
                extra = [_clean_line(c) for c in extra if _clean_line(c)]
                extra_ideal = [c for c in extra if 40 <= len(c.split()) <= 55]
                extra_tol = [c for c in extra if 35 <= len(c.split()) <= 65]
                chosen = pick(extra_ideal) or pick(extra_tol)

            if chosen:
                used.add(f"{prefix}{_md5(chosen)}"); save_used_phrases(used)
                logger.info("🧠 Frase motivacional longa escolhida: %s", chosen[:100] + "..." if len(chosen) > 100 else chosen)
                return chosen

            time.sleep(1.2 * round_idx)
        except Exception as e:
            logger.warning("Rodada %d falhou: %s", round_idx, e)
            time.sleep(1.2 * round_idx)

    return ("Você é mais forte do que imagina. Respire fundo e siga em frente todos os dias, superando desafios para realizar seus sonhos."
            if lang == "pt"
            else "You are stronger than you think. Take a deep breath and push forward every day, overcoming challenges to achieve your dreams.")

# -----------------------------------------------------------------------------#
# Tarot (Fase 4)
# -----------------------------------------------------------------------------#
def gerar_prompt_tarot(idioma: str = "en") -> str:
    """
    Prompt curto para Pexels/planos de fundo (sempre em inglês p/ melhor match).
    Conteúdo: mesa de cartas, velas, atmosfera mística, vertical/cinematic.
    """
    if not os.getenv("GEMINI_API_KEY"):
        logger.warning("GEMINI_API_KEY ausente — usando prompt tarot padrão.")
        return "tarot table, candlelight, mystic fortune teller hands shuffling cards, purple velvet, cinematic, vertical"

    used = load_used_phrases()
    prefix = "PROMPT_TAROT::"

    prompt_text = (
        "Create 16 short visual queries for stock images about tarot/fortune-telling. "
        "Each query <= 12 words, English, concrete nouns/visual cues only, vertical/cinematic vibe. "
        "Return ONLY a JSON array of strings."
    )
    items = _ask_json_list(prompt_text, temperature=0.9, tries=3)
    items = [i for i in items if len(i.split()) <= 12]

    random.shuffle(items)
    for it in items:
        key = prefix + _md5(it)
        if key not in used:
            used.add(key); save_used_phrases(used)
            logger.info("🔮 Prompt tarot gerado: %s", it)
            return it

    return "tarot table, candlelight, mystic fortune teller hands, purple velvet, cinematic, vertical"

def gerar_frase_tarot_curta(idioma: str = "en") -> str:
    """
    Frase curta para capa/descrição. Sem markdown, sem hashtags.
    """
    lang = _lang_tag(idioma)
    if not os.getenv("GEMINI_API_KEY"):
        return ("A carta certa aparece quando você está pronto." if lang == "pt"
                else ("البطاقة المناسبة تظهر حين يكون قلبك مستعدًا." if lang == "ar"
                else "The right card appears when your heart is ready."))

    used = load_used_phrases()
    prefix = "TAROT_SHORT::"

    if lang == "ar":
        prompt = (
            "اكتب 16 جملة قصيرة مستوحاة من قراءة التاروت باللهجة العربية المفهومة في مصر. "
            "كل جملة بين 6 و 14 كلمة، بلا علامات تنصيص ولا وسوم. "
            "تكون روحانية ولطيفة وتدعو للتأمل بلا وعود حتمية. "
            "أعد فقط مصفوفة JSON من السلاسل."
        )
    elif lang == "pt":
        prompt = (
            "Escreva 16 frases curtas inspiradas em leitura de tarô, em português do Brasil. "
            "Cada uma com 6–14 palavras, sem aspas e sem hashtags; tom místico, gentil, sem promessas determinísticas. "
            "Retorne APENAS um array JSON de strings."
        )
    else:
        prompt = (
            "Write 16 short tarot-inspired lines in English. "
            "Each 6–14 words, no quotes, no hashtags; mystical, gentle, non-deterministic. "
            "Return ONLY a JSON array of strings."
        )

    items = _ask_json_list(prompt, temperature=0.95, tries=3)
    random.shuffle(items)
    for it in items:
        key = prefix + _md5(it)
        if key not in used:
            used.add(key); save_used_phrases(used)
            logger.info("🃏 Frase tarot curta: %s", it)
            return it

    # fallbacks
    if lang == "ar":
        return "اسمع همس قلبك… الرسالة أوضح مما تظن."
    if lang == "pt":
        return "Ouça o sussurro do seu coração — a mensagem está mais perto do que imagina."
    return "Listen to the whisper within — your message is closer than you think."

def gerar_frase_tarot_longa(idioma: str = "en") -> str:
    """
    Narração ~12–20 s, tom místico e gentil. Sem markdown/hashtags.
    """
    lang = _lang_tag(idioma)
    if not os.getenv("GEMINI_API_KEY"):
        return ("Hoje, o tarô revela uma passagem de clareza: respire, acolha o que chega e libere o que pesa. "
                "Seu caminho brilha quando você honra sua intuição e dá um passo de cada vez.")
    used = load_used_phrases()
    prefix = "TAROT_LONG::"

    if lang == "ar":
        prompt = (
            "اكتب 8 مقاطع قصيرة مستوحاة من قراءة التاروت بالعربية المفهومة في مصر. "
            "طول كل مقطع 40–55 كلمة تقريبًا، نبرة روحانية ولطيفة وغير حتمية، بدون وعود مطلقة. "
            "بدون علامات تنصيص وبدون هاشتاجات. "
            "أعد فقط مصفوفة JSON من السلاسل."
        )
    elif lang == "pt":
        prompt = (
            "Escreva 8 textos curtos inspirados em leitura de tarô, em português do Brasil. "
            "Cada um com 40–55 palavras (~20s), tom místico, acolhedor e não determinístico, sem promessas absolutas. "
            "Sem aspas e sem hashtags. Retorne APENAS um array JSON de strings."
        )
    else:
        prompt = (
            "Write 8 short tarot-inspired narrations in English. "
            "Each 40–55 words (~20s), mystical, gentle, non-deterministic, no absolute promises. "
            "No quotes and no hashtags. Return ONLY a JSON array of strings."
        )

    items = _ask_json_list(prompt, temperature=1.02, tries=3)
    items = [_clean_line(x) for x in items if _clean_line(x)]
    random.shuffle(items)
    for it in items:
        wc = len(it.split())
        if 35 <= wc <= 65:
            key = prefix + _md5(it)
            if key not in used:
                used.add(key); save_used_phrases(used)
                logger.info("🔮 Frase tarot longa escolhida (%d palavras).", wc)
                return it

    # fallback simples
    if lang == "ar":
        return ("تنصحك الرسالة اليوم بالهدوء والإصغاء لنبض قلبك. ما تبحث عنه يقترب حين تتقبل إشارات الكون "
                "بدون استعجال. خطوة صغيرة بنية صادقة تغيّر المزاج والاتجاه. دع الضوء يرشدك، وستعرف متى تتحرك.")
    if lang == "pt":
        return ("A mensagem de hoje convida ao silêncio e à escuta da sua intuição. Quando você acolhe os sinais "
                "sem pressa, o caminho clareia. Um passo sincero muda o ritmo e a direção. Confie no brilho que já "
                "habita em você, e avance quando o coração disser sim.")
    return ("Today’s message invites quiet and listening. As you welcome subtle signs without haste, the path clears. "
            "A sincere small step shifts your rhythm and direction. Trust the light already within you, and move when your heart says yes.")

# -----------------------------------------------------------------------------#
# Utilidades
# -----------------------------------------------------------------------------#
def quebrar_em_duas_linhas(frase: str) -> str:
    palavras = frase.split()
    n = len(palavras)
    if n <= 4:
        return frase

    candidatos = range(2, n - 1)

    def comp_len(ws: List[str]) -> int:
        return sum(len(w) for w in ws) + max(0, len(ws) - 1)

    pequenos = {
        "e","ou","de","da","do","das","dos","em","no","na","nos","nas","por","pra","para",
        "o","a","os","as","um","uma","que","se","com"
    }
    pontos = {".", ",", ";", "!", "?", "—", "-", "–", ":"}

    melhor_div = None
    menor_dif = float('inf')

    for i in candidatos:
        linha1 = palavras[:i]
        linha2 = palavras[i:]
        len1 = comp_len(linha1)
        len2 = comp_len(linha2)
        dif = abs(len1 - len2)
        if linha1[-1].lower().strip("".join(pontos)) in pequenos: dif += 8
        if linha2[0].lower().strip("".join(pontos)) in pequenos:  dif += 10
        if any(linha1[-1].endswith(p) for p in pontos):           dif -= 5
        if dif < menor_dif:
            menor_dif = dif
            melhor_div = i

    if melhor_div is None:
        return frase
    return f'{" ".join(palavras[:melhor_div])}\n{" ".join(palavras[melhor_div:])}'

def gerar_slug(texto: str, limite: int = 30) -> str:
    try:
        base = (texto or "").strip()
        texto_ascii = unicodedata.normalize('NFD', base).encode('ascii', 'ignore').decode('utf-8')
        texto_ascii = re.sub(r'[^a-zA-Z0-9\s]', '', texto_ascii)
        texto_ascii = texto_ascii.strip().lower().replace(" ", "_")
        slug = texto_ascii[:limite]
        if not slug:
            slug = f"slug_{int(time.time())}"
        logger.info("🔗 Slug gerado: %s", slug)
        return slug
    except Exception as e:
        logger.error("Erro ao gerar slug: %s", e)
        return f"slug_{int(time.time())}"
