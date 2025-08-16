# utils/imagem.py
import os
import re
import json
import random
import logging
from typing import List, Optional

import requests
from requests.adapters import HTTPAdapter, Retry
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

def _make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.7, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    ph = os.getenv("PROXY_HOST")
    if ph:
        user = os.getenv("PROXY_USER", "")
        pw = os.getenv("PROXY_PASS", "")
        port = os.getenv("PROXY_PORT", "")
        auth = f"{user}:{pw}@" if (user or pw) else ""
        url = f"http://{auth}{ph}:{port}"
        s.proxies.update({"http": url, "https": url})
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

_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_logged_fonts: set[tuple[str, int]] = set()

def _load_font(fname: str, size: int) -> ImageFont.FreeTypeFont:
    key = (fname, size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    path = os.path.join(FONTS_DIR, fname)
    try:
        font = ImageFont.truetype(path, size=size)
        if key not in _logged_fonts:
            logger.info("🔤 Fonte carregada: %s (tam=%d)", fname, size)
            _logged_fonts.add(key)
        _FONT_CACHE[key] = font
        return font
    except Exception:
        if key not in _logged_fonts:
            logger.warning("⚠️  Fonte %s não encontrada; usando default.", fname)
            _logged_fonts.add(key)
        f = ImageFont.load_default()
        _FONT_CACHE[key] = f
        return f

def _draw_text_with_stroke(draw, xy, text, font, fill, stroke_fill, stroke_w):
    x, y = xy
    if stroke_w > 0:
        for dx, dy in [(-stroke_w,0),(stroke_w,0),(0,-stroke_w),(0,stroke_w),
                       (-stroke_w,-stroke_w),(-stroke_w,stroke_w),(stroke_w,-stroke_w),(stroke_w,stroke_w)]:
            draw.text((x+dx, y+dy), text, font=font, fill=stroke_fill)
    draw.text((x, y), text, font=font, fill=fill)

def _darken_and_vignette(img: Image.Image, base_dark=70, vig=0.2) -> Image.Image:
    w, h = img.size
    base = img.convert("RGBA")
    if base_dark:
        base = Image.alpha_composite(base, Image.new("RGBA", (w, h), (0,0,0,base_dark)))
    grad = Image.new("L", (w*2, h*2), 0)
    d = ImageDraw.Draw(grad); d.ellipse((0,0,w*2,h*2), fill=255)
    grad = grad.resize((w, h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(radius=max(8, min(w,h)//20)))
    return Image.composite(base, Image.new("RGBA", (w,h), (0,0,0,int(255*vig))), ImageOps.invert(grad)).convert("RGB")

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
            params = {"query": prompt, "orientation": "portrait", "size": "large", "per_page": 30, "page": random.randint(1, 10)}
            r = _SESSION.get("https://api.pexels.com/v1/search", headers=headers, params=params, timeout=15)
            r.raise_for_status()
            photos = r.json().get("photos") or []
            if not photos:
                raise RuntimeError("Pexels sem fotos.")
            choice = random.choice(photos)
            url = choice["src"].get("large2x") or choice["src"].get("large") or choice["src"].get("portrait")
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

def _render_modern_block(img: Image.Image, frase: str) -> Image.Image:
    img = _darken_and_vignette(_ensure_1080x1920(img), 70, 0.18).convert("RGBA")
    W, H = img.size; draw = ImageDraw.Draw(img)
    bold = _load_font("Montserrat-ExtraBold.ttf", 88)
    reg  = _load_font("Montserrat-Regular.ttf", 40)

    two = quebrar_em_duas_linhas(frase).upper().split("\n")
    line1 = two[0].strip()
    line2 = " ".join(two[1:]).strip() if len(two) > 1 else ""

    def wrap(text, font, maxw):
        words = text.split()
        lines, cur = [], ""
        for w in words:
            test = (cur + " " + w).strip()
            if draw.textbbox((0,0), test, font=font)[2] <= maxw:
                cur = test
            else:
                lines.append(cur); cur = w
        if cur: lines.append(cur)
        return lines

    maxw = int(W * 0.84)
    y = int(H * 0.18)
    l1 = wrap(line1, reg, maxw)[:2]
    for ln in l1:
        _draw_text_with_stroke(draw, (int(W*0.08), y), ln, reg, "white", "black", 2)
        y += int(reg.size * 1.2)
    y += int(H * 0.02)
    l2 = wrap(line2, bold, maxw) or [line2]
    for ln in l2:
        _draw_text_with_stroke(draw, (int(W*0.08), y), ln, bold, "white", "black", 3)
        y += int(bold.size * 1.18)
    return img.convert("RGB")

def _render_classic_serif(img: Image.Image, frase: str) -> Image.Image:
    img = _darken_and_vignette(_ensure_1080x1920(img), 85, 0.22).convert("RGBA")
    W, H = img.size; draw = ImageDraw.Draw(img)
    bold = _load_font("PlayfairDisplay-Bold.ttf", 70)
    reg  = _load_font("PlayfairDisplay-Regular.ttf", 50)

    text = frase.upper()
    maxw = int(W * 0.76)
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if draw.textbbox((0,0), t, font=reg)[2] <= maxw:
            cur = t
        else:
            lines.append(cur); cur = w
    if cur: lines.append(cur)
    y = int(H*0.25)
    for i, ln in enumerate(lines):
        f = bold if i >= 1 else reg
        _draw_text_with_stroke(draw, (int(W*0.12), y), ln, f, "white", "black", 2)
        y += int(f.size * 1.2)
    return img.convert("RGB")

def _render_minimal_center(img: Image.Image, frase: str) -> Image.Image:
    img = _darken_and_vignette(_ensure_1080x1920(img), 85, 0.20).convert("RGBA")
    W, H = img.size; draw = ImageDraw.Draw(img)
    big  = _load_font("BebasNeue-Regular.ttf", 110)
    small= _load_font("Montserrat-Regular.ttf", 42)

    two = quebrar_em_duas_linhas(frase).upper().split("\n")
    l1 = two[0].strip()
    l2 = " ".join(two[1:]).strip() if len(two) > 1 else ""

    b1 = draw.textbbox((0,0), l1, font=small); tw1 = b1[2]-b1[0]; th1 = b1[3]-b1[1]
    x1 = (W - tw1)//2; y = int(H*0.22)
    _draw_text_with_stroke(draw, (x1, y), l1, small, "white", "black", 2)
    y += th1 + int(H*0.02)

    words = l2.split(); cur=""; lines=[]
    for w in words:
        t=(cur+" "+w).strip()
        if draw.textbbox((0,0), t, font=big)[2] <= int(W*0.9):
            cur=t
        else:
            lines.append(cur); cur=w
    if cur: lines.append(cur)

    for ln in lines:
        bb=draw.textbbox((0,0), ln, font=big); tw=bb[2]-bb[0]
        _draw_text_with_stroke(draw, ((W-tw)//2, y), ln, big, "white", "black", 3)
        y += (bb[3]-bb[1]) + int(big.size*0.06)
    return img.convert("RGB")

def escrever_frase_na_imagem(imagem_path: str, frase: str, saida_path: str, *, idioma: str = "pt-br", template: str = "auto") -> None:
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
    logger.info("🖼️ Imagem final salva (%s): %s", template, saida_path.replace("\\", "/"))

def montar_slides_pexels(query: str, count: int = 4, primeira_imagem: Optional[str] = None) -> List[str]:
    if not PEXELS_API_KEY:
        logger.error("❌ PEXELS_API_KEY não configurado.")
        return [p for p in [primeira_imagem] if p and os.path.isfile(p)]

    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "orientation": "portrait", "size": "large", "per_page": 30, "page": 1}
    r = _SESSION.get("https://api.pexels.com/v1/search", headers=headers, params=params, timeout=15)
    r.raise_for_status()
    photos = r.json().get("photos") or []

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
            logger.info("⬇️  Slide salvo: %s", out.replace("\\", "/"))
            slides.append(out)
        except Exception:
            continue

    return slides[:count] if slides else [p for p in [primeira_imagem] if p and os.path.isfile(p)]
