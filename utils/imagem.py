# utils/imagem.py
import os
import re
import json
import random
import logging
from typing import List, Optional, Tuple, Iterable
from typing import Optional

import requests
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except Exception:
    from requests.adapters import HTTPAdapter
    Retry = None

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps
from dotenv import load_dotenv

# >>> cache por idioma
try:
    from cache_store import cache, _norm_lang as _norm_lang_cache
except Exception:
    cache = None
    def _norm_lang_cache(x: Optional[str]) -> str:
        s = (x or "").lower()
        if s.startswith("pt"): return "pt"
        if s.startswith("ar"): return "ar"
        return "en"

try:
    from utils.chatgpt_image import gerar_imagem_chatgpt
    _HAVE_CHATGPT_AUTOMATION = True
except ImportError:
    _HAVE_CHATGPT_AUTOMATION = False

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------#
# Helpers de Texto (mantidos e compatíveis com seu fluxo)
# -----------------------------------------------------------------------------#
IMAGE_EMPHASIS_ONLY_MARKUP = os.getenv("IMAGE_EMPHASIS_ONLY_MARKUP", "True").lower() in ("true", "1", "yes")
IMAGE_FORCE_EMPHASIS = os.getenv("IMAGE_FORCE_EMPHASIS", "False").lower() in ("true", "1", "yes")
IMAGE_EMPHASIS_LAST_WORDS = int(os.getenv("IMAGE_EMPHASIS_LAST_WORDS", "1"))
_PUNCH_WORDS = {"vocę","voce","vida","fé","fe","deus","foco","força","forca","coragem","propósito","proposito","sucesso","sonho","agora","hoje","mais","nunca","sempre"}

def _idioma_norm(idioma: Optional[str]) -> str:
    s = (idioma or "pt").lower()
    if s.startswith("ar"): return "ar"
    if s.startswith("pt"): return "pt"
    if s.startswith("ru"): return "ru"
    return "en"

def _parse_highlights_from_markdown(s: str) -> Tuple[str, List[str]]:
    segs = re.findall(r"\*\*(.+?)\*\*", s, flags=re.S)
    clean = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    words: List[str] = [tok.strip().lower() for seg in segs for tok in re.split(r"[^\wÄ-Öò-öø-ÿ]+", seg) if tok.strip()]
    return clean, words

def _pick_highlights(line: str) -> List[str]:
    words = [re.sub(r"[^\wÄ-Öò-öø-ÿ]", "", w).lower() for w in line.split()]
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
    parts = [p.strip() for p in re.split(r"(?:\.\.\.|[.!?:;\u2012\u2013-])", s) if p and p.strip()]
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
        toks = [re.sub(r"[^\wÄ-Öò-öø-ÿ]", "", w).lower() for w in punch.split() if w.strip()]
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
# Config do módulo / ENV
# -----------------------------------------------------------------------------#
IMAGE_MODE = os.getenv("IMAGE_MODE", "pexels").lower()
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")

CACHE_DIR = os.getenv("CACHE_DIR", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

FONTS_DIR = "fonts"
IMAGENS_DIR = os.getenv("IMAGES_DIR", "imagens")
os.makedirs(IMAGENS_DIR, exist_ok=True)

USER_AGENT = "TiktokMotivacional/1.0 (+https://local)"

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default

def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, str(default))).strip())
    except Exception:
        return default

def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip().lower()
    if v in ("1", "true", "yes", "on"): return True
    if v in ("0", "false", "no", "off"): return False
    return default

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

def _env_color(name: str, default: str) -> Tuple[int, int, int]:
    s = (os.getenv(name, default) or "").strip()
    if s.startswith("#") and len(s) == 7:
        return tuple(int(s[i:i+2], 16) for i in (1, 3, 5))
    try:
        r, g, b = [int(x) for x in s.split(",")]
        return (r, g, b)
    except Exception:
        return (243, 179, 74)

