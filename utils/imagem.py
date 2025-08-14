# utils/imagem.py

import os
import re
import math
import json
import base64
import random
import logging
from typing import List, Tuple, Optional

import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps
from dotenv import load_dotenv

from utils.frase import quebrar_em_duas_linhas

# === CONFIGURAÇÕES ===
load_dotenv()

# Modo: "pexels", "local", "colab"
IMAGE_MODE = os.getenv("IMAGE_MODE", "local").lower()
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

# Defaults para o COLAB (podem ser sobrescritos por argumentos)
COLAB_WIDTH = int(os.getenv("COLAB_WIDTH", 720))
COLAB_HEIGHT = int(os.getenv("COLAB_HEIGHT", 1280))
COLAB_STEPS = int(os.getenv("COLAB_STEPS", 30))
COLAB_GUIDANCE = float(os.getenv("COLAB_GUIDANCE", 8.5))
COLAB_NEGATIVE = os.getenv("COLAB_NEGATIVE", "")
COLAB_SCHEDULER = os.getenv("COLAB_SCHEDULER", "")
COLAB_SEED = os.getenv("COLAB_SEED")

# CONFIG LOCAL (Stable Diffusion)
PIPELINE = None
MODEL_NAME = os.getenv("LOCAL_MODEL_NAME", "runwayml/stable-diffusion-v1-5")
IMAGE_WIDTH, IMAGE_HEIGHT = 576, 1024
INFERENCE_STEPS = 10
GUIDANCE_LOCAL = 6.5

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# --- caminhos RELATIVOS (simples) ---
CACHE_DIR = "cache"
os.makedirs(CACHE_DIR, exist_ok=True)
IMAGES_CACHE_FILE = os.path.join(CACHE_DIR, "used_images.json")

FONTS_DIR = "fonts"
LOGO_PATH = os.path.join("assets", "logo.png")  # opcional

# Paletas
PALETTES = {
    "modern": [
        {"accent": "#F5A524", "primary": "#FFFFFF", "outline": "#000000"},
        {"accent": "#60A5FA", "primary": "#FFFFFF", "outline": "#000000"},
        {"accent": "#5EEAD4", "primary": "#FFFFFF", "outline": "#000000"},
        {"accent": "#E879F9", "primary": "#FFFFFF", "outline": "#000000"},
        {"accent": "#F97316", "primary": "#FFFFFF", "outline": "#000000"},
    ],
    "classic": [
        {"accent": "#E5C04E", "primary": "#F3EFE4", "outline": "#000000"},
        {"accent": "#D9B453", "primary": "#F7F2E7", "outline": "#000000"},
    ],
    "minimal": [
        {"accent": "#E5E7EB", "primary": "#FFFFFF", "outline": "#000000"},
    ],
}

STOPWORDS = {
    "pt": {"de","da","do","das","dos","a","o","os","as","um","uma","e","ou",
           "no","na","nos","nas","em","para","por","que","se","com","pra","ao","à","às"},
    "en": {"the","a","an","and","or","to","of","in","on","for","with","at","by",
           "is","are","be","you","your","this","that"}
}

