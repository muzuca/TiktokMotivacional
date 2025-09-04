# utils/video.py
import os
import re
import logging
import shutil
import subprocess
import uuid
from typing import Optional, List, Tuple

from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

# Importa apenas o necessário do frase.py
from .frase import gerar_frase_motivacional_longa, _split_for_emphasis, quebrar_em_duas_linhas
try:
    from .frase import gerar_frase_tarot_longa
    _HAVE_TAROT_LONG = True
except Exception:
    _HAVE_TAROT_LONG = False

from .audio import obter_caminho_audio, gerar_narracao_tts
from .subtitles import make_segments_for_audio

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ================== Presets / Constantes ==================
PRESETS = {
    "sd":     {"w": 540,  "h": 960,  "br_v": "1400k", "br_a": "128k", "level": "3.1"},
    "hd":     {"w": 720,  "h": 1280, "br_v": "2200k", "br_a": "128k", "level": "3.1"},
    "fullhd": {"w": 1080, "h": 1920, "br_v": "5000k", "br_a": "192k", "level": "4.0"},
}
FPS_OUT = 30
AUDIO_SR = 44100

# Diretórios
IMAGES_DIR = os.getenv("IMAGES_DIR", "imagens")
AUDIO_DIR  = os.getenv("AUDIO_DIR", "audios")
FONTS_DIR = "fonts"
CACHE_DIR = "cache"
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(os.path.join(AUDIO_DIR, "tts"), exist_ok=True)

# Helpers de .env
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
    v = os.getenv(name, str(default)).strip().lower()
    return v in ("1", "true", "yes", "on")

def _env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return default if v is None else str(v)

# Parâmetros de Vídeo e Áudio
BG_MIX_VOLUME      = _env_float("BG_MIX_VOLUME", 0.07)
DEFAULT_TRANSITION = os.getenv("TRANSITION", "fade").lower()
KENBURNS_ZOOM_MAX  = _env_float("KENBURNS_ZOOM_MAX", 1.22)
PAN_ZOOM           = _env_float("PAN_ZOOM", 1.18)
MOTION_FPS         = _env_int("MOTION_FPS", 45)
VIDEO_RESPECT_TTS  = _env_bool("VIDEO_RESPECT_TTS", True)
VIDEO_TAIL_PAD     = _env_float("VIDEO_TAIL_PAD", 0.40)
VIDEO_MAX_S        = _env_float("VIDEO_MAX_S", 0.0)
DUCK_ENABLE        = _env_bool("DUCK_ENABLE", True)

# Limpeza opcional do áudio de fundo (por padrão: apaga)
CLEANUP_BG_AUDIO   = _env_bool("CLEANUP_BG_AUDIO", True)

# Parâmetros de Estilo de Texto (espelhados de imagem.py)
IMAGE_TEXT_UPPER           = _env_bool("IMAGE_TEXT_UPPER", True)
IMAGE_TEXT_OUTLINE_STYLE   = os.getenv("IMAGE_TEXT_OUTLINE_STYLE", "shadow").strip().lower()
IMAGE_STROKE_WIDTH         = _env_int("IMAGE_STROKE_WIDTH", 2)
try:
    _dx, _dy = [int(v) for v in (os.getenv("IMAGE_SHADOW_OFFSET", "2,3").split(","))]
    IMAGE_SHADOW_OFFSET = (_dx, _dy)
except Exception:
    IMAGE_SHADOW_OFFSET = (2, 3)
IMAGE_SHADOW_ALPHA         = _env_int("IMAGE_SHADOW_ALPHA", 170)
IMAGE_HL_COLOR             = os.getenv("IMAGE_HL_COLOR", "#F3B34A")
IMAGE_TEXT_SCALE           = _env_float("IMAGE_TEXT_SCALE", 1.10)
IMAGE_TEXT_SCALE_CLASSIC   = _env_float("IMAGE_TEXT_SCALE_CLASSIC", 1.12)
IMAGE_TEXT_SCALE_MODERN    = _env_float("IMAGE_TEXT_SCALE_MODERN", 0.92)
IMAGE_TEXT_SCALE_MINIMAL   = _env_float("IMAGE_TEXT_SCALE_MINIMAL", 0.95)
ARABIC_FONT_IMAGE_REG      = os.getenv("ARABIC_FONT_IMAGE_REG",  "NotoNaskhArabic-Regular.ttf")
ARABIC_FONT_IMAGE_BOLD     = os.getenv("ARABIC_FONT_IMAGE_BOLD", "NotoNaskhArabic-Bold.ttf")

