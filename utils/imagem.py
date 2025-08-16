# utils/imagem.py
import os
import re
import json
import random
import logging
from typing import List, Optional, Tuple, Iterable

import requests
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry  # type: ignore
except Exception:  # pragma: no cover
    from requests.adapters import HTTPAdapter  # type: ignore
    Retry = None  # type: ignore

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps
from dotenv import load_dotenv

from utils.frase import quebrar_em_duas_linhas

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

IMAGE_MODE = os.getenv("IMAGE_MODE", "pexels").lower()
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")

CACHE_DIR = "cache"
os.makedirs(CACHE_DIR, exist_ok=True)
IMAGES_CACHE_FILE = os.path.join(CACHE_DIR, "used_images.json")

FONTS_DIR = "fonts"
IMAGENS_DIR = "imagens"
os.makedirs(IMAGENS_DIR, exist_ok=True)

USER_AGENT = "TiktokMotivacional/1.0 (+https://local)"

# -------------------- ENV HELPERS --------------------
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
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default

def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))

def _env_color(name: str, default: str) -> Tuple[int, int, int]:
    s = (os.getenv(name, default) or "").strip()
    if s.startswith("#") and len(s) == 7:
        return tuple(int(s[i:i+2], 16) for i in (1, 3, 5))  # type: ignore
    try:
        r, g, b = [int(x) for x in s.split(",")]
        return (r, g, b)
    except Exception:
        return (243, 179, 74)

# -------------------- Parâmetros de imagem --------------------
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
IMAGE_FORCE_EMPHASIS = _env_bool("IMAGE_FORCE_EMPHASIS", False)
IMAGE_EMPHASIS_LAST_WORDS = max(1, _env_int("IMAGE_EMPHASIS_LAST_WORDS", 1))
IMAGE_EMPHASIS_ONLY_MARKUP = _env_bool("IMAGE_EMPHASIS_ONLY_MARKUP", True)

# Aparência do contorno
IMAGE_TEXT_OUTLINE_STYLE = os.getenv("IMAGE_TEXT_OUTLINE_STYLE", "shadow").strip().lower()
IMAGE_STROKE_WIDTH       = _env_int("IMAGE_STROKE_WIDTH", 2)
try:
    _dx, _dy = [int(v) for v in (os.getenv("IMAGE_SHADOW_OFFSET", "2,3").split(","))]
    IMAGE_SHADOW_OFFSET = (_dx, _dy)
except Exception:
    IMAGE_SHADOW_OFFSET = (2, 3)
IMAGE_SHADOW_ALPHA = _env_int("IMAGE_SHADOW_ALPHA", 170)

# Ajuste fino por template
IMAGE_TEXT_SCALE_CLASSIC = _clamp(_env_float("IMAGE_TEXT_SCALE_CLASSIC", 1.0), 0.5, 2.0)
IMAGE_TEXT_SCALE_MODERN  = _clamp(_env_float("IMAGE_TEXT_SCALE_MODERN",  1.0), 0.5, 2.0)
IMAGE_TEXT_SCALE_MINIMAL = _clamp(_env_float("IMAGE_TEXT_SCALE_MINIMAL", 1.0), 0.5, 2.0)

IMAGE_LOG_PROXY   = _env_bool("IMAGE_LOG_PROXY", False)
IMAGE_VERBOSE_LOG = _env_bool("IMAGE_VERBOSE_LOG", False)

# ----------------------------------------------------
def _make_session() -> requests.Session:
    s = requests.Session()
    if Retry is not None:
        retry = Retry(total=3, backoff_factor=0.7, status_forcelist=[429, 500, 502, 503, 504])
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.mount("http://", HTTPAdapter(max_retries=retry))
    else:
        s.mount("https://", HTTPAdapter())
        s.mount("http://", HTTPAdapter())

    ph = os.getenv("PROXY_HOST")
    if ph:
        user = os.getenv("PROXY_USER", "")
        pw = os.getenv("PROXY_PASS", "")
        port = os.getenv("PROXY_PORT", "")
        auth = f"{user}:{pw}@" if (user or pw) else ""
        url = f"http://{auth}{ph}:{port}"
        s.proxies.update({"http": url, "https": url})
        if IMAGE_LOG_PROXY:
            logger.info("🌐 Proxy habilitado (%s:%s) - auth=%s", ph, port or "-", "sim" if (user or pw) else "não")
        else:
            logger.debug("🌐 Proxy habilitado (detalhes ocultos).")
    else:
        logger.debug("🌐 Proxy desabilitado")

    s.headers.update({"User-Agent": USER_AGENT})
    return s