# -------------------------------------------------------------------
# Utilitários
# -------------------------------------------------------------------
def load_used_images():
    if os.path.exists(IMAGES_CACHE_FILE):
        with open(IMAGES_CACHE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_used_images(used_images):
    with open(IMAGES_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(list(used_images), f)

# ---- DEBUG leve de fontes ----
def _debug_fonts_env_once():
    if getattr(_debug_fonts_env_once, "_done", False):
        return
    _debug_fonts_env_once._done = True
    logger.info("📂 CWD: %s", os.getcwd())
    logger.info("📚 FONTS_DIR (relativo): %s", FONTS_DIR)
    try:
        items = ", ".join(sorted(os.listdir(FONTS_DIR)))
        logger.info("🗂️  Conteúdo de %s: %s", FONTS_DIR, items if items else "(vazio)")
    except Exception as e:
        logger.warning("Não foi possível listar %s: %s", FONTS_DIR, e)

def _norm_key(s: str) -> str:
    s = os.path.splitext(s)[0]
    s = re.sub(r'[^a-z0-9]', '', s.lower())
    s = re.sub(r'(.)\1+', r'\1', s)  # "Reegular" -> "Regular"
    return s

def _find_font_file(fname: str) -> Optional[str]:
    if not os.path.isdir(FONTS_DIR):
        return None
    exact = os.path.join(FONTS_DIR, fname)
    if os.path.isfile(exact):
        return exact
    files = os.listdir(FONTS_DIR)
    for f in files:
        if f.lower() == fname.lower():
            return os.path.join(FONTS_DIR, f)
    target_key = _norm_key(fname)
    for f in files:
        if _norm_key(f) == target_key:
            return os.path.join(FONTS_DIR, f)
    return None

def _load_font(fname: str, size: int) -> ImageFont.FreeTypeFont:
    """Carrega de fonts/ (relativo). Se falhar, usa default."""
    _debug_fonts_env_once()
    path = _find_font_file(fname)
    if not path:
        logger.warning("⚠️  Fonte '%s' não localizada em %s. Usando default.", fname, FONTS_DIR)
        return ImageFont.load_default()
    try:
        font = ImageFont.truetype(path, size=size)
        logger.info("🔤 Fonte carregada: %s (tam=%s)", os.path.basename(path), size)
        return font
    except Exception as e:
        logger.warning("❌ Falha ao abrir TTF '%s': %s. Usando default.", path, e)
        return ImageFont.load_default()

def _pick_highlights(text: str, idioma: str, max_words: int = 3, prefer: Optional[List[str]] = None) -> List[str]:
    lang = "pt" if (idioma or "").lower().startswith("pt") else "en"
    stop = STOPWORDS.get(lang, set())
    tokens = re.findall(r"\w+", text, flags=re.UNICODE)
    pref_set = {p.upper() for p in (prefer or [])}
    chosen: List[str] = []
    for w in tokens:
        if w.upper() in pref_set and w.lower() not in stop and w.upper() not in {c.upper() for c in chosen}:
            chosen.append(w)
            if len(chosen) >= max_words:
                return chosen
    cands = [t for t in tokens if len(t) >= 4 and t.lower() not in stop and t.upper() not in {c.upper() for c in chosen}]
    cands = sorted(set(cands), key=lambda x: (-len(x), x.lower()))
    chosen.extend(cands[: max_words - len(chosen)])
    return chosen

# --------- ESCURECER + VINHETA (suave, sem borrar fundo) ----------
def _darken_and_vignette(img: Image.Image,
                         base_dark: int = 70,
                         vignette_strength: float = 0.25,
                         feather: float = 0.20) -> Image.Image:
    """Escurece levemente e aplica vinheta nas bordas (sem borrar centro)."""
    w, h = img.size
    base = img.convert("RGBA")
    if base_dark > 0:
        base = Image.alpha_composite(base, Image.new("RGBA", (w, h), (0, 0, 0, int(base_dark))))
    grad = Image.new("L", (w * 2, h * 2), 0)
    d = ImageDraw.Draw(grad)
    d.ellipse((0, 0, w * 2, h * 2), fill=255)
    grad = grad.resize((w, h), Image.LANCZOS)
    blur_r = max(1, int(min(w, h) * max(0.05, min(0.35, feather))))
    grad = grad.filter(ImageFilter.GaussianBlur(radius=blur_r))
    edges_mask = ImageOps.invert(grad)
    if vignette_strength > 0:
        edges_overlay = Image.new("RGBA", (w, h), (0, 0, 0, int(255 * max(0.0, min(1.0, vignette_strength)))))
        with_edges = Image.alpha_composite(base, edges_overlay)
        base = Image.composite(base, with_edges, edges_mask)
    return base.convert("RGB")

def _apply_sepia(img: Image.Image, intensity: float = 0.25) -> Image.Image:
    gray = ImageOps.grayscale(img).convert("RGB")
    sepia_tone = Image.new("RGB", img.size, (112, 66, 20))
    return Image.blend(gray, sepia_tone, intensity)

def _text_wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> List[str]:
    words = text.split()
    if not words:
        return []
    lines, current = [], words[0]
    for w in words[1:]:
        test = current + " " + w
        bbox = draw.textbbox((0,0), test, font=font)
        if bbox[2]-bbox[0] <= max_w:
            current = test
        else:
            lines.append(current)
            current = w
    lines.append(current)
    return lines

def _wrap_words_to_lines(draw: ImageDraw.ImageDraw, words: List[str], font: ImageFont.FreeTypeFont, max_w: int) -> List[str]:
    """Quebra uma sequência de palavras em várias linhas respeitando max_w."""
    lines: List[str] = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        wpx = draw.textbbox((0,0), test, font=font)[2]
        if cur and wpx > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = test
    if cur:
        lines.append(cur)
    return lines

def _draw_text_with_stroke(draw, xy, text, font, fill, stroke_fill, stroke_w):
    x, y = xy
    if stroke_w > 0:
        for dx, dy in [(-stroke_w,0),(stroke_w,0),(0,-stroke_w),(0,stroke_w),
                       (-stroke_w,-stroke_w),(-stroke_w,stroke_w),(stroke_w,-stroke_w),(stroke_w,stroke_w)]:
            draw.text((x+dx, y+dy), text, font=font, fill=stroke_fill)
    draw.text((x, y), text, font=font, fill=fill)

def _place_logo(img: Image.Image):
    if not os.path.isfile(LOGO_PATH):
        return
    try:
        img_rgba = img.convert("RGBA")
        logo = Image.open(LOGO_PATH).convert("RGBA")
        W, H = img_rgba.size
        target_w = int(W * 0.16)
        ratio = target_w / logo.width
        logo = logo.resize((target_w, int(logo.height * ratio)), Image.LANCZOS)
        halo = Image.new("RGBA", logo.size, (0,0,0,120)).filter(ImageFilter.GaussianBlur(radius=6))
        canvas = Image.new("RGBA", img_rgba.size, (0,0,0,0))
        x = (W - logo.width)//2
        y = int(H * 0.82)
        canvas.paste(halo, (x, y), halo)
        canvas.paste(logo, (x, y), logo)
        out = Image.alpha_composite(img_rgba, canvas)
        img.paste(out)
    except Exception as e:
        logger.warning("Falha ao aplicar logo: %s", e)

# -------------------------------------------------------------------
# GERAÇÃO (mantida)
# -------------------------------------------------------------------
def descobrir_url_colab():
    url_api = "https://api.github.com/repos/muzuca/colab-api-endpoint/contents/ngrok_url.txt"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}" if GITHUB_TOKEN else None,
        "Accept": "application/vnd.github.v3.raw"
    }
    headers = {k: v for k, v in headers.items() if v is not None}
    try:
        logger.info("🔍 Buscando URL da API Colab no GitHub...")
        resp = requests.get(url_api, headers=headers, timeout=8)
        if resp.status_code == 200:
            linha = resp.text.strip()
            inicio = linha.find('"') + 1
            fim = linha.find('"', inicio)
            if inicio > 0 and fim > inicio:
                url = linha[inicio:fim]
                logger.info("✅ URL da API Colab encontrada: %s", url)
                return url
            else:
                raise ValueError("Formato da URL inválido na resposta.")
        else:
            raise Exception(f"Erro ao acessar arquivo GitHub: {resp.status_code}")
    except Exception as e:
        logger.error("❌ Erro ao obter URL do Colab: %s", str(e))
        return None