IMAGE_DARK_ENABLE = _env_bool("IMAGE_DARK_ENABLE", True)
IMAGE_DARK_MODERN  = _clamp(_env_float("IMAGE_DARK_MODERN", 0.14), 0.0, 0.95)
IMAGE_DARK_CLASSIC = _clamp(_env_float("IMAGE_DARK_CLASSIC", 0.16), 0.0, 0.95)
IMAGE_DARK_MINIMAL = _clamp(_env_float("IMAGE_DARK_MINIMAL", 0.15), 0.0, 0.95)
IMAGE_VIGNETTE_STRENGTH   = _clamp(_env_float("IMAGE_VIGNETTE_STRENGTH", 0.06), 0.0, 0.95)
IMAGE_VIGNETTE_SOFTNESS   = _clamp(_env_float("IMAGE_VIGNETTE_SOFTNESS", 0.72), 0.0, 1.0)
IMAGE_DARK_CENTER_PROTECT = _clamp(_env_float("IMAGE_DARK_CENTER_PROTECT", 0.80), 0.0, 1.0)
IMAGE_TEXT_SCALE   = _clamp(_env_float("IMAGE_TEXT_SCALE", 1.20), 0.5, 2.0)
IMAGE_TEXT_UPPER   = _env_bool("IMAGE_TEXT_UPPER", True)
IMAGE_HL_COLOR     = _env_color("IMAGE_HL_COLOR", "#F3B34A")
IMAGE_HL_STROKE    = _env_bool("IMAGE_HL_STROKE", True)
IMAGE_TEXT_OUTLINE_STYLE = os.getenv("IMAGE_TEXT_OUTLINE_STYLE", "shadow").strip().lower()
IMAGE_STROKE_WIDTH       = _env_int("IMAGE_STROKE_WIDTH", 2)
try:
    _dx, _dy = [int(v) for v in (os.getenv("IMAGE_SHADOW_OFFSET", "2,3").split(","))]
    IMAGE_SHADOW_OFFSET = (_dx, _dy)
except Exception:
    IMAGE_SHADOW_OFFSET = (2, 3)
IMAGE_SHADOW_ALPHA = _env_int("IMAGE_SHADOW_ALPHA", 170)
IMAGE_TEXT_SCALE_CLASSIC = _clamp(_env_float("IMAGE_TEXT_SCALE_CLASSIC", 1.0), 0.5, 2.0)
IMAGE_TEXT_SCALE_MODERN  = _clamp(_env_float("IMAGE_TEXT_SCALE_MODERN",  1.0), 0.5, 2.0)
IMAGE_TEXT_SCALE_MINIMAL = _clamp(_env_float("IMAGE_TEXT_SCALE_MINIMAL", 1.0), 0.5, 2.0)
IMAGE_LOG_PROXY   = _env_bool("IMAGE_LOG_PROXY", False)
IMAGE_VERBOSE_LOG = _env_bool("IMAGE_VERBOSE_LOG", False)
MEDIA_PROXY_MODE = os.getenv("MEDIA_PROXY_MODE", "never").strip().lower()
PROXY_AUTO_BY_LANG = os.getenv("PROXY_AUTO_BY_LANG", "1").strip().lower() not in ("0", "false", "no")
DEFAULT_PROXY_REGION = (os.getenv("DEFAULT_PROXY_REGION", "") or "").upper() or None

# -----------------------------------------------------------------------------#
# Sessões HTTP + Proxy
# -----------------------------------------------------------------------------#
def _proxy_url_from_env(prefix: str) -> Optional[str]:
    host = os.getenv(f"{prefix}_HOST", "").strip()
    port = os.getenv(f"{prefix}_PORT", "").strip()
    user = os.getenv(f"{prefix}_USER", "").strip()
    pw   = os.getenv(f"{prefix}_PASS", "").strip()
    if not host or not port:
        return None
    auth = f"{user}:{pw}@" if (user or pw) else ""
    return f"http://{auth}{host}:{port}"

def _pick_proxy_region(explicit_region: Optional[str], idioma: Optional[str]) -> Optional[str]:
    if explicit_region:
        return explicit_region.upper()
    if PROXY_AUTO_BY_LANG and _idioma_norm(idioma) == "ar":
        return "EG"
    return DEFAULT_PROXY_REGION

_SESSIONS: dict[str, requests.Session] = {}