# ============ Legendas (fonts) ============
SUB_FONT_SCALE_1 = _env_float("SUB_FONT_SCALE_1", 0.050)
SUB_FONT_SCALE_2 = _env_float("SUB_FONT_SCALE_2", 0.056)
SUB_FONT_SCALE_3 = _env_float("SUB_FONT_SCALE_3", 0.044)
REQUIRE_FONTFILE = _env_bool("REQUIRE_FONTFILE", False)

# Árabe: fonte fixa (sem fallback) — se não estiver presente, erro explícito.
ARABIC_FONT = os.getenv("ARABIC_FONT", "NotoNaskhArabic-Regular.ttf")
ARABIC_FONT_STRICT = _env_bool("ARABIC_FONT_STRICT", True)

# Russo: usa por padrão a fonte disponibilizada por você
CYRILLIC_FONT = _env_str("CYRILLIC_FONT", "bebas-neue-cyrillic.ttf")

# ================== Helpers ==================
def _ffmpeg_or_die() -> str:
    return os.getenv("FFMPEG_BIN") or "ffmpeg"

def _ffprobe_or_die() -> str:
    return os.getenv("FFPROBE_BIN") or "ffprobe"

def _ffmpeg_has_filter(filter_name: str) -> bool:
    try:
        out = subprocess.check_output([_ffmpeg_or_die(), "-hide_banner", "-filters"], stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
        return any(re.search(rf"\b{re.escape(filter_name)}\b", line) for line in out.splitlines())
    except Exception:
        return False

def _idioma_norm(idioma: str) -> str:
    """
    Normaliza o idioma para: 'pt', 'ar', 'ru' ou 'en' (default).
    Isso é usado para TTS, escolha de fonte e geração de legendas.
    """
    s = (idioma or "en").lower()
    if s.startswith("pt"): return "pt"
    if s.startswith("ar"): return "ar"
    if s.startswith("ru"): return "ru"
    return "en"

def _text_contains_arabic(s: str) -> bool:
    return bool(re.search(r'[\u0600-\u06FF]', s or ""))

def _duracao_audio_segundos(a: str) -> Optional[float]:
    if not a or not os.path.isfile(a): return None
    try:
        out = subprocess.check_output([_ffprobe_or_die(), "-v","error","-show_entries","format=duration", "-of","default=noprint_wrappers=1:nokey=1", a], text=True).strip()
        return float(out) if float(out) > 0 else None
    except Exception:
        return None

def _uuid_suffix() -> str:
    return uuid.uuid4().hex[:8]

def _stage_to_dir(src_path: str, target_dir: str, prefix: str) -> str:
    os.makedirs(target_dir, exist_ok=True)
    base, ext = os.path.splitext(os.path.basename(src_path))
    dst_name = f"{prefix}_{base}_{_uuid_suffix()}{ext or '.jpg'}"
    dst_path = os.path.join(target_dir, dst_name)
    shutil.copy2(src_path, dst_path)
    return dst_path

_IS_WIN = (os.name == "nt")

def _ff_escape_filter_path(p: str) -> str:
    s = p.replace("\\", "/").replace("'", r"\'")
    if _IS_WIN and re.match(r"^[A-Za-z]:/", s):
        return s.replace(":", r"\\:")
    return s

def _ff_q(val: str) -> str:
    return f"'{_ff_escape_filter_path(val)}'"

# ================== Motion ==================
def _smoothstep_expr(p: str) -> str:
    return f"(({p})*({p})*(3-2*({p})))"

def _kb_in(W,H,F):
    p=f"(on/{F})"; ps=_smoothstep_expr(p); z=f"(1+({KENBURNS_ZOOM_MAX:.5f}-1)*{ps})"
    return f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={F}:s={W}x{H}:fps={MOTION_FPS}"

def _kb_out(W,H,F):
    p=f"(on/{F})"; ps=_smoothstep_expr(p); z=f"({KENBURNS_ZOOM_MAX:.5f}+(1-{KENBURNS_ZOOM_MAX:.5f})*{ps})"
    return f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={F}:s={W}x{H}:fps={MOTION_FPS}"

def _pan_lr(W,H,F):
    p=f"(on/{F})"; ps=_smoothstep_expr(p)
    return f"zoompan=z={PAN_ZOOM:.5f}:x='(iw/zoom-ow)*{ps}':y='(ih/zoom-oh)/2':d={F}:s={W}x{H}:fps={MOTION_FPS}"

def _pan_ud(W,H,F):
    p=f"(on/{F})"; ps=_smoothstep_expr(p)
    return f"zoompan=z={PAN_ZOOM:.5f}:x='(iw/zoom-ow)/2':y='(ih/zoom-oh)*{ps}':d={F}:s={W}x{H}:fps={MOTION_FPS}"

def _build_slide_branch(idx: int, W: int, H: int, motion: str, per_slide: float) -> str:
    F = max(1, int(round(per_slide * MOTION_FPS)))
    m = (motion or "none").lower()
    motion_map = {"kenburns_in": _kb_in, "2": _kb_in, "kenburns_out": _kb_out, "3": _kb_out, "pan_lr": _pan_lr, "4": _pan_lr, "pan_ud": _pan_ud, "5": _pan_ud}
    func = motion_map.get(m)
    if func:
        return f"[{idx}:v]{func(W,H,F)},format=yuv420p,setsar=1/1,fps={FPS_OUT}[v{idx}]"
    return f"[{idx}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,format=yuv420p,setsar=1/1,fps={FPS_OUT}[v{idx}]"

# ================== Lógica de Renderização de Título (Sincronizada com imagem.py) ==================
_FONT_CACHE: dict[Tuple[str, int], ImageFont.FreeTypeFont] = {}

def _load_font(fname: str, size: int) -> ImageFont.FreeTypeFont:
    key = (fname, size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    primary = os.path.abspath(os.path.join(FONTS_DIR, fname))
    try:
        font = ImageFont.truetype(primary, size=size) if os.path.isfile(primary) else ImageFont.truetype(fname, size=size)
        _FONT_CACHE[key] = font
        return font
    except Exception:
        return ImageFont.load_default()

def _hex_to_rgba(hex_color: str, alpha: int = 255) -> Tuple[int,int,int,int]:
    s = (hex_color or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch*2 for ch in s)
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        return (r,g,b,alpha)
    except Exception:
        return (243,179,74,alpha)

def _best_font_and_wrap(draw, text, font_name, maxw, min_size, max_size, max_lines=3):
    words = text.split()
    lo, hi = min_size, max_size
    best_font = _load_font(font_name, lo)

    def wrap_words(f):
        lines, cur = [], []
        width_of = lambda ws: draw.textbbox((0,0), " ".join(ws), font=f)[2]
        for w in words:
            if not cur:
                cur = [w]; continue
            if width_of(cur + [w]) <= maxw:
                cur.append(w)
            else:
                lines.append(cur); cur = [w]
        if cur:
            lines.append(cur)
        return lines

    best_lines = wrap_words(best_font)
    while lo <= hi:
        mid = (lo + hi) // 2
        f = _load_font(font_name, mid)
        lines = wrap_words(f)
        try:
            w_ok = max(draw.textbbox((0,0), " ".join(ln), font=f)[2] for ln in lines) <= maxw
            if w_ok and len(lines) <= max_lines:
                best_font, best_lines = f, lines
                lo = mid + 2
            else:
                hi = mid - 2
        except Exception:
            hi = mid - 2
    return best_font, [" ".join(l) for l in best_lines]

def _draw_line_colored(draw, x, y, line_text, font, highlight_set, fill="white", hl_fill=(243, 179, 74)):
    tokens = line_text.split(" ")
    cur_x = x
    for i, raw in enumerate(tokens):
        key = re.sub(r"[^\wÀ-ÖØ-öø-ÿ]", "", raw).lower()
        color = hl_fill if key in highlight_set else fill
        if IMAGE_TEXT_OUTLINE_STYLE == "shadow":
            sx, sy = IMAGE_SHADOW_OFFSET
            shadow_col = (0, 0, 0, IMAGE_SHADOW_ALPHA)
            draw.text((cur_x + sx, y + sy), raw, font=font, fill=shadow_col)
            draw.text((cur_x, y), raw, font=font, fill=color)
        else:  # stroke
            stroke_w = IMAGE_STROKE_WIDTH
            for dx in range(-stroke_w, stroke_w + 1, stroke_w):
                for dy in range(-stroke_w, stroke_w + 1, stroke_w):
                    if dx != 0 or dy != 0:
                        draw.text((cur_x + dx, y + dy), raw, font=font, fill=(0,0,0,200))
            draw.text((cur_x, y), raw, font=font, fill=color)
        cur_x += draw.textbbox((0,0), raw + (" " if i < len(tokens)-1 else ""), font=font)[2]

def _font_for_lang(base_font, idioma, bold=False):
    if _idioma_norm(idioma) == "ar":
        return ARABIC_FONT_IMAGE_BOLD if bold else ARABIC_FONT_IMAGE_REG
    return base_font

def _render_modern_block(img, frase, *, idioma=None):
    W, H = img.size
    draw = ImageDraw.Draw(img)
    intro, punch, hl_words = _split_for_emphasis(frase)
    is_ar = (_idioma_norm(idioma) == "ar")
    if IMAGE_TEXT_UPPER and not is_ar:
        intro, punch = intro.upper(), punch.upper()
    base_scale = (W / 1080.0) * IMAGE_TEXT_SCALE * IMAGE_TEXT_SCALE_MODERN
    left_margin, right_margin, maxw = int(W*0.08), int(W*0.08), int(W*0.84)
    y = int(H * 0.20)  # começa mais alto

    if intro:
        f_small, lines1 = _best_font_and_wrap(
            draw, intro, _font_for_lang("Montserrat-Regular.ttf", idioma, bold=False),
            maxw, int(38*base_scale), int(70*base_scale), max_lines=2
        )
        for ln in lines1:
            _draw_line_colored(draw, left_margin, y, ln, f_small, set())
            y += int(f_small.size * 1.16)
        y += int(H * 0.018)

    f_main, lines2 = _best_font_and_wrap(
        draw, punch, _font_for_lang("Montserrat-ExtraBold.ttf", idioma, bold=True),
        maxw, int(68*base_scale), int(104*base_scale), max_lines=4
    )
    for ln in lines2:
        _draw_line_colored(draw, left_margin, y, ln, f_main, set(hl_words), hl_fill=_hex_to_rgba(IMAGE_HL_COLOR))
        y += int(f_main.size * 1.10)
    return img

def _render_classic_serif(img, frase, *, idioma=None):
    W, H = img.size
    draw = ImageDraw.Draw(img)
    clean = re.sub(r'\*\*(.*?)\*\*', r'\1', frase)
    explicit_words = [w.lower() for w in re.findall(r"\*\*(.+?)\*\*", frase)]
    hl_set = set(explicit_words)
    is_ar = (_idioma_norm(idioma) == "ar")
    text = clean.upper() if (IMAGE_TEXT_UPPER and not is_ar) else clean
    left_margin, right_margin, maxw = int(W*0.12), int(W*0.10), int(W*0.78)
    scls = IMAGE_TEXT_SCALE * IMAGE_TEXT_SCALE_CLASSIC
    f_serif, lines = _best_font_and_wrap(
        draw, text, _font_for_lang("PlayfairDisplay-Bold.ttf", idioma, bold=True),
        maxw, int(54*scls), int(80*scls), max_lines=4
    )
    y = int(H * 0.20)
    for ln in lines:
        _draw_line_colored(draw, left_margin, y, ln, f_serif, highlight_set=hl_set, hl_fill=_hex_to_rgba(IMAGE_HL_COLOR))
        y += int(f_serif.size * 1.18)
    return img

def _title_overlay_png(text: str, style_id: str, idioma: str, W: int, H: int) -> str:
    img = Image.new("RGBA", (W, H), (0,0,0,0))
    if style_id == "1":  # Clássico
        final_img = _render_classic_serif(img, text, idioma=idioma)
    else:  # Moderno e outros
        final_img = _render_modern_block(img, text, idioma=idioma)
    out_path = os.path.join("cache", f"title_overlay_{_uuid_suffix()}.png")
    final_img.save(out_path, "PNG")
    return out_path

# ================== Legendas (drawtext) ==================
def _normalize_style(style: str) -> str:
    s = (style or "1").strip().lower()
    return str({
        "classic": "1", "modern": "2", "serif": "3", "mono": "4", "clean": "5"
    }.get(s, s if s in "12345" else "1"))

def _first_existing_font(*names: str) -> Optional[str]:
    for n in names:
        p = os.path.join(FONTS_DIR, n)
        if os.path.isfile(p):
            return p
    for p in ["/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "C:\\Windows\\Fonts\\arial.ttf"]:
        if os.path.isfile(p):
            return p
    return None

def _style_fontsize_from_H(H: int, style_id: str) -> Tuple[int, int, int]:
    sid = _normalize_style(style_id)
    if sid == "1":
        scale, margin_pct = SUB_FONT_SCALE_1, 0.12
    elif sid == "2":
        scale, margin_pct = SUB_FONT_SCALE_2, 0.125
    else:
        scale, margin_pct = SUB_FONT_SCALE_3, 0.13
    fs = max(18, int(H * scale))
    borderw = max(1, int(fs * 0.05))
    margin = max(58, int(H * margin_pct))
    return fs, borderw, margin

def _get_subtitle_font_path(lang_norm: str) -> Optional[str]:
    """
    Retorna o caminho da fonte das legendas para cada idioma.
    - AR: fonte fixa (ARABIC_FONT). Se não existir e ARABIC_FONT_STRICT=1 (padrão), lança erro;
          caso contrário, tenta NotoNaskhArabic.
    - RU: prioriza 'bebas-neue-cyrillic.ttf' (ou CYRILLIC_FONT do .env).
    - PT/EN: BebasNeue-Regular.ttf.
    """
    # Árabe (sempre a mesma)
    if lang_norm == "ar":
        p = os.path.join(FONTS_DIR, ARABIC_FONT)
        if os.path.isfile(p):
            return p
        if ARABIC_FONT_STRICT or REQUIRE_FONTFILE:
            raise FileNotFoundError(
                f"Fonte árabe obrigatória não encontrada: {p}. "
                f"Defina ARABIC_FONT corretamente e coloque o arquivo em '{FONTS_DIR}'."
            )
        logger.warning("AR: fonte %s não encontrada — usando fallback NotoNaskhArabic.", ARABIC_FONT)
        return _first_existing_font("NotoNaskhArabic-Regular.ttf", "NotoNaskhArabic-Bold.ttf")

    # Russo (Bebas Neue compatível com cirílico)
    if lang_norm == "ru":
        # ordem de preferência
        candidates = [
            CYRILLIC_FONT,
            "bebas-neue-cyrillic.ttf",   # sua fonte
            "BebasNeue-Regular.ttf",     # métrica similar
            "Inter-Bold.ttf",
            "Montserrat-Bold.ttf",
            "NotoSans-Regular.ttf",
            "Arial.ttf",
        ]
        for n in candidates:
            p = os.path.join(FONTS_DIR, n)
            if os.path.isfile(p):
                if n != CYRILLIC_FONT:
                    logger.warning("RU: fonte preferida '%s' não encontrada — usando '%s'.", CYRILLIC_FONT, n)
                return p
        return None

    # Demais (pt/en…)
    return _first_existing_font("BebasNeue-Regular.ttf", "Inter-Bold.ttf", "Montserrat-Bold.ttf")

def _write_textfile_for_drawtext(content: str, idx: int) -> str:
    path = os.path.join(CACHE_DIR, f"drawtext_{idx:02d}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(re.sub(r"\s+", " ", content.strip()))
    return path

def _build_subs_drawtext_chain(
    H: int,
    style_id: str,
    segments: List[Tuple[float, float, str]],
    font_path: Optional[str],
    *,
    rtl: bool = False
) -> str:
    """
    Gera uma cadeia de drawtext para queimar as legendas.
    - LTR (padrão): centraliza (x = (w-text_w)/2)
    - RTL (árabe): alinha à direita (x = w - (text_w + margin))
    """
    fs, borderw, margin = _style_fontsize_from_H(H, style_id)
    font_opt = f":fontfile={_ff_q(font_path)}" if font_path and os.path.isfile(font_path) else ""
    if not font_opt and REQUIRE_FONTFILE:
        raise RuntimeError("Fonte de legendas não encontrada.")

    # X expr de acordo com a direção do texto
    if rtl:
        x_expr = f"w-(text_w+{margin})"
    else:
        x_expr = "(w-text_w)/2"

    y_expr = f"h-(text_h+{margin})"

    # Observação: não colocamos text_shaping=1 para evitar falha em builds sem harfbuzz/fribidi
    blocks = []
    for idx, (ini, fim, txt) in enumerate(segments, start=1):
        tf = _write_textfile_for_drawtext(txt, idx)
        block = (
            f"drawtext=textfile={_ff_q(tf)}{font_opt}"
            f":fontsize={fs}:fontcolor=white"
            f":borderw={borderw}:bordercolor=black@0.85"
            f":shadowcolor=black@0.7:shadowx=2:shadowy=2"
            f":x={x_expr}:y={y_expr}"
            f":enable='between(t,{ini:.3f},{fim:.3f})'"
        )
        blocks.append(block)
    return ",".join(blocks)

# ================== Pipeline Principal ==================
def gerar_video(
    imagem_path,
    saida_path,
    *,
    frase_principal="",
    preset="fullhd",
    idioma="auto",
    tts_engine="gemini",
    legendas=True,
    video_style="1",
    motion="none",
    slides_paths=None,
    transition=None,
    content_mode="motivacional",
    # --- novos parâmetros para evitar chamadas duplicadas ---
    long_text: Optional[str] = None,
    tts_path: Optional[str] = None,
    background_audio_path: Optional[str] = None,
    segments_override: Optional[List[Tuple[float, float, str]]] = None,
):
    """
    Agora aceita:
      - long_text: narração já gerada externamente (reusa, não chama gerar_frase_*).
      - tts_path: caminho de um TTS já gerado (reusa, não chama gerar_narracao_tts).
      - background_audio_path: se já tiver escolhido/baixado bg antes.
      - segments_override: se já tiver segmentos prontos de legenda.

    Se não forem passados, o comportamento anterior é mantido.
    """
    staged_images: List[str] = []
    staged_tts: Optional[str] = None
    staged_title_overlay: Optional[str] = None
    extra_to_cleanup: List[str] = []

    # também vamos manter referência dos SLIDES ORIGINAIS para apagar depois
    original_slides_used: List[str] = []

    # manter referência ao BG music para limpeza opcional
    bg_audio_to_cleanup: Optional[str] = None

    try:
        conf = PRESETS.get(preset, PRESETS["fullhd"])
        W, H = conf["w"], conf["h"]
        BR_V, BR_A, LEVEL = conf["br_v"], conf["br_a"], conf["level"]

        slides_validos = [p for p in (slides_paths or [imagem_path]) if p and os.path.isfile(p)]
        if not slides_validos:
            raise FileNotFoundError("Nenhuma imagem de slide válida foi fornecida.")
        original_slides_used = list(slides_validos)  # guardamos os originais

        n_slides = len(slides_validos)

        lang_norm = _idioma_norm(idioma)
        if _text_contains_arabic(frase_principal):
            lang_norm = "ar"
        style_norm = _normalize_style(video_style)

        # --- NARRAÇÃO LONGA ---
        if long_text and isinstance(long_text, str) and long_text.strip():
            logger.info("📝 Usando narração fornecida externamente (%d chars).", len(long_text))
        else:
            # comportamento antigo (gera internamente)
            if (content_mode or "").lower() == "tarot" and _HAVE_TAROT_LONG:
                long_text = gerar_frase_tarot_longa(idioma)
            else:
                long_text = gerar_frase_motivacional_longa(idioma)
            logger.info("📝 Narração gerada internamente (frase longa).")

        # --- TTS ---
        if tts_path and os.path.isfile(tts_path):
            voice_audio_path = tts_path
            logger.info("🎧 Usando TTS existente: %s", voice_audio_path)
        else:
            voice_audio_path = gerar_narracao_tts(long_text, idioma=lang_norm, engine=tts_engine)
            logger.info("🎧 TTS gerado internamente via %s.", tts_engine)

        dur_voz = _duracao_audio_segundos(voice_audio_path)
        logger.info("🎙️ Duração da voz (ffprobe): %.2fs", dur_voz or 0.0)

        if voice_audio_path:
            staged_tts = _stage_to_dir(voice_audio_path, os.path.join(AUDIO_DIR, "tts"), "tts")
            # se veio de fora, não apagar a fonte; só apaga se for de um temp interno
            if os.path.basename(os.path.dirname(voice_audio_path)).lower() in ("audios_tts", "tts"):
                extra_to_cleanup.append(voice_audio_path)
            voice_audio_path = staged_tts

        # --- BG MUSIC ---
        if background_audio_path and os.path.isfile(background_audio_path):
            bg_path = background_audio_path
            logger.info("🎵 Usando BG pré-definido: %s", os.path.basename(bg_path))
        else:
            bg_path = obter_caminho_audio(idioma=lang_norm)

        has_voice, has_bg = bool(voice_audio_path), bool(bg_path)

        if CLEANUP_BG_AUDIO and bg_path and os.path.isfile(bg_path):
            bg_audio_to_cleanup = bg_path

        # --- LEGENDAS ---
        if segments_override is not None:
            segments = segments_override
        else:
            if legendas and has_voice:
                if not long_text:
                    logger.warning("Legendas ativadas, mas sem 'long_text' — pulando geração de legendas.")
                    segments = []
                else:
                    segments = make_segments_for_audio(long_text, voice_audio_path, idioma=lang_norm)
            else:
                segments = []

        if segments:
            logger.info("📝 %d segmentos de legenda gerados.", len(segments))

        total_video = (dur_voz or 12.0) + VIDEO_TAIL_PAD if has_voice and VIDEO_RESPECT_TTS else 12.0
        if VIDEO_MAX_S > 0:
            total_video = min(total_video, VIDEO_MAX_S)

        trans = transition or DEFAULT_TRANSITION or "fade"
        trans_dur = max(0.45, min(0.85, (total_video / n_slides) * 0.135)) if n_slides > 1 else 0.0
        per_slide = (total_video + (n_slides - 1) * trans_dur) / n_slides if n_slides > 0 else 0

        cmd_base = [_ffmpeg_or_die(), "-y", "-loglevel", "error", "-stats"]
        staged_inputs = [_stage_to_dir(p, IMAGES_DIR, "stage") for p in slides_validos]
        staged_images.extend(staged_inputs)

        for sp in staged_inputs:
            cmd_base += ["-framerate", str(FPS_OUT), "-loop", "1", "-i", sp]

        use_title = bool(frase_principal and frase_principal.strip())
        if use_title:
            logger.info("✍️ Gerando overlay de título (PNG transparente)...")
            staged_title_overlay = _title_overlay_png(frase_principal, style_norm, lang_norm, W, H)
            cmd_base += ["-framerate", str(FPS_OUT), "-loop", "1", "-i", staged_title_overlay]

        if has_voice:
            cmd_base += ["-i", voice_audio_path]
        if has_bg:
            cmd_base += ["-i", bg_path]

        parts: List[str] = []
        for i in range(n_slides):
            parts.append(_build_slide_branch(i, W, H, motion, per_slide))

        last_label = "[v0]"
        if n_slides >= 2:
            offset = per_slide - trans_dur
            for i in range(1, n_slides):
                out_label = f"[x{i}]"
                parts.append(f"{last_label}[v{i}]xfade=transition={trans}:duration={trans_dur:.3f}:offset={offset:.3f}{out_label}")
                last_label = out_label
                offset += (per_slide - trans_dur)

        parts.append(f"{last_label}format=yuv420p,setsar=1/1,trim=duration={total_video:.3f},setpts=PTS-STARTPTS[v_base]")

        current_v = "[v_base]"
        if use_title and staged_title_overlay:
            idx_title = n_slides
            parts.append(f"[{idx_title}:v]format=rgba,setpts=PTS-STARTPTS[titlev]")
            parts.append(f"[v_base][titlev]overlay=x=(W-w)/2:y=(H-h)/2:shortest=1[v_title]")
            current_v = "[v_title]"

        if legendas and segments:
            font_path_subs = _get_subtitle_font_path(lang_norm)
            logger.info(f"🔤 Fonte das Legendas: {os.path.basename(font_path_subs) if font_path_subs else 'Padrão'}")
            subs_chain = _build_subs_drawtext_chain(
                H, style_norm, segments, font_path_subs,
                rtl=(lang_norm == "ar")
            )
            parts.append(f"{current_v}{subs_chain}[vout]")
        else:
            parts.append(f"{current_v}null[vout]")

        fade_in_dur, fade_out_dur = 0.30, 0.60
        fade_out_start = max(0.0, total_video - fade_out_dur)

        audio_inputs_offset = n_slides + (1 if use_title and staged_title_overlay else 0)

        if has_voice and has_bg:
            idx_voice, idx_bg = audio_inputs_offset, audio_inputs_offset + 1
            v_chain = ["loudnorm=I=-15:TP=-1.0:LRA=11"] if _ffmpeg_has_filter("loudnorm") else []
            v_chain += [f"aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={AUDIO_SR}", f"aresample={AUDIO_SR}:async=1"]
            parts.append(f"[{idx_voice}:a]{','.join(v_chain)},asplit=2[voice_main][voice_sc]")
            parts.append(f"[{idx_bg}:a]volume={BG_MIX_VOLUME},aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={AUDIO_SR},aresample={AUDIO_SR}:async=1[bg]")
            if DUCK_ENABLE and _ffmpeg_has_filter("sidechaincompress"):
                parts.append(f"[bg][voice_sc]sidechaincompress[bg_duck]")
                parts.append(f"[voice_main][bg_duck]amix=inputs=2:duration=first[mixa]")
            else:
                parts.append(f"[voice_main][bg]amix=inputs=2:duration=first[mixa]")
            parts.append(f"[mixa]atrim=end={total_video:.3f},asetpts=PTS-STARTPTS,afade=in:st=0:d={fade_in_dur:.2f},afade=out:st={fade_out_start:.2f}:d={fade_out_dur:.2f}[aout]")
        elif has_voice or has_bg:
            idx = audio_inputs_offset
            chain = ["loudnorm=I=-15:TP=-1.0:LRA=11"] if has_voice and _ffmpeg_has_filter("loudnorm") else [f"volume={BG_MIX_VOLUME}"] if has_bg else []
            chain += [f"aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={AUDIO_SR}", f"aresample={AUDIO_SR}:async=1"]
            parts.append(f"[{idx}:a]{','.join(chain)}[amono]")
            parts.append(f"[amono]atrim=end={total_video:.3f},asetpts=PTS-STARTPTS,afade=in:st=0:d={fade_in_dur:.2f},afade=out:st={fade_out_start:.2f}:d={fade_out_dur:.2f}[aout]")
        else:
            parts.append(f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_SR}:d={total_video:.3f}[aout]")

        filter_complex = ";".join(parts)
        fc_path = os.path.join(CACHE_DIR, "last_filter.txt")
        with open(fc_path, "w", encoding="utf-8") as f:
            f.write(filter_complex)

        common_out = [
            "-r", str(FPS_OUT), "-vsync", "cfr", "-pix_fmt", "yuv420p", "-c:v", "libx264",
            "-preset", "superfast", "-tune", "stillimage", "-b:v", BR_V, "-maxrate", BR_V,
            "-bufsize", "6M", "-profile:v", "high", "-level", LEVEL,
            "-c:a", "aac", "-b:a", BR_A, "-ar", str(AUDIO_SR), "-ac", "2",
            "-movflags", "+faststart+use_metadata_tags",
            "-x264-params", f"keyint={FPS_OUT*2}:min-keyint={FPS_OUT*2}:scenecut=0",
            "-map_metadata", "-1", "-threads", str(max(1, os.cpu_count()//2)),
        ]

        cmd = list(cmd_base) + [
            "-filter_complex_script", fc_path,
            "-map", "[vout]", "-map", "[aout]",
            "-t", f"{total_video:.3f}"
        ] + common_out + [saida_path]

        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            logger.warning("⚠️  -filter_complex_script falhou (%s). Tentando fallback com -filter_complex…", e)
            cmd_fb = list(cmd_base) + [
                "-filter_complex", filter_complex,
                "-map", "[vout]", "-map", "[aout]",
                "-t", f"{total_video:.3f}"
            ] + common_out + [saida_path]
            subprocess.run(cmd_fb, check=True)

        logger.info("✅ Vídeo salvo: %s", saida_path)

    finally:
        # 1) remover cópias staged e quaisquer “extra_to_cleanup”
        for fp in (staged_images + extra_to_cleanup):
            try:
                if fp and os.path.isfile(fp):
                    os.remove(fp)
            except Exception:
                pass

        # 2) remover o TTS staged
        if staged_tts:
            try:
                if os.path.isfile(staged_tts):
                    os.remove(staged_tts)
            except Exception:
                pass

        # 3) remover o overlay de título
        if staged_title_overlay:
            try:
                if os.path.isfile(staged_title_overlay):
                    os.remove(staged_title_overlay)
            except Exception:
                pass

        # 4) remover os SLIDES ORIGINAIS utilizados (apenas se estavam em IMAGES_DIR)
        for orig in original_slides_used or []:
            try:
                if orig and os.path.isfile(orig) and os.path.abspath(IMAGES_DIR) in os.path.abspath(orig):
                    os.remove(orig)
            except Exception:
                pass

        # 5) limpeza de arquivos temporários do CACHE (drawtext_*, title_overlay_* e last_filter.txt)
        try:
            for name in os.listdir(CACHE_DIR):
                if name.startswith("drawtext_") and name.endswith(".txt"):
                    try:
                        os.remove(os.path.join(CACHE_DIR, name))
                    except Exception:
                        pass
                if name.startswith("title_overlay_") and name.endswith(".png"):
                    try:
                        os.remove(os.path.join(CACHE_DIR, name))
                    except Exception:
                        pass
            lf = os.path.join(CACHE_DIR, "last_filter.txt")
            if os.path.isfile(lf):
                try:
                    os.remove(lf)
                except Exception:
                    pass
        except Exception:
            pass

        # 6) remover BG music (opcional via .env CLEANUP_BG_AUDIO=1)
        if CLEANUP_BG_AUDIO and 'bg_audio_to_cleanup' in locals():
            try:
                if bg_audio_to_cleanup and os.path.isfile(bg_audio_to_cleanup):
                    os.remove(bg_audio_to_cleanup)
            except Exception:
                pass