if IMAGE_MODE == "local":
    from diffusers import DiffusionPipeline
    import torch

    def inicializar_pipeline():
        global PIPELINE
        if PIPELINE is not None:
            return
        logger.info("⚙️ Carregando modelo local: %s", MODEL_NAME)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.float32
        PIPELINE = DiffusionPipeline.from_pretrained(
            MODEL_NAME,
            torch_dtype=dtype
        ).to(device)

def gerar_imagem_com_frase(
    prompt: str,
    arquivo_saida: str,
    *,
    width: int | None = None,
    height: int | None = None,
    steps: int | None = None,
    guidance_scale: float | None = None,
    negative_prompt: str | None = None,
    seed: int | None = None,
    scheduler: str | None = None,
    timeout: int = 120,
    max_retries: int = 3,
):
    """
    Gera SEMPRE uma imagem nova (nunca 'pula' por já existir no cache).
    Tenta até max_retries variações e salva em arquivo_saida (sobrescrevendo).
    """
    os.makedirs(os.path.dirname(arquivo_saida) or ".", exist_ok=True)

    if IMAGE_MODE == "pexels" and not PEXELS_API_KEY:
        logger.error("❌ Chave API da Pexels (PEXELS_API_KEY) não configurada no .env.")
        return

    used_images = load_used_images()

    # helper para registrar no cache
    def _commit_ok():
        used_images.add(arquivo_saida)
        save_used_images(used_images)
        logger.info("✅ Imagem salva: %s", arquivo_saida)

    for attempt in range(1, max_retries + 1):
        try:
            if IMAGE_MODE == "pexels":
                import random as _rnd
                logger.info("🌐 Pexels tentativa %d/%d…", attempt, max_retries)
                headers = {"Authorization": PEXELS_API_KEY}
                # pega um conjunto maior e escolhe aleatório pra reduzir repetição
                page = _rnd.randint(1, 10)
                per_page = 30
                params = {
                    "query": prompt,
                    "orientation": "portrait",
                    "size": "large",
                    "per_page": per_page,
                    "page": page,
                }
                r = requests.get("https://api.pexels.com/v1/search",
                                 headers=headers, params=params, timeout=15)
                r.raise_for_status()
                data = r.json()
                photos = data.get("photos") or []
                if not photos:
                    raise RuntimeError("Nenhuma imagem retornada pela Pexels.")

                choice = _rnd.choice(photos)
                image_url = choice["src"].get("large2x") or choice["src"].get("large") or choice["src"].get("portrait")
                if not image_url:
                    raise RuntimeError("Foto sem src válido.")

                img_data = requests.get(image_url, timeout=15).content
                with open(arquivo_saida, "wb") as f:
                    f.write(img_data)
                _commit_ok()
                return

            elif IMAGE_MODE == "local":
                # difusor local — varia seed automaticamente a cada tentativa
                from diffusers import DiffusionPipeline
                import torch, random as _rnd
                if PIPELINE is None:
                    logger.info("⚙️ Carregando modelo local: %s", MODEL_NAME)
                    device = "cuda" if torch.cuda.is_available() else "cpu"
                    dtype = torch.float32
                    globals()["PIPELINE"] = DiffusionPipeline.from_pretrained(
                        MODEL_NAME, torch_dtype=dtype
                    ).to(device)

                w = width or IMAGE_WIDTH
                h = height or IMAGE_HEIGHT
                st = steps or INFERENCE_STEPS
                gs = guidance_scale or GUIDANCE_LOCAL
                sd_seed = seed if seed is not None else _rnd.randint(0, 2**31 - 1)

                logger.info("🖥️ Local tentativa %d/%d… %dx%d steps=%d guidance=%.1f seed=%s",
                            attempt, max_retries, w, h, st, gs, sd_seed)

                prompt_final = f"{prompt}. cinematic, high detail, natural lights"
                image = PIPELINE(
                    prompt=prompt_final,
                    negative_prompt=negative_prompt or "",
                    width=w, height=h,
                    num_inference_steps=st,
                    guidance_scale=gs,
                    generator=torch.Generator().manual_seed(sd_seed),
                ).images[0]
                image.save(arquivo_saida)
                _commit_ok()
                return

            elif IMAGE_MODE == "colab":
                import random as _rnd
                if not GITHUB_TOKEN:
                    raise RuntimeError("GITHUB_TOKEN não configurado.")
                base_url = descobrir_url_colab()
                if not base_url:
                    raise RuntimeError("URL da API Colab não encontrada.")
                url = f"{base_url}/gerar-imagem"
                payload = {
                    "prompt": prompt,
                    "width": int(width or COLAB_WIDTH),
                    "height": int(height or COLAB_HEIGHT),
                    "steps": int(steps or COLAB_STEPS),
                    "guidance_scale": float(guidance_scale or COLAB_GUIDANCE),
                    "negative_prompt": (negative_prompt if negative_prompt is not None else COLAB_NEGATIVE),
                    "scheduler": (scheduler if scheduler is not None else COLAB_SCHEDULER),
                    "seed": _rnd.randint(0, 2**31-1) if seed is None else int(seed),
                }
                logger.info("☁️ Colab tentativa %d/%d…", attempt, max_retries)
                resp = requests.post(url, json=payload, timeout=timeout)
                if resp.status_code != 200:
                    raise RuntimeError(f"Erro do Colab: {resp.text}")
                data = resp.json()
                if "imagem_base64" not in data:
                    raise RuntimeError("Resposta inválida: imagem_base64 ausente")
                image_data = base64.b64decode(data["imagem_base64"])
                with open(arquivo_saida, "wb") as f:
                    f.write(image_data)
                _commit_ok()
                return

            else:
                raise RuntimeError(f"IMAGE_MODE inválido: {IMAGE_MODE}")

        except Exception as e:
            logger.warning("⚠️ Falha na geração (tentativa %d/%d): %s",
                           attempt, max_retries, e)

    logger.error("❌ Não conseguiu gerar nova imagem após %d tentativas.", max_retries)