def _make_session(region: Optional[str] = None) -> requests.Session:
    s = requests.Session()
    if Retry is not None:
        retry = Retry(total=3, backoff_factor=0.7, status_forcelist=[429, 500, 502, 503, 504])
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.mount("http://", HTTPAdapter(max_retries=retry))
    else:
        s.mount("https://", HTTPAdapter())
        s.mount("http://", HTTPAdapter())

    prefix = ""
    if (region or "").upper() == "EG": prefix = "PROXY_EG"
    elif (region or "").upper() == "US": prefix = "PROXY_US"
    elif region: prefix = "PROXY"

    if prefix:
        url = _proxy_url_from_env(prefix)
        if url:
            s.proxies.update({"http": url, "https": url})
            if IMAGE_LOG_PROXY:
                logger.info("🌐 Proxy %s habilitado", (region or "DEFAULT"))
        else:
            logger.debug("🚫 Proxy desabilitado (configuração %s ausente)", region or "DEFAULT")

    s.headers.update({"User-Agent": USER_AGENT})
    return s

def _get_session(region: Optional[str]) -> requests.Session:
    key = (region or "DEFAULT").upper()
    if key not in _SESSIONS:
        _SESSIONS[key] = _make_session(region)
    return _SESSIONS[key]

def _get_media_session(idioma: Optional[str]) -> requests.Session:
    region_candidato = _pick_proxy_region(None, idioma)
    usar_proxy = MEDIA_PROXY_MODE == 'always' or (MEDIA_PROXY_MODE == 'auto' and region_candidato)
    return _get_session(region_candidato if usar_proxy else None)

# -----------------------------------------------------------------------------#
# Fontes: Suporte completo para árabe e cirílico (RU)
# -----------------------------------------------------------------------------#
ARABIC_FONT_IMAGE_REG  = os.getenv("ARABIC_FONT_IMAGE_REG",  "NotoNaskhArabic-Regular.ttf")
ARABIC_FONT_IMAGE_BOLD = os.getenv("ARABIC_FONT_IMAGE_BOLD", "NotoNaskhArabic-Bold.ttf")
CYRILLIC_FONT_IMAGE    = os.getenv("CYRILLIC_FONT_IMAGE",    "bebas-neue-cyrillic.ttf")
_FONT_CACHE: dict[Tuple[str, int], ImageFont.FreeTypeFont] = {}
_logged_fonts: set[Tuple[str, int]] = set()

def _font_for_lang(base_font: str, idioma: Optional[str], bold: bool = False) -> str:
    norm = _idioma_norm(idioma)
    if norm == "ar":
        return ARABIC_FONT_IMAGE_BOLD if bold else ARABIC_FONT_IMAGE_REG
    if norm == "ru":
        return CYRILLIC_FONT_IMAGE
    return base_font

def _find_first_existing(paths: List[str]) -> Optional[str]:
    for p in paths:
        if p and os.path.isfile(p):
            return p
    return None

def _system_font_candidates(names: List[str]) -> List[str]:
    base_candidates = []
    for n in names:
        base_candidates.extend([
            os.path.join(FONTS_DIR, n), f"/usr/share/fonts/truetype/noto/{n}",
            f"C:\\Windows\\Fonts\\{n}",
        ])
    return [os.path.abspath(p) for p in base_candidates]

def _abs_font_path(fname: str) -> str:
    return os.path.abspath(os.path.join(FONTS_DIR, fname))

def _load_font(fname: str, size: int) -> ImageFont.FreeTypeFont:
    key = (fname, size)
    if key in _FONT_CACHE: return _FONT_CACHE[key]
    primary = _abs_font_path(fname)
    candidates = [primary] + _system_font_candidates([fname, "arial.ttf", "NotoSans-Regular.ttf"])
    chosen = _find_first_existing(candidates)
    try:
        if not chosen: raise FileNotFoundError(f"Fonte '{fname}' não encontrada.")
        font = ImageFont.truetype(chosen, size=size)
        if key not in _logged_fonts and IMAGE_VERBOSE_LOG:
            logger.info("🔤 Fonte carregada: %s (tam=%d)", chosen, size)
            _logged_fonts.add(key)
        _FONT_CACHE[key] = font
        return font
    except Exception as e:
        if IMAGE_VERBOSE_LOG: logger.warning("⚠️ Falha ao carregar fonte %s: %s; usando default.", fname, e)
        return ImageFont.load_default()

def _text_size(draw, text, font):
    b = draw.textbbox((0,0), text, font=font); return b[2]-b[0], b[3]-b[1]