_SESSION = _make_session()

def load_used_images():
    if os.path.exists(IMAGES_CACHE_FILE):
        try:
            with open(IMAGES_CACHE_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_used_images(s):
    with open(IMAGES_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(list(s), f)

def _slug(s: str, maxlen: int = 28) -> str:
    import re as _re
    s = s.lower().strip()
    s = _re.sub(r"[^a-z0-9]+", "_", s)
    s = _re.sub(r"_+", "_", s).strip("_")
    return s[:maxlen] or "img"

# -------------------- Fontes / medidas --------------------
_FONT_CACHE: dict[Tuple[str, int], ImageFont.FreeTypeFont] = {}
_logged_fonts: set[Tuple[str, int]] = set()

def _load_font(fname: str, size: int) -> ImageFont.FreeTypeFont:
    key = (fname, size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    path = os.path.join(FONTS_DIR, fname)
    try:
        font = ImageFont.truetype(path, size=size)
        if key not in _logged_fonts and IMAGE_VERBOSE_LOG:
            logger.info("🔤 Fonte carregada: %s (tam=%d)", fname, size)
            _logged_fonts.add(key)
        _FONT_CACHE[key] = font
        return font
    except Exception:
        if key not in _logged_fonts and IMAGE_VERBOSE_LOG:
            logger.warning("⚠️  Fonte %s não encontrada; usando default.", fname)
            _logged_fonts.add(key)
        f = ImageFont.load_default()
        _FONT_CACHE[key] = f
        return f

def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> Tuple[int, int]:
    b = draw.textbbox((0, 0), text, font=font)
    return b[2] - b[0], b[3] - b[1]

# -------------------- Texto / wrapping --------------------
def _wrap_words(draw: ImageDraw.ImageDraw, words: List[str], font: ImageFont.FreeTypeFont, maxw: int) -> List[List[str]]:
    lines: List[List[str]] = []
    cur: List[str] = []
    def width_of(ws: Iterable[str]) -> int:
        t = " ".join(ws) if ws else ""
        return draw.textbbox((0,0), t, font=font)[2]
    for w in words:
        if not cur:
            cur = [w]; continue
        if width_of(cur + [w]) <= maxw:
            cur.append(w)
        else:
            lines.append(cur)
            cur = [w]
    if cur: lines.append(cur)
    return lines

def _best_font_and_wrap(draw, text, font_name, maxw, min_size, max_size, max_lines=3):
    words = text.split()
    lo, hi = min_size, max_size
    best_font = _load_font(font_name, lo)
    best_lines = _wrap_words(draw, words, best_font, maxw)
    while lo <= hi:
        mid = (lo + hi) // 2
        f = _load_font(font_name, mid)
        lines = _wrap_words(draw, words, f, maxw)
        w_ok = max(draw.textbbox((0,0), " ".join(ln), font=f)[2] for ln in lines) <= maxw
        if w_ok and len(lines) <= max_lines:
            best_font, best_lines = f, lines
            lo = mid + 2
        else:
            hi = mid - 2
    return best_font, [" ".join(l) for l in best_lines]

def _draw_text_with_stroke(draw, xy, text, font, fill, stroke_fill, stroke_w):
    x, y = xy
    if stroke_w > 0:
        for dx, dy in [(-stroke_w,0),(stroke_w,0),(0,-stroke_w),(0,stroke_w),
                       (-stroke_w,-stroke_w),(-stroke_w,stroke_w),(stroke_w,-stroke_w),(stroke_w,stroke_w)]:
            draw.text((x+dx, y+dy), text, font=font, fill=stroke_fill)
    draw.text((x, y), text, font=font, fill=fill)

def _draw_line_colored(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    line_text: str,
    font: ImageFont.FreeTypeFont,
    highlight_set: set,
    fill="white",
    hl_fill=(243, 179, 74),
    style: Optional[str] = None,
    stroke_w: Optional[int] = None,
):
    """Desenha token a token; highlight só nas palavras marcadas."""
    if style is None:
        style = IMAGE_TEXT_OUTLINE_STYLE
    if stroke_w is None:
        stroke_w = max(1, IMAGE_STROKE_WIDTH)

    tokens = line_text.split(" ")
    cur_x = x

    for i, raw in enumerate(tokens):
        key = re.sub(r"[^\wÀ-ÖØ-öø-ÿ]", "", raw).lower()
        color = hl_fill if key in highlight_set else fill

        if style == "stroke":
            if stroke_w > 0:
                for dx, dy in [(-stroke_w,0),(stroke_w,0),(0,-stroke_w),(0,stroke_w),
                               (-stroke_w,-stroke_w),(-stroke_w,stroke_w),
                               (stroke_w,-stroke_w),(stroke_w,stroke_w)]:
                    draw.text((cur_x+dx, y+dy), raw, font=font, fill=(0,0,0,200))
            draw.text((cur_x, y), raw, font=font, fill=color)
        elif style == "shadow":
            sx, sy = IMAGE_SHADOW_OFFSET
            shadow_col = (0, 0, 0, max(0, min(255, IMAGE_SHADOW_ALPHA)))
            draw.text((cur_x + sx, y + sy), raw, font=font, fill=shadow_col)
            draw.text((cur_x, y), raw, font=font, fill=color)
        else:
            draw.text((cur_x, y), raw, font=font, fill=color)

        w = draw.textbbox((0,0), raw + (" " if i < len(tokens)-1 else ""), font=font)[2]
        cur_x += w

# -------------------- Efeitos de fundo --------------------
def _darken_and_vignette(img: Image.Image, base_dark_alpha: int, vig: float, softness: float, center_protect: float) -> Image.Image:
    w, h = img.size
    out = img.convert("RGBA")

    if base_dark_alpha > 0:
        out = Image.alpha_composite(out, Image.new("RGBA", (w, h), (0, 0, 0, int(base_dark_alpha))))

    if vig > 0:
        mask = Image.new("L", (w, h), 255)
        d = ImageDraw.Draw(mask)
        inner_scale = 0.72 + 0.16 * _clamp(softness, 0.0, 1.0) + 0.12 * _clamp(center_protect, 0.0, 1.0)
        inner_scale = _clamp(inner_scale, 0.70, 0.97)
        inner_w = int(w * inner_scale)
        inner_h = int(h * inner_scale)
        left = (w - inner_w) // 2
        top  = (h - inner_h) // 2
        d.ellipse((left, top, left + inner_w, top + inner_h), fill=0)

        blur = max(6, int(min(w, h) * (0.06 + 0.16 * _clamp(softness, 0.0, 1.0))))
        mask = mask.filter(ImageFilter.GaussianBlur(radius=blur))

        scaled_alpha = mask.point(lambda v: int(v * _clamp(vig, 0.0, 1.0)))
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        overlay.putalpha(scaled_alpha)
        out = Image.alpha_composite(out, overlay)

    return out.convert("RGB")

def _ensure_1080x1920(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img).convert("RGB")
    tw, th = 1080, 1920
    iw, ih = img.size
    scale = max(tw/iw, th/ih)
    new = img.resize((int(iw*scale), int(ih*scale)), Image.LANCZOS)
    nx, ny = new.size
    left = (nx - tw)//2
    top  = (ny - th)//2
    return new.crop((left, top, left+tw, top+th))

# -------------------- Download / geração --------------------
def gerar_imagem_com_frase(prompt: str, arquivo_saida: str, *, max_retries: int = 3):
    os.makedirs(os.path.dirname(arquivo_saida) or ".", exist_ok=True)
    used = load_used_images()

    for attempt in range(1, max_retries + 1):
        try:
            if IMAGE_MODE != "pexels":
                raise RuntimeError("Somente IMAGE_MODE=pexels implementado.")
            if not PEXELS_API_KEY:
                raise RuntimeError("PEXELS_API_KEY ausente.")
            headers = {"Authorization": PEXELS_API_KEY}
            page = random.randint(1, 10)
            params = {"query": prompt, "orientation": "portrait", "size": "large", "per_page": 30, "page": page}
            logger.debug("📸 Pexels: query='%s' page=%s", prompt, page)

            r = _SESSION.get("https://api.pexels.com/v1/search", headers=headers, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            photos = data.get("photos") or []
            logger.debug("📸 Pexels: resultados=%d", len(photos))
            if not photos:
                raise RuntimeError("Pexels sem fotos.")
            choice = random.choice(photos)
            src = choice.get("src") or {}
            url = src.get("large2x") or src.get("large") or src.get("portrait")
            logger.debug("📸 Pexels: escolhida id=%s", choice.get("id"))

            raw_resp = _SESSION.get(url, timeout=20)
            raw_resp.raise_for_status()
            raw = raw_resp.content
            with open(arquivo_saida, "wb") as f:
                f.write(raw)
            img = Image.open(arquivo_saida)
            img = _ensure_1080x1920(img)
            img.save(arquivo_saida, quality=92)
            used.add(arquivo_saida); save_used_images(used)
            logger.info("✅ Imagem salva: %s", arquivo_saida.replace("\\", "/"))
            return
        except Exception as e:
            logger.warning("⚠️ Falha na geração (tentativa %d/%d): %s", attempt, max_retries, e)
    logger.error("❌ Não conseguiu gerar nova imagem após %d tentativas.", max_retries)

# -------------------- Helpers de layout do texto --------------------
_PUNCH_WORDS = {
    "você","voce","vida","fé","fe","deus","foco","força","forca","coragem",
    "propósito","proposito","sucesso","sonho","agora","hoje","mais","nunca","sempre"
}

def _parse_highlights_from_markdown(s: str) -> Tuple[str, List[str]]:
    """
    Remove **marcas** e retorna:
      clean: texto sem os **asteriscos**
      words: lista de PALAVRAS destacadas (sem pontuação, minúsculas)
    """
    segs = re.findall(r"\*\*(.+?)\*\*", s, flags=re.S)
    clean = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    words: List[str] = []
    for seg in segs:
        for tok in re.split(r"[^\wÀ-ÖØ-öø-ÿ]+", seg):
            tok = tok.strip().lower()
            if tok:
                words.append(tok)
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
    parts = re.split(r"(?:\.\.\.|[.!?:;—–-])", s)
    parts = [p.strip() for p in parts if p and p.strip()]
    if len(parts) >= 2:
        intro = parts[0]
        punch = " ".join(parts[1:]).strip()
    else:
        ws = s.split()
        if len(ws) >= 6:
            cut = max(3, min(len(ws)-2, len(ws)//2))
            intro = " ".join(ws[:cut])
            punch = " ".join(ws[cut:])
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

# -------------------- Renders --------------------
def _prepare_bg(img: Image.Image, base_dark: float, template: str) -> Image.Image:
    base = _ensure_1080x1920(img)
    if not IMAGE_DARK_ENABLE:
        logger.debug("🎛️ prepare_bg (NO-DARK) | template=%s", template)
        return base
    return _darken_and_vignette(
        base,
        base_dark_alpha=int(255 * _clamp(base_dark, 0.0, 1.0)),
        vig=IMAGE_VIGNETTE_STRENGTH,
        softness=IMAGE_VIGNETTE_SOFTNESS,
        center_protect=IMAGE_DARK_CENTER_PROTECT
    )

def _render_singleline_center(
    img: Image.Image,
    text: str,
    *,
    font_name: str,
    width_ratio: float = 0.86,
    y_ratio: float = 0.18,
    min_size: int = 54,
    max_size: int = 128,
    stroke_ratio: float = 0.045
) -> Image.Image:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)

    W, H = img.size
    draw = ImageDraw.Draw(img)
    text = text.upper() if IMAGE_TEXT_UPPER else text

    max_w = int(W * _clamp(width_ratio, 0.4, 0.95))
    lo, hi = min_size, max_size
    best = lo
    while lo <= hi:
        mid = (lo + hi)//2
        f = _load_font(font_name, mid)
        if draw.textbbox((0,0), text, font=f)[2] <= max_w:
            best = mid; lo = mid + 2
        else:
            hi = mid - 2
    font = _load_font(font_name, best)
    tw, _ = _text_size(draw, text, font)

    x = (W - tw) // 2
    y = int(H * _clamp(y_ratio, 0.05, 0.35))

    if IMAGE_TEXT_OUTLINE_STYLE == "stroke":
        stroke_w = max(1, int(best * stroke_ratio))
        _draw_text_with_stroke(draw, (x, y), text, font, "white", "black", stroke_w)
    elif IMAGE_TEXT_OUTLINE_STYLE == "shadow":
        sx, sy = IMAGE_SHADOW_OFFSET
        draw.text((x + sx, y + sy), text, font=font, fill=(0,0,0,IMAGE_SHADOW_ALPHA))
        draw.text((x, y), text, font=font, fill="white")
    else:
        draw.text((x, y), text, font=font, fill="white")

    return img

def _render_modern_block(img: Image.Image, frase: str) -> Image.Image:
    img = _prepare_bg(img, IMAGE_DARK_MODERN, "modern_block").convert("RGBA")
    W, H = img.size
    draw = ImageDraw.Draw(img)

    intro, punch, hl_words = _split_for_emphasis(frase)
    if IMAGE_TEXT_UPPER:
        intro = intro.upper()
        punch = punch.upper()

    base_scale = (W / 1080.0) * IMAGE_TEXT_SCALE * IMAGE_TEXT_SCALE_MODERN
    left  = int(W * 0.08)
    maxw  = int(W * 0.84)
    y     = int(H * 0.14)

    if intro:
        f_small, lines1 = _best_font_and_wrap(
            draw, intro, "Montserrat-Regular.ttf",
            maxw, int(38*base_scale), int(70*base_scale), max_lines=2
        )
        for ln in lines1:
            _draw_line_colored(draw, left, y, ln, f_small, set(), fill="white", hl_fill=IMAGE_HL_COLOR, style=None)
            y += int(f_small.size * 1.16)
        y += int(H * 0.018)

    f_main, lines2 = _best_font_and_wrap(
        draw, punch, "Montserrat-ExtraBold.ttf",
        maxw, int(68*base_scale), int(104*base_scale), max_lines=4
    )
    for ln in lines2:
        _draw_line_colored(
            draw, left, y, ln, f_main, set(hl_words),
            fill="white", hl_fill=IMAGE_HL_COLOR, style=None,
            stroke_w=max(2, int(f_main.size * 0.055)) if IMAGE_HL_STROKE else 0
        )
        y += int(f_main.size * 1.10)

    return img.convert("RGB")

def _render_classic_serif(img: Image.Image, frase: str) -> Image.Image:
    clean, explicit_words = _parse_highlights_from_markdown(frase.strip())
    hl_set = set(explicit_words)

    img = _prepare_bg(img, IMAGE_DARK_CLASSIC, "classic_serif").convert("RGBA")
    W, H = img.size
    draw = ImageDraw.Draw(img)

    text = clean.upper() if IMAGE_TEXT_UPPER else clean
    maxw = int(W * 0.76)

    scls = IMAGE_TEXT_SCALE * IMAGE_TEXT_SCALE_CLASSIC
    f_serif, lines = _best_font_and_wrap(
        draw, text, "PlayfairDisplay-Bold.ttf",
        maxw, int(54*scls), int(80*scls), max_lines=4
    )

    y = int(H*0.20)
    left = int(W*0.12)
    hl_for_draw = hl_set if (hl_set or not IMAGE_EMPHASIS_ONLY_MARKUP) else set()

    for ln in lines:
        _draw_line_colored(
            draw, left, y, ln, f_serif,
            highlight_set=hl_for_draw,
            fill="white",
            hl_fill=IMAGE_HL_COLOR,
            style=None,
            stroke_w=max(1, int(f_serif.size * 0.05)) if IMAGE_HL_STROKE else 0
        )
        y += int(f_serif.size * 1.18)
    return img.convert("RGB")

def _render_minimal_center(img: Image.Image, frase: str) -> Image.Image:
    img = _prepare_bg(img, IMAGE_DARK_MINIMAL, "minimal_center").convert("RGBA")
    W, H = img.size
    draw = ImageDraw.Draw(img)

    scls = IMAGE_TEXT_SCALE * IMAGE_TEXT_SCALE_MINIMAL
    big_name   = "BebasNeue-Regular.ttf"
    small_name = "Montserrat-Regular.ttf"

    clean, _ = _parse_highlights_from_markdown(frase)
    two = quebrar_em_duas_linhas(clean)
    if IMAGE_TEXT_UPPER:
        two = two.upper()
    parts = two.split("\n")
    l1 = parts[0].strip()
    l2 = " ".join(parts[1:]).strip() if len(parts) > 1 else ""

    small = _load_font(small_name, max(36, int(W*0.039*scls)))
    big   = _load_font(big_name,   max(90, int(W*0.102*scls)))

    b1 = draw.textbbox((0,0), l1, font=small); tw1 = b1[2]-b1[0]; th1 = b1[3]-b1[1]
    x1 = (W - tw1)//2; y = int(H*0.20)

    if IMAGE_TEXT_OUTLINE_STYLE == "stroke":
        _draw_text_with_stroke(draw, (x1, y), l1, small, "white", "black", max(1, int(small.size*0.05)))
    elif IMAGE_TEXT_OUTLINE_STYLE == "shadow":
        sx, sy = IMAGE_SHADOW_OFFSET
        draw.text((x1 + sx, y + sy), l1, font=small, fill=(0,0,0,IMAGE_SHADOW_ALPHA))
        draw.text((x1, y), l1, font=small, fill="white")
    else:
        draw.text((x1, y), l1, font=small, fill="white")

    y += th1 + int(H*0.02)

    maxw = int(W*0.90)
    words = l2.split(); cur=""; lines=[]
    for w in words:
        t=(cur+" "+w).strip()
        if draw.textbbox((0,0), t, font=big)[2] <= maxw:
            cur=t
        else:
            if cur: lines.append(cur)
            cur=w
    if cur: lines.append(cur)

    for ln in lines:
        bb=draw.textbbox((0,0), ln, font=big); tw=bb[2]-bb[0]
        x = (W - tw)//2
        if IMAGE_TEXT_OUTLINE_STYLE == "stroke":
            _draw_text_with_stroke(draw, (x, y), ln, big, "white", "black", max(2, int(big.size*0.05)))
        elif IMAGE_TEXT_OUTLINE_STYLE == "shadow":
            sx, sy = IMAGE_SHADOW_OFFSET
            draw.text((x + sx, y + sy), ln, font=big, fill=(0,0,0,IMAGE_SHADOW_ALPHA))
            draw.text((x, y), ln, font=big, fill="white")
        else:
            draw.text((x, y), ln, font=big, fill="white")
        y += (bb[3]-bb[1]) + int(big.size*0.06)
    return img.convert("RGB")

# -------------------- API pública --------------------
def escrever_frase_na_imagem(imagem_path: str, frase: str, saida_path: str, *,
                             idioma: str = "pt-br", template: str = "auto") -> None:
    if not os.path.exists(imagem_path):
        raise FileNotFoundError(imagem_path)
    img = Image.open(imagem_path)

    if template == "auto":
        template = random.choice(["classic_serif", "modern_block", "minimal_center"])

    if template == "classic_serif":
        out = _render_classic_serif(img, frase)
    elif template == "minimal_center":
        out = _render_minimal_center(img, frase)
    else:
        out = _render_modern_block(img, frase)

    os.makedirs(os.path.dirname(saida_path) or ".", exist_ok=True)
    out.save(saida_path, quality=92)

    try:
        w, h = out.size
        size_kb = os.path.getsize(saida_path) // 1024
    except Exception:
        w = h = size_kb = -1

    logger.info("✅ Imagem final salva (%s) | %dx%d | %d KB", template, w, h, size_kb)

def montar_slides_pexels(query: str, count: int = 4, primeira_imagem: Optional[str] = None) -> List[str]:
    if not PEXELS_API_KEY:
        logger.error("❌ PEXELS_API_KEY não configurado.")
        return [p for p in [primeira_imagem] if p and os.path.isfile(p)]

    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "orientation": "portrait", "size": "large", "per_page": 30, "page": 1}
    logger.debug("🖼️ Slides Pexels: query='%s' count=%d", query, count)

    r = _SESSION.get("https://api.pexels.com/v1/search", headers=headers, params=params, timeout=15)
    r.raise_for_status()
    photos = r.json().get("photos") or []
    logger.debug("🖼️ Slides Pexels: resultados=%d", len(photos))

    slides: List[str] = []
    if primeira_imagem and os.path.isfile(primeira_imagem):
        slides.append(primeira_imagem)

    random.shuffle(photos)
    for photo in photos:
        if len(slides) >= count:
            break
        src = photo.get("src") or {}
        url = src.get("large2x") or src.get("large") or src.get("portrait")
        if not url:
            continue
        try:
            resp = _SESSION.get(url, timeout=15)
            resp.raise_for_status()
            raw = resp.content
            pid = photo.get("id") or random.randint(100000, 999999)
            name = f"{_slug(query)}_{pid}.jpg"
            out = os.path.join(IMAGENS_DIR, name)
            with open(out, "wb") as f:
                f.write(raw)
            img = Image.open(out)
            img = _ensure_1080x1920(img)
            img.save(out, quality=92)
            logger.debug("⬇️  Slide salvo: id=%s", photo.get("id"))
            slides.append(out)
        except Exception as e:
            logger.warning("⚠️ Falha ao baixar slide id=%s: %s", photo.get("id"), e)

    if not slides and primeira_imagem and os.path.isfile(primeira_imagem):
        slides = [primeira_imagem]

    logger.info("🖼️ %d slide(s) prontos.", len(slides[:count]))
    return slides[:count]