# -------------------------------------------------------------------
# RENDER TEMPLATES
# -------------------------------------------------------------------
def _render_modern_block(img: Image.Image, frase: str, idioma: str) -> Image.Image:
    """
    Layout:
      - linha 1 menor (regular), pode quebrar em 1–2 linhas
      - linha 2 divide automaticamente em 1–3 linhas (bold) para não estourar a largura
      - se necessário, reduz tamanhos até caber (shrink-to-fit)
    """
    palette = random.choice(PALETTES["modern"])
    W, H = img.size
    pad_x = int(W * 0.08)
    pad_top = int(W * 0.08)
    max_w = int(W * 0.84)
    max_block_h = int(H * 0.35)  # altura reservada ao bloco de texto do topo

    # vinheta e darken leves
    img = _darken_and_vignette(img, base_dark=70, vignette_strength=0.18, feather=0.22).convert("RGBA")
    draw = ImageDraw.Draw(img)

    # fontes base
    base = 720
    scale = max(0.7, min(1.4, W / base))
    size_bold = int(78 * scale)
    size_reg  = int(36 * scale)

    bold_font    = _load_font("Montserrat-ExtraBold.ttf", size_bold)
    regular_font = _load_font("Montserrat-Regular.ttf",  size_reg)

    # separa em duas partes
    two = quebrar_em_duas_linhas(frase).upper().split("\n")
    line1_txt = two[0].strip()
    line2_words = (" ".join(two[1:]).strip() if len(two) > 1 else "").split()
    highlights = set(w.upper() for w in _pick_highlights(frase, idioma, max_words=3))

    # LINE 1 pode quebrar (no máx. 2 linhas)
    l1_lines = _text_wrap(draw, line1_txt, regular_font, max_w)
    if len(l1_lines) > 2:
        l1_lines = [" ".join(l1_lines)]  # se exagerou, volta para 1 linha

    # função para refazer as linhas grandes conforme tamanhos atuais
    def build_big_lines() -> List[str]:
        if not line2_words:
            return []
        lines = _wrap_words_to_lines(draw, line2_words, bold_font, max_w)
        # limita a 3 linhas juntando as últimas se necessário
        if len(lines) > 3:
            lines = lines[:2] + [" ".join(lines[2:])]
        return lines

    big_lines = build_big_lines()

    # mede altura total; se não couber, reduz tamanhos e reflowa
    def total_block_height() -> int:
        h1 = 0
        for ln in l1_lines:
            b = draw.textbbox((0,0), ln, font=regular_font)
            h1 += (b[3]-b[1]) + int(W * 0.012)
        if l1_lines:
            h1 -= int(W * 0.012)  # remove espaçamento extra ao fim

        gap1 = int(W * 0.02) if l1_lines and big_lines else 0

        hb = 0
        for ln in big_lines:
            b = draw.textbbox((0,0), ln, font=bold_font)
            hb += (b[3]-b[1]) + int(size_bold * 0.08)
        if big_lines:
            hb -= int(size_bold * 0.08)

        return h1 + gap1 + hb

    tries = 0
    while (
        any(draw.textbbox((0,0), ln, font=bold_font)[2] > max_w for ln in big_lines)
        or total_block_height() > max_block_h
    ) and tries < 10:
        tries += 1
        size_bold = max(28, int(size_bold * 0.92))
        size_reg  = max(18, int(size_reg  * 0.95))
        bold_font    = _load_font("Montserrat-ExtraBold.ttf", size_bold)
        regular_font = _load_font("Montserrat-Regular.ttf",  size_reg)
        l1_lines = _text_wrap(draw, line1_txt, regular_font, max_w)
        if len(l1_lines) > 2:
            l1_lines = [" ".join(l1_lines)]
        big_lines = build_big_lines()
        logger.info("✂️ shrink-to-fit (%d): bold=%d, reg=%d, l1=%d, l2=%d",
                    tries, size_bold, size_reg, len(l1_lines), len(big_lines))

    # desenha
    outline = palette["outline"]
    y = pad_top

    for i, ln in enumerate(l1_lines):
        _draw_text_with_stroke(draw, (pad_x, y), ln, regular_font,
                               fill=palette["primary"], stroke_fill=outline, stroke_w=2)
        b = draw.textbbox((0,0), ln, font=regular_font)
        y += (b[3]-b[1]) + int(W*0.012)

    if l1_lines and big_lines:
        y += int(W * 0.02)

    for ln in big_lines:
        x = pad_x
        for w in ln.split():
            clean = re.sub(r"[^\w]", "", w, flags=re.UNICODE).upper()
            color = palette["accent"] if clean in highlights else palette["primary"]
            _draw_text_with_stroke(draw, (x, y), w + " ", bold_font, fill=color,
                                   stroke_fill=outline, stroke_w=3)
            x = draw.textbbox((x, y), w + " ", font=bold_font)[2]
        b = draw.textbbox((0,0), ln, font=bold_font)
        y += (b[3]-b[1]) + int(size_bold * 0.08)

    _place_logo(img)
    return img