def _wrap_words(draw, words, font, maxw):
    lines, cur = [], []
    for w in words:
        if not cur: cur = [w]; continue
        if draw.textbbox((0,0), " ".join(cur + [w]), font=font)[2] <= maxw: cur.append(w)
        else: lines.append(cur); cur = [w]
    if cur: lines.append(cur)
    return lines

def _best_font_and_wrap(draw, text, font_name, maxw, min_size, max_size, max_lines=3):
    words = text.split(); lo, hi = min_size, max_size
    best_font = _load_font(font_name, lo)
    best_lines = _wrap_words(draw, words, best_font, maxw)
    while lo <= hi:
        mid = (lo + hi) // 2; f = _load_font(font_name, mid); lines = _wrap_words(draw, words, f, maxw)
        if max(draw.textbbox((0,0), " ".join(ln), font=f)[2] for ln in lines) <= maxw and len(lines) <= max_lines:
            best_font, best_lines = f, lines; lo = mid + 2
        else:
            hi = mid - 2
    return best_font, [" ".join(l) for l in best_lines]

def _draw_text_with_stroke(draw, xy, text, font, fill, stroke_fill, stroke_w):
    x, y = xy
    for dx, dy in [(-stroke_w,0),(stroke_w,0),(0,-stroke_w),(0,stroke_w),
                   (-stroke_w,-stroke_w),(-stroke_w,stroke_w),(stroke_w,-stroke_w),(stroke_w,stroke_w)]:
        if stroke_w > 0:
            draw.text((x+dx, y+dy), text, font=font, fill=stroke_fill)
    draw.text((x, y), text, font=font, fill=fill)

# ==================== RTL-aware: desenho token a token =======================
def _draw_line_colored(
    draw, x, y, line_text, font, highlight_set,
    fill="white", hl_fill=(243, 179, 74),
    style=None, stroke_w=None, rtl: bool=False, right_edge: Optional[int]=None,
):
    style = style or IMAGE_TEXT_OUTLINE_STYLE
    stroke_w = stroke_w or max(1, IMAGE_STROKE_WIDTH)
    tokens = line_text.split(" ")
    space_w = draw.textbbox((0, 0), " ", font=font)[2]

    if not rtl:
        cur_x = x; it = tokens
    else:
        cur_x = right_edge if right_edge is not None else x
        it = reversed(tokens)

    for raw in it:
        key = re.sub(r"[^\wÄ-Öò-öø-ÿ]", "", raw).lower()
        color = hl_fill if key in highlight_set else fill
        token_w = draw.textbbox((0, 0), raw, font=font)[2]
        draw_x = cur_x if not rtl else (cur_x - token_w)

        if style == "stroke" and stroke_w > 0:
            _draw_text_with_stroke(draw, (draw_x, y), raw, font, color, (0,0,0,200), stroke_w)
        elif style == "shadow":
            sx, sy = IMAGE_SHADOW_OFFSET
            shadow_col = (0, 0, 0, IMAGE_SHADOW_ALPHA)
            draw.text((draw_x + sx, y + sy), raw, font=font, fill=shadow_col)
            draw.text((draw_x, y), raw, font=font, fill=color)
        else:
            draw.text((draw_x, y), raw, font=font, fill=color)

        if not rtl:
            cur_x += token_w + space_w
        else:
            cur_x -= token_w + space_w

# -----------------------------------------------------------------------------#
# Pós-processamento de imagem / ajuste de fundo
# -----------------------------------------------------------------------------#
def _darken_and_vignette(img, base_dark_alpha, vig, softness, center_protect) -> Image.Image:
    w, h = img.size; out = img.convert("RGBA")
    if base_dark_alpha > 0:
        out = Image.alpha_composite(out, Image.new("RGBA", (w, h), (0, 0, 0, int(base_dark_alpha))))
    if vig > 0:
        mask = Image.new("L", (w, h), 255); d = ImageDraw.Draw(mask)
        inner_scale = 0.72 + 0.16 * max(0,min(1,softness)) + 0.12 * max(0,min(1,center_protect))
        iw, ih = int(w*inner_scale), int(h*inner_scale); left, top = (w-iw)//2, (h-ih)//2
        d.ellipse((left, top, left+iw, top+ih), fill=0)
        blur = max(6, int(min(w,h) * (0.06 + 0.16*max(0,min(1,softness)))))
        mask = mask.filter(ImageFilter.GaussianBlur(radius=blur))
        overlay = Image.new("RGBA", (w,h), (0,0,0,0)); overlay.putalpha(mask.point(lambda v: int(v*vig)))
        out = Image.alpha_composite(out, overlay)
    return out.convert("RGB")