def _render_classic_serif(img: Image.Image, frase: str, idioma: str) -> Image.Image:
    palette = random.choice(PALETTES["classic"])
    img = _apply_sepia(img, 0.18)
    img = _darken_and_vignette(img, base_dark=80, vignette_strength=0.25, feather=0.24).convert("RGBA")

    W, H = img.size
    draw = ImageDraw.Draw(img)

    base = 720
    scale = max(0.7, min(1.4, W / base))
    serif_bold = _load_font("PlayfairDisplay-Bold.ttf", int(50*scale))
    serif_reg  = _load_font("PlayfairDisplay-Regular.ttf", int(36*scale))

    frase_caps = (frase or "").upper()
    col_w = int(W * 0.60)
    linhas = _text_wrap(draw, frase_caps, serif_reg, col_w)

    preferidas = ["HOJE","ONTEM","AMANHÃ","AMANHA","AMÉM","AMEM","DEUS","FORÇA","CORAGEM"]
    highlights = set(_pick_highlights(frase_caps, idioma, max_words=4, prefer=preferidas))

    left  = int(W * 0.12)
    total_h = 0
    for i, ln in enumerate(linhas):
        f = serif_bold if i >= 1 else serif_reg
        bbox = draw.textbbox((0,0), ln, font=f)
        total_h += (bbox[3]-bbox[1]) + int(10*scale)
    y = (H - total_h)//2 - int(0.05*H)

    for i, ln in enumerate(linhas):
        f = serif_bold if i >= 1 else serif_reg
        tokens = ln.split()
        x = left
        for j, w in enumerate(tokens):
            clean = re.sub(r"[^\wÁÀÂÃÉÈÊÍÌÎÓÒÔÕÚÙÛÇ]", "", w).upper()
            color = palette["accent"] if clean in highlights else palette["primary"]
            _draw_text_with_stroke(draw, (x, y), w + (" " if j < len(tokens)-1 else ""), f,
                                   fill=color, stroke_fill=palette["outline"], stroke_w=1)
            x = draw.textbbox((x, y), w + " ", font=f)[2]
        y += int(f.size * 1.25)

    _place_logo(img)
    return img