def _ensure_1080x1920(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img).convert("RGB")
    tw, th = 1080, 1920; iw, ih = img.size
    scale = max(tw/iw, th/ih); new = img.resize((int(iw*scale), int(ih*scale)), Image.LANCZOS)
    left, top = (new.size[0]-tw)//2, (new.size[1]-th)//2
    return new.crop((left, top, left+tw, top+th))

# -----------------------------------------------------------------------------#
# Geração de imagens (Pexels / ChatGPT) + registro em cache por idioma
# -----------------------------------------------------------------------------#
def gerar_imagem_dalle(prompt: str, arquivo_saida: str, *, idioma: Optional[str] = None):
    if not _HAVE_CHATGPT_AUTOMATION:
        raise ImportError("'gerar_imagem_chatgpt' năo importada.")
    cookies_filename = os.getenv("COOKIES_CHATGPT_FILENAME", "cookies_chatgpt.txt")
    if not gerar_imagem_chatgpt(prompt=prompt, cookies_path=cookies_filename, arquivo_saida=arquivo_saida):
        raise RuntimeError("Automaçăo do ChatGPT falhou ao gerar imagem.")
    logger.info("🎨 Imagem DALL-E/ChatGPT gerada via automaçăo.")
    # cache de prompts/imagens
    if cache: 
        cache.add("used_pexels_prompts", prompt, lang=_norm_lang_cache(idioma))
        cache.add("used_images", arquivo_saida, lang=_norm_lang_cache(idioma))

def gerar_imagem_com_frase(prompt: str, arquivo_saida: str, *, idioma: Optional[str] = None, max_retries: int = 3):
    os.makedirs(os.path.dirname(arquivo_saida) or ".", exist_ok=True)
    session = _get_media_session(idioma)
    idioma_norm = _norm_lang_cache(idioma)

    for attempt in range(1, max_retries + 1):
        try:
            if IMAGE_MODE == "chatgpt":
                gerar_imagem_dalle(prompt, arquivo_saida, idioma=idioma)
                if not os.path.exists(arquivo_saida): 
                    raise RuntimeError("Arquivo não criado pelo ChatGPT.")
                # cache
                if cache:
                    cache.add("used_pexels_prompts", prompt, lang=idioma_norm)
                    cache.add("used_images", arquivo_saida, lang=idioma_norm)
                return

            # Pexels
            if not PEXELS_API_KEY: raise RuntimeError("PEXELS_API_KEY ausente.")
            headers = {"Authorization": PEXELS_API_KEY}
            params = {
                "query": prompt, "orientation": "portrait", "size": "large",
                "per_page": 30, "page": random.randint(1,10)
            }
            r = session.get("https://api.pexels.com/v1/search", headers=headers, params=params, timeout=15)
            r.raise_for_status()
            photos = r.json().get("photos", [])
            if not photos: raise RuntimeError(f"Pexels sem fotos para: '{prompt}'")

            choice = random.choice(photos)
            src = choice.get("src", {})
            url = src.get("large2x") or src.get("large") or src.get("portrait")
            raw_resp = session.get(url, timeout=20); raw_resp.raise_for_status()
            with open(arquivo_saida, "wb") as f:
                f.write(raw_resp.content)

            img = _ensure_1080x1920(Image.open(arquivo_saida))
            img.save(arquivo_saida, quality=92)

            logger.info("🖼️ Imagem salva: %s", arquivo_saida)
            # cache
            if cache:
                cache.add("used_pexels_prompts", prompt, lang=idioma_norm)
                cache.add("used_images", arquivo_saida, lang=idioma_norm)
            return
        except Exception as e:
            logger.warning("⚠️ Falha na geração (tentativa %d/%d): %s", attempt, max_retries, e)
    logger.error("❌ Não conseguiu gerar nova imagem após %d tentativas.", max_retries)

# -----------------------------------------------------------------------------#
# Renderização do título na imagem (templates + RTL)
# -----------------------------------------------------------------------------#
def _prepare_bg(img, base_dark, _template):
    base = _ensure_1080x1920(img)
    if IMAGE_DARK_ENABLE:
        return _darken_and_vignette(base, int(255*base_dark), IMAGE_VIGNETTE_STRENGTH, IMAGE_VIGNETTE_SOFTNESS, IMAGE_DARK_CENTER_PROTECT)
    return base

def _render_modern_block(img, frase, *, idioma=None):
    img = _prepare_bg(img, IMAGE_DARK_MODERN, "modern_block").convert("RGBA"); W, H = img.size; draw = ImageDraw.Draw(img)
    intro, punch, hl_words = _split_for_emphasis(frase)
    is_ar = _idioma_norm(idioma) == "ar"
    if IMAGE_TEXT_UPPER and not is_ar: intro, punch = intro.upper(), punch.upper()

    base_scale = (W / 1080.0) * IMAGE_TEXT_SCALE * IMAGE_TEXT_SCALE_MODERN
    margin = int(W*0.08); maxw = W - 2*margin; y = int(H*0.14)
    right_edge = W - margin  # borda direita para RTL

    if intro:
        f_small, lines1 = _best_font_and_wrap(
            draw, intro, _font_for_lang("Montserrat-Regular.ttf", idioma, False),
            maxw, int(38*base_scale), int(70*base_scale), 2
        )
        for ln in lines1:
            _draw_line_colored(draw, margin, y, ln, f_small, set(), hl_fill=IMAGE_HL_COLOR,
                               rtl=is_ar, right_edge=right_edge)
            y += int(f_small.size*1.16)
        y += int(H*0.018)

    f_main, lines2 = _best_font_and_wrap(
        draw, punch, _font_for_lang("Montserrat-ExtraBold.ttf", idioma, True),
        maxw, int(68*base_scale), int(104*base_scale), 4
    )
    for ln in lines2:
        _draw_line_colored(draw, margin, y, ln, f_main, set(hl_words), hl_fill=IMAGE_HL_COLOR,
                           rtl=is_ar, right_edge=right_edge)
        y += int(f_main.size*1.10)
    return img.convert("RGB")

def _render_classic_serif(img, frase, *, idioma=None):
    clean, explicit = _parse_highlights_from_markdown(frase.strip()); hl_set = set(explicit)
    img = _prepare_bg(img, IMAGE_DARK_CLASSIC, "classic_serif").convert("RGBA"); W, H = img.size; draw = ImageDraw.Draw(img)
    is_ar = _idioma_norm(idioma) == "ar"; text = clean.upper() if IMAGE_TEXT_UPPER and not is_ar else clean
    margin = int(W*0.12); maxw = W - 2*margin
    right_edge = W - margin
    scls = IMAGE_TEXT_SCALE * IMAGE_TEXT_SCALE_CLASSIC
    f_serif, lines = _best_font_and_wrap(
        draw, text, _font_for_lang("PlayfairDisplay-Bold.ttf", idioma, True),
        maxw, int(54*scls), int(80*scls), 4
    )
    y = int(H*0.20)
    for ln in lines:
        _draw_line_colored(draw, margin, y, ln, f_serif, hl_set, hl_fill=IMAGE_HL_COLOR,
                           rtl=is_ar, right_edge=right_edge)
        y += int(f_serif.size*1.18)
    return img.convert("RGB")