def _render_minimal_center(img: Image.Image, frase: str, idioma: str) -> Image.Image:
    palette = PALETTES["minimal"][0]
    W, H = img.size
    img = _darken_and_vignette(img, base_dark=85, vignette_strength=0.22, feather=0.22).convert("RGBA")
    draw = ImageDraw.Draw(img)

    base = 720
    scale = max(0.7, min(1.4, W / base))
    big   = _load_font("BebasNeue-Regular.ttf", int(92*scale))
    light = _load_font("Montserrat-Regular.ttf", int(36*scale))

    two = quebrar_em_duas_linhas(frase).upper().split("\n")
    line1 = two[0].strip()
    line2 = " ".join(two[1:]).strip() if len(two) > 1 else ""

    # se a linha 2 ficar muito larga, quebra em 2 linhas centralizadas
    max_w = int(W * 0.9)
    lines2 = _wrap_words_to_lines(draw, line2.split(), big, max_w) if line2 else []
    if len(lines2) > 2:
        lines2 = lines2[:1] + [" ".join(lines2[1:])]

    bbox1 = draw.textbbox((0,0), line1, font=light)
    h2 = sum(draw.textbbox((0,0), ln, font=big)[3] for ln in lines2) + (len(lines2)-1)*int(big.size*0.08)
    total_h = (bbox1[3]-bbox1[1]) + int(W*0.02) + h2
    y = (H - total_h)//2

    x1 = (W - (bbox1[2]-bbox1[0]))//2
    _draw_text_with_stroke(draw, (x1, y), line1, light, fill=palette["primary"], stroke_fill=palette["outline"], stroke_w=2)
    y += (bbox1[3]-bbox1[1]) + int(W*0.02)

    for ln in lines2:
        bb = draw.textbbox((0,0), ln, font=big)
        x2 = (W - (bb[2]-bb[0]))//2
        _draw_text_with_stroke(draw, (x2, y), ln, big, fill=palette["primary"], stroke_fill=palette["outline"], stroke_w=3)
        y += (bb[3]-bb[1]) + int(big.size*0.08)

    _place_logo(img)
    return img

# -------------------------------------------------------------------
# API de escrita
# -------------------------------------------------------------------
def escrever_frase_na_imagem(imagem_path: str,
                             frase: str,
                             saida_path: str,
                             * , 
                             idioma: str = "pt-br",
                             template: str = "auto") -> None:
    """
    Desenha a frase com estilo.
    template: "auto" | "classic_serif" | "modern_block" | "minimal_center"
    """
    try:
        if not os.path.exists(imagem_path):
            raise FileNotFoundError(f"Imagem de entrada {imagem_path} não encontrada.")

        img = Image.open(imagem_path).convert("RGB")

        if template == "auto":
            template = random.choice(["classic_serif", "modern_block", "minimal_center"])

        if template == "classic_serif":
            out = _render_classic_serif(img, frase, idioma)
        elif template == "minimal_center":
            out = _render_minimal_center(img, frase, idioma)
        else:
            out = _render_modern_block(img, frase, idioma)

        os.makedirs(os.path.dirname(saida_path) or ".", exist_ok=True)
        out.convert("RGB").save(saida_path, quality=92)
        logger.info("🖼️ Imagem final salva (%s): %s", template, saida_path)

    except Exception as e:
        logger.error("❌ Erro ao escrever a frase na imagem: %s", e)