def _render_minimal_center(img, frase, *, idioma=None):
    img = _prepare_bg(img, IMAGE_DARK_MINIMAL, "minimal_center").convert("RGBA"); W, H = img.size; draw = ImageDraw.Draw(img)
    is_ar = _idioma_norm(idioma) == "ar"; scls = IMAGE_TEXT_SCALE * IMAGE_TEXT_SCALE_MINIMAL
    big_name, small_name = _font_for_lang("BebasNeue-Regular.ttf", idioma, True), _font_for_lang("Montserrat-Regular.ttf", idioma, False)
    clean, _ = _parse_highlights_from_markdown(frase); two = quebrar_em_duas_linhas(clean)
    if IMAGE_TEXT_UPPER and not is_ar: two = two.upper()
    parts = two.split("\n"); l1 = parts[0].strip(); l2 = " ".join(parts[1:]).strip() if len(parts) > 1 else ""

    small = _load_font(small_name, max(36, int(W*0.039*scls)))
    big   = _load_font(big_name,   max(90, int(W*0.102*scls)))

    margin = int(W*0.08)
    right_edge = W - margin

    # linha 1 (menor)
    tw1, th1 = _text_size(draw, l1, small)
    x1 = (W - tw1)//2 if not is_ar else (right_edge - tw1)
    y = int(H*0.20)
    _draw_text_with_stroke(draw, (x1, y), l1, small, "white", "black", max(1, int(small.size*0.05)))
    y += th1 + int(H*0.02)

    # linhas grandes (quebra e centraliza / alinha à direita)
    maxw = int(W * 0.92)
    for ln_words in _wrap_words(draw, l2.split(), big, maxw):
        ln = " ".join(ln_words)
        tw, th = _text_size(draw, ln, big)
        x = (W - tw)//2 if not is_ar else (right_edge - tw)
        _draw_text_with_stroke(draw, (x, y), ln, big, "white", "black", max(2, int(big.size*0.05)))
        y += th + int(big.size*0.06)
    return img.convert("RGB")

def escrever_frase_na_imagem(
    imagem_path,
    *args,
    idioma: str = "pt-br",
    template: str = "auto",
    frase: str | None = None,
    saida_path: str | None = None,
    slide_index: int | None = None,      # aceitos mas não usados aqui (compat)
    total_slides: int | None = None,     # aceitos mas não usados aqui (compat)
    **_ignored,
):
    """
    Função robusta e retrocompatível.

    Aceita:
      - escrever_frase_na_imagem(img, frase, saida_path, ...)
      - escrever_frase_na_imagem(img, saida_path, frase, ...)
      - escrever_frase_na_imagem(img, frase=..., saida_path=..., ...)
      - kwargs extras como slide_index/total_slides (ignorados aqui).
    """
    import os
    from PIL import Image

    # ---------- Resolver parâmetros vindos por *args ----------
    a1 = args[0] if len(args) >= 1 else None
    a2 = args[1] if len(args) >= 2 else None

    def _looks_like_path(s: object) -> bool:
        if not isinstance(s, str):
            return False
        ext = os.path.splitext(s)[1].lower()
        return ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp")

    # Se vieram apenas posicionais, deduzir ordem automaticamente
    if frase is None or saida_path is None:
        if a1 is not None and a2 is not None:
            # Tentar detectar qual argumento é o caminho de saída
            if _looks_like_path(a1) and not _looks_like_path(a2):
                # ordem: (imagem, saida_path, frase)
                saida_path = a1 if saida_path is None else saida_path
                frase = a2 if frase is None else frase
            elif _looks_like_path(a2) and not _looks_like_path(a1):
                # ordem: (imagem, frase, saida_path)
                frase = a1 if frase is None else frase
                saida_path = a2 if saida_path is None else saida_path
            else:
                # Não deu pra distinguir? assuma (frase, saida_path)
                frase = a1 if frase is None else frase
                saida_path = a2 if saida_path is None else saida_path
        elif a1 is not None:
            # 1 arg só: se parece caminho, é saida; senão, é frase
            if _looks_like_path(a1) and saida_path is None:
                saida_path = a1
            elif frase is None:
                frase = a1

    # Validação final
    if not frase or not saida_path:
        raise TypeError("escrever_frase_na_imagem requer 'frase' e 'saida_path' (posicionais ou kwargs).")

    # ---------- Corpo original ----------
    img = Image.open(imagem_path)
    if template == "auto":
        template = random.choice(["classic_serif", "modern_block", "minimal_center"])

    if template == "classic_serif":
        out = _render_classic_serif(img, frase, idioma=idioma)
    elif template == "minimal_center":
        out = _render_minimal_center(img, frase, idioma=idioma)
    else:
        out = _render_modern_block(img, frase, idioma=idioma)

    os.makedirs(os.path.dirname(saida_path) or ".", exist_ok=True)
    out.save(saida_path, quality=92)
    logger.info("🖼️ Frase renderizada em: %s", saida_path)
