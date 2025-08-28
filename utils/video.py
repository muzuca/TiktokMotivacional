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

from .frase import gerar_frase_motivacional_longa
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

IMAGES_DIR = os.getenv("IMAGES_DIR", "imagens")
AUDIO_DIR  = os.getenv("AUDIO_DIR", "audios")
AUDIO_TTS_DIR = os.path.join(AUDIO_DIR, "tts")
FONTS_DIR = "fonts"
CACHE_DIR = "cache"
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(AUDIO_TTS_DIR, exist_ok=True)

# -------------- helpers .env --------------
def _clean_env_value(v: Optional[str]) -> str:
    if v is None: return ""
    s = str(v).strip()
    if "#" in s: s = s.split("#", 1)[0]
    return s.strip().strip("'").strip('"').strip()

def _env_float(name: str, default: float) -> float:
    s = _clean_env_value(os.getenv(name)); return float(s) if s else default
def _env_int(name: str, default: int) -> int:
    s = _clean_env_value(os.getenv(name)); return int(float(s)) if s else default
def _env_bool(name: str, default: bool) -> bool:
    s = _clean_env_value(os.getenv(name)).lower()
    if s in ("1","true","yes","on"): return True
    if s in ("0","false","no","off"): return False
    return default
def _env_str(name: str, default: str) -> str:
    s = _clean_env_value(os.getenv(name)); return s if s else default

# -------- áudio / mix / pós --------
BG_MIX_VOLUME      = _env_float("BG_MIX_VOLUME", 0.10)  # <— corrige NameError
DEFAULT_TRANSITION = _env_str("TRANSITION", "fade").lower()
VOICE_LOUDNORM     = _env_bool("VOICE_LOUDNORM", True)
KENBURNS_ZOOM_MAX  = _env_float("KENBURNS_ZOOM_MAX", 1.22)
PAN_ZOOM           = _env_float("PAN_ZOOM", 1.18)
VIDEO_SAT          = _env_float("VIDEO_SAT", 1.00)
VIDEO_CONTRAST     = _env_float("VIDEO_CONTRAST", 1.00)
VIDEO_GAMMA        = _env_float("VIDEO_GAMMA", 1.00)
VIDEO_SHARP        = _env_float("VIDEO_SHARP", 0.00)
VIDEO_GRAIN        = _env_int  ("VIDEO_GRAIN", 0)
VIDEO_CHROMA_SHIFT = _env_int  ("VIDEO_CHROMA_SHIFT", 0)
DUCK_ENABLE        = _env_bool("DUCK_ENABLE", True)
DUCK_THRESH        = _env_float("DUCK_THRESH", 0.05)
DUCK_RATIO         = _env_float("DUCK_RATIO", 8.0)
DUCK_ATTACK_MS     = _env_int  ("DUCK_ATTACK_MS", 20)
DUCK_RELEASE_MS    = _env_int  ("DUCK_RELEASE_MS", 250)
VIDEO_RESPECT_TTS  = _env_bool("VIDEO_RESPECT_TTS", True)
VIDEO_TAIL_PAD     = _env_float("VIDEO_TAIL_PAD", 0.40)
VIDEO_MAX_S        = _env_float("VIDEO_MAX_S", 0.0)

# ====== Estética/escala de texto (espelha imagem.py) ======
IMAGE_TEXT_SCALE           = _env_float("IMAGE_TEXT_SCALE", 1.10)
IMAGE_TEXT_SCALE_MODERN    = _env_float("IMAGE_TEXT_SCALE_MODERN", 0.92)
IMAGE_TEXT_SCALE_MINIMAL   = _env_float("IMAGE_TEXT_SCALE_MINIMAL", 0.95)
IMAGE_TEXT_SCALE_CLASSIC   = _env_float("IMAGE_TEXT_SCALE_CLASSIC", 1.12)
IMAGE_TEXT_UPPER           = _env_bool("IMAGE_TEXT_UPPER", True)
IMAGE_TEXT_OUTLINE_STYLE   = _env_str("IMAGE_TEXT_OUTLINE_STYLE", "shadow")  # shadow|stroke
IMAGE_STROKE_WIDTH         = _env_int("IMAGE_STROKE_WIDTH", 2)
try:
    _dx, _dy = [int(v) for v in (_env_str("IMAGE_SHADOW_OFFSET","2,3").split(","))]
    IMAGE_SHADOW_OFFSET = (_dx, _dy)
except Exception:
    IMAGE_SHADOW_OFFSET = (2, 3)
IMAGE_SHADOW_ALPHA         = _env_int("IMAGE_SHADOW_ALPHA", 170)
IMAGE_HL_COLOR             = _env_str("IMAGE_HL_COLOR", "#F3B34A")
IMAGE_HL_STROKE            = _env_int("IMAGE_HL_STROKE", 1)

# Legendas
SUB_FONT_SCALE_1 = _env_float("SUB_FONT_SCALE_1", 0.050)
SUB_FONT_SCALE_2 = _env_float("SUB_FONT_SCALE_2", 0.056)
SUB_FONT_SCALE_3 = _env_float("SUB_FONT_SCALE_3", 0.044)
REQUIRE_FONTFILE = _env_bool("REQUIRE_FONTFILE", False)

# ================== helpers ffmpeg ==================
def _ffmpeg_or_die() -> str:
    path = shutil.which("ffmpeg")
    if not path: raise RuntimeError("ffmpeg não encontrado no PATH.")
    return path
def _ffprobe_or_die() -> str:
    path = shutil.which("ffprobe")
    if not path: raise RuntimeError("ffprobe não encontrado no PATH.")
    return path
def _ffmpeg_has_filter(filter_name: str) -> bool:
    try:
        out = subprocess.check_output([_ffmpeg_or_die(), "-hide_banner", "-filters"], stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
        return any(re.search(rf"\b{re.escape(filter_name)}\b", line) for line in out.splitlines())
    except Exception:
        return False

# ================== helpers gerais ==================
def _idioma_norm(idioma: str) -> str:
    s = (idioma or "en").lower()
    if s.startswith("pt"): return "pt"
    if s.startswith("ar"): return "ar"
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

_IS_WIN = (os.name == "nt")
def _ff_escape_filter_path(p: str) -> str:
    s = p.replace("\\", "/").replace("'", r"\'")
    if _IS_WIN and re.match(r"^[A-Za-z]:/", s): return s.replace(":", r"\\:")
    return s
def _ff_q(val: str) -> str:
    return f"'{_ff_escape_filter_path(val)}'"
def _uuid_suffix() -> str:
    return uuid.uuid4().hex[:8]
def _stage_to_dir(src_path: str, target_dir: str, prefix: str) -> str:
    os.makedirs(target_dir, exist_ok=True)
    base, ext = os.path.splitext(os.path.basename(src_path))
    dst_name = f"{prefix}_{base}_{_uuid_suffix()}{ext or '.jpg'}"
    dst_path = os.path.join(target_dir, dst_name)
    shutil.copy2(src_path, dst_path)
    return dst_path

# ================== motion (suavizado) ==================
def _smoothstep_expr(p: str) -> str:
    return f"(({p})*({p})*(3-2*({p})))"

def _kb_in(W,H,F):
    p=f"(on/{F})"; ps=_smoothstep_expr(p)
    z=f"(1+({KENBURNS_ZOOM_MAX:.5f}-1)*{ps})"
    return f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={F}:s={W}x{H}:fps={FPS_OUT}"

def _kb_out(W,H,F):
    p=f"(on/{F})"; ps=_smoothstep_expr(p)
    z=f"({KENBURNS_ZOOM_MAX:.5f}+(1-{KENBURNS_ZOOM_MAX:.5f})*{ps})"
    return f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={F}:s={W}x{H}:fps={FPS_OUT}"

def _pan_lr(W,H,F):
    p=f"(on/{F})"; ps=_smoothstep_expr(p)
    return f"zoompan=z={PAN_ZOOM:.5f}:x='(iw/zoom-ow)*{ps}':y='(ih/zoom-oh)/2':d={F}:s={W}x{H}:fps={FPS_OUT}"

def _pan_ud(W,H,F):
    p=f"(on/{F})"; ps=_smoothstep_expr(p)
    return f"zoompan=z={PAN_ZOOM:.5f}:x='(iw/zoom-ow)/2':y='(ih/zoom-oh)*{ps}':d={F}:s={W}x{H}:fps={FPS_OUT}"

def _build_slide_branch(idx: int, W: int, H: int, motion: str, per_slide: float) -> str:
    F = max(1, int(round(per_slide * FPS_OUT)))
    m = (motion or "none").lower()
    motion_map = {
        "kenburns_in": _kb_in, "2": _kb_in,
        "kenburns_out": _kb_out, "3": _kb_out,
        "pan_lr": _pan_lr, "4": _pan_lr,
        "pan_ud": _pan_ud, "5": _pan_ud,
    }
    func = motion_map.get(m)
    if func:
        expr = func(W, H, F)
        return f"[{idx}:v]{expr},format=yuv420p,setsar=1/1[v{idx}]"

    return (f"[{idx}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,format=yuv420p,setsar=1/1[v{idx}]")

# ================== fontes / overlay do título ==================
def _normalize_style(style: str) -> str:
    s = (style or "1").strip().lower()
    return str({"classic": "1", "modern": "2", "serif": "3", "mono": "4", "clean": "5"}.get(s, s if s in "12345" else "1"))
def _first_existing_font(*names: str) -> Optional[str]:
    for n in names:
        p = os.path.join(FONTS_DIR, n)
        if os.path.isfile(p): return p
    for p in ["/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "C:\\Windows\\Fonts\\arial.ttf"]:
        if os.path.isfile(p): return p
    return None
def _pick_font_for_style(style_key: str, is_main_phrase: bool) -> List[str]:
    s = _normalize_style(style_key)
    if s == "1": return ["Montserrat-Bold.ttf","Inter-Bold.ttf","Montserrat-ExtraBold.ttf"]
    if s == "2": return ["BebasNeue-Regular.ttf","Montserrat-ExtraBold.ttf","Inter-Black.ttf"]
    if s == "3": return ["PlayfairDisplay-Bold.ttf","Cinzel-Bold.ttf","Inter-Bold.ttf"]
    if s == "4": return ["RobotoMono-Regular.ttf","DejaVuSansMono.ttf","Inter-Regular.ttf"]
    if s == "5": return ["Inter-SemiBold.ttf","Montserrat-SemiBold.ttf","Inter-Bold.ttf"]
    return ["Montserrat-Bold.ttf","Inter-Bold.ttf"]
def _style_fontsize_from_H(H: int, style_id: str) -> Tuple[int, int, int]:
    sid = _normalize_style(style_id)
    if sid == "1": scale, margin_pct = SUB_FONT_SCALE_1, 0.12
    elif sid == "2": scale, margin_pct = SUB_FONT_SCALE_2, 0.125
    else: scale, margin_pct = SUB_FONT_SCALE_3, 0.13
    fs = max(18, int(H * scale)); borderw = max(1, int(fs * 0.05)); margin = max(58, int(H * margin_pct))
    return fs, borderw, margin
def _abs_font_path(fname: str) -> str: return os.path.abspath(os.path.join(FONTS_DIR, fname))
def _load_font_any(candidates: List[str], size: int) -> ImageFont.FreeTypeFont:
    for c in candidates:
        p = c if os.path.isabs(c) else _abs_font_path(c)
        if os.path.isfile(p):
            try: return ImageFont.truetype(p, size=size)
            except Exception: continue
    try: return ImageFont.truetype("arial.ttf", size=size)
    except Exception: return ImageFont.load_default()
def _hex_to_rgba(hex_color: str, alpha: int = 255) -> Tuple[int,int,int,int]:
    s = (hex_color or "").strip().lstrip("#")
    if len(s) == 3: s = "".join(ch*2 for ch in s)
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        return (r,g,b,alpha)
    except Exception: return (243,179,74,alpha)

def _tokenize_with_markup(text: str) -> List[Tuple[str, bool]]:
    """Divide a string com **markup** em pares (segmento, highlighted?)."""
    tokens: List[Tuple[str, bool]] = []
    for part in re.split(r'(\*\*.*?\*\*)', text):
        if not part: continue
        if part.startswith("**") and part.endswith("**"):
            tokens.append((part[2:-2], True))
        else:
            tokens.append((part, False))
    return tokens

def _measure(draw: ImageDraw.ImageDraw, s: str, font: ImageFont.FreeTypeFont) -> int:
    return draw.textbbox((0,0), s, font=font)[2]

def _best_font_and_wrap(draw: ImageDraw.ImageDraw, text_no_markup: str, font_candidates: List[str], maxw: int, min_size: int, max_size: int, max_lines: int = 4) -> Tuple[ImageFont.FreeTypeFont, List[str]]:
    words = text_no_markup.split()
    best_font = _load_font_any(font_candidates, min_size); best_lines: List[str] = []
    def wrap_with(f: ImageFont.FreeTypeFont) -> List[str]:
        lines: List[str] = []; cur: List[str] = []
        def get_w(ws: List[str]) -> int: return 0 if not ws else _measure(draw, " ".join(ws), f)
        for w_ in words:
            if not cur: cur=[w_]; continue
            if get_w(cur+[w_]) <= maxw: cur.append(w_)
            else: lines.append(" ".join(cur)); cur=[w_]
        if cur: lines.append(" ".join(cur))
        return lines
    lo, hi = min_size, max_size
    while lo <= hi:
        mid = (lo + hi)//2
        f = _load_font_any(font_candidates, mid)
        lines = wrap_with(f)
        ok = (len(lines) <= max_lines) and (max((_measure(draw, ln, f) for ln in lines), default=0) <= maxw)
        if ok: best_font, best_lines = f, lines; lo = mid + 2
        else: hi = mid - 2
    if not best_lines: best_lines = wrap_with(best_font)
    return best_font, best_lines

def _draw_text_with_style(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, font: ImageFont.FreeTypeFont, *, fill=(255,255,255,255), stroke=False, stroke_w=2, shadow=False, shadow_xy=(2,3), shadow_a=170):
    if shadow: draw.text((x+shadow_xy[0], y+shadow_xy[1]), text, font=font, fill=(0,0,0,shadow_a))
    if stroke and stroke_w > 0:
        for dx in range(-stroke_w, stroke_w + 1, stroke_w):
            for dy in range(-stroke_w, stroke_w + 1, stroke_w):
                if dx != 0 or dy != 0:
                    draw.text((x + dx, y + dy), text, font=font, fill=(0,0,0,220))
    draw.text((x, y), text, font=font, fill=fill)

def _title_overlay_png(text: str, style_id: str, idioma: str, W: int, H: int) -> str:
    """
    Gera um PNG transparente com o mesmo visual usado em imagem.py:
    - mesmas quebras de linha
    - mesma família/tamanho de fonte por estilo
    - mesmo destaque de **palavras** via cor IMAGE_HL_COLOR
    """
    img = Image.new("RGBA", (W, H), (0,0,0,0))
    draw = ImageDraw.Draw(img)

    is_ar = (_idioma_norm(idioma) == "ar")
    raw = (text or "").strip()
    raw = raw if is_ar else (raw.upper() if IMAGE_TEXT_UPPER else raw)

    # tokens mantendo o markup
    tokens = _tokenize_with_markup(raw)
    # string limpa (sem **), usada para cálculo/empacotamento
    clean = "".join(t for (t, _) in tokens)

    sid = _normalize_style(style_id)
    scale_mult = IMAGE_TEXT_SCALE_CLASSIC if sid == "1" else (IMAGE_TEXT_SCALE_MODERN if sid == "2" else IMAGE_TEXT_SCALE_MINIMAL)
    scls = IMAGE_TEXT_SCALE * scale_mult
    font_candidates = _pick_font_for_style(style_id, True)
    max_w = int(W * 0.84)

    font, lines = _best_font_and_wrap(
        draw, clean, font_candidates,
        maxw=max_w, min_size=int(60*scls), max_size=int(110*scls), max_lines=4
    )

    # métricas verticais
    line_heights = [draw.textbbox((0,0), ln, font=font, stroke_width=IMAGE_STROKE_WIDTH)[3] for ln in lines]
    total_h = sum(line_heights) + int(font.size * 0.15)*(len(lines)-1)
    y = (H - total_h)//2

    base_fill = (255,255,255,255)
    hl_fill   = _hex_to_rgba(IMAGE_HL_COLOR, 255)

    # cursor nos tokens para desenhar nas linhas embrulhadas
    tok_i = 0
    tok_off = 0  # offset dentro do texto do token atual

    for i, ln in enumerate(lines):
        line_w = _measure(draw, ln, font)
        x = (W - line_w)//2
        remaining = ln

        while remaining:
            tok_text, tok_hl = tokens[tok_i]
            # segmento remanescente do token atual
            seg = tok_text[tok_off:]
            if not seg:
                tok_i += 1; tok_off = 0
                continue
            # decide quanto desse token cabe no prefixo de 'remaining'
            # (como 'lines' é derivado de 'clean', a concatenação dos tokens == lines)
            take = min(len(seg), len(remaining))
            chunk = seg[:take]
            # desenha este pedacinho
            _draw_text_with_style(
                draw, x, y, chunk, font,
                fill=(hl_fill if tok_hl else base_fill),
                stroke=(IMAGE_TEXT_OUTLINE_STYLE == "stroke"),
                stroke_w=IMAGE_STROKE_WIDTH,
                shadow=(IMAGE_TEXT_OUTLINE_STYLE == "shadow"),
                shadow_xy=IMAGE_SHADOW_OFFSET,
                shadow_a=IMAGE_SHADOW_ALPHA
            )
            # avança
            x += _measure(draw, chunk, font)
            remaining = remaining[take:]
            tok_off += take
            if tok_off >= len(tok_text):
                tok_i += 1; tok_off = 0

        y += line_heights[i] + int(font.size * 0.15)

    out_path = os.path.join(CACHE_DIR, f"title_overlay_{_uuid_suffix()}.png")
    img.save(out_path, "PNG")
    return out_path

# ================== legendas (drawtext) ==================
def _write_textfile_for_drawtext(content: str, idx: int) -> str:
    path = os.path.join(CACHE_DIR, f"drawtext_{idx:02d}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(re.sub(r"\s+", " ", content.strip()))
    return path

def _get_subtitle_font_path(lang_norm: str) -> Optional[str]:
    # pedido: sempre BebasNeue para legendas (exceto árabe)
    if lang_norm == "ar":
        p = os.path.join(FONTS_DIR, _env_str("ARABIC_FONT", "NotoNaskhArabic-Regular.ttf"))
        return p if os.path.isfile(p) else _first_existing_font("NotoNaskhArabic-Regular.ttf", "NotoNaskhArabic-Bold.ttf")
    return _first_existing_font("BebasNeue-Regular.ttf", "Inter-Bold.ttf", "Montserrat-Bold.ttf")

def _build_subs_drawtext_chain(H: int, style_id: str, segments: List[Tuple[float, float, str]], font_path: Optional[str]) -> str:
    fs, borderw, margin = _style_fontsize_from_H(H, style_id)
    font_opt = f":fontfile={_ff_q(font_path)}" if font_path and os.path.isfile(font_path) else ""
    if not font_opt and REQUIRE_FONTFILE:
        raise RuntimeError("Fonte de legendas não encontrada. Coloque o TTF/OTF em ./fonts.")
    blocks = [f"drawtext=textfile={_ff_q(_write_textfile_for_drawtext(txt, idx))}"
              f"{font_opt}:fontsize={fs}:fontcolor=white:borderw={borderw}:"
              f"bordercolor=black@0.85:shadowcolor=black@0.7:shadowx=2:shadowy=2:x=(w-text_w)/2:"
              f"y=h-(text_h+{margin}):enable='between(t,{ini:.3f},{fim:.3f})'"
              for idx, (ini, fim, txt) in enumerate(segments, start=1)]
    return ",".join(blocks)

# ================== pipeline principal ==================
def gerar_video(
    imagem_path: str,
    saida_path: str,
    *,
    frase_principal: str = "",
    preset: str = "hd",
    idioma: str = "auto",
    tts_engine: str = "gemini",
    legendas: bool = True,
    video_style: str = "1",
    motion: str = "none",
    slides_paths: Optional[List[str]] = None,
    transition: Optional[str] = None,
    content_mode: str = "motivacional"
):
    staged_images: List[str] = []; staged_tts: Optional[str] = None
    staged_title_overlay: Optional[str] = None; extra_to_cleanup: List[str] = []

    try:
        conf = PRESETS.get(preset, PRESETS["hd"])
        W, H = conf["w"], conf["h"]; BR_V, BR_A, LEVEL = conf["br_v"], conf["br_a"], conf["level"]
        slides_validos = [p for p in (slides_paths or [imagem_path]) if p and os.path.isfile(p)]
        if not slides_validos: raise FileNotFoundError("Nenhuma imagem de slide válida foi fornecida.")
        n_slides = len(slides_validos)
        lang_norm = _idioma_norm(idioma)
        if _text_contains_arabic(frase_principal): lang_norm = "ar"
        style_norm = _normalize_style(video_style)

        long_text = gerar_frase_tarot_longa(idioma) if (content_mode or "").lower() == "tarot" and _HAVE_TAROT_LONG else gerar_frase_motivacional_longa(idioma)
        voice_audio_path = gerar_narracao_tts(long_text, idioma=lang_norm, engine=tts_engine)
        dur_voz = _duracao_audio_segundos(voice_audio_path)
        logger.info("🎙️ Duração da voz (ffprobe): %.2fs", dur_voz or 0.0)

        if voice_audio_path:
            staged_tts = _stage_to_dir(voice_audio_path, AUDIO_TTS_DIR, "tts")
            if os.path.basename(os.path.dirname(voice_audio_path)).lower() in ("audios_tts", "tts"):
                extra_to_cleanup.append(voice_audio_path)
            voice_audio_path = staged_tts
        background_audio_path = obter_caminho_audio(idioma=lang_norm)
        has_voice, has_bg = bool(voice_audio_path), bool(background_audio_path)

        segments = make_segments_for_audio(long_text, voice_audio_path, idioma=lang_norm) if legendas and has_voice else []
        if segments: logger.info("📝 %d segmentos de legenda gerados.", len(segments))

        total_video = (dur_voz or 12.0) + VIDEO_TAIL_PAD if has_voice and VIDEO_RESPECT_TTS else 12.0
        if VIDEO_MAX_S > 0: total_video = min(total_video, VIDEO_MAX_S)

        trans = transition or DEFAULT_TRANSITION or "fade"
        trans_dur = max(0.45, min(0.85, (total_video / n_slides) * 0.135)) if n_slides > 1 else 0.0
        per_slide = (total_video + (n_slides - 1) * trans_dur) / n_slides if n_slides > 0 else 0

        cmd_base = [_ffmpeg_or_die(), "-y", "-loglevel", "error", "-stats"]
        staged_inputs = [_stage_to_dir(p, IMAGES_DIR, "stage") for p in slides_validos]
        staged_images.extend(staged_inputs)

        # garante CFR para xfade
        for sp in staged_inputs:
            cmd_base += ["-framerate", str(FPS_OUT), "-loop", "1", "-i", sp]

        use_title = bool(frase_principal and frase_principal.strip())
        if use_title:
            logger.info("✍️  Gerando overlay de título (PNG transparente)…")
            staged_title_overlay = _title_overlay_png(frase_principal, style_norm, lang_norm, W, H)
            cmd_base += ["-framerate", str(FPS_OUT), "-loop", "1", "-i", staged_title_overlay]

        if has_voice: cmd_base += ["-i", voice_audio_path]
        if has_bg:    cmd_base += ["-i", background_audio_path]

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

        look_ops = []
        if any(v != 1.0 for v in [VIDEO_SAT, VIDEO_CONTRAST, VIDEO_GAMMA]):
            look_ops.append(f"eq=saturation={VIDEO_SAT:.3f}:contrast={VIDEO_CONTRAST:.3f}:gamma={VIDEO_GAMMA:.3f}")
        if VIDEO_SHARP > 0: look_ops.append(f"unsharp=3:3:{VIDEO_SHARP:.3f}:3:3:0.0")
        if VIDEO_GRAIN > 0: look_ops.append(f"noise=alls={VIDEO_GRAIN}:allf=t")
        if VIDEO_CHROMA_SHIFT != 0 and _ffmpeg_has_filter("chromashift"):
            look_ops.append(f"chromashift=cbh={int(VIDEO_CHROMA_SHIFT)}:crh={-int(VIDEO_CHROMA_SHIFT)}")

        filters = [op for op in look_ops if op] + ["format=yuv420p,setsar=1/1"]
        parts.append(f"{last_label}{','.join(filters)},trim=duration={total_video:.3f},setpts=PTS-STARTPTS[v_base]")

        current_v = "[v_base]"
        if use_title and staged_title_overlay:
            idx_title = n_slides
            parts.append(f"[{idx_title}:v]format=rgba,setpts=PTS-STARTPTS[titlev]")
            parts.append(f"{current_v}[titlev]overlay=x=(W-w)/2:y=(H-h)/2:shortest=1[v_title]")
            current_v = "[v_title]"

        if legendas and segments:
            font_path_subs = _get_subtitle_font_path(lang_norm)
            logger.info(f"🔤 Fonte das Legendas: {os.path.basename(font_path_subs) if font_path_subs else 'Padrão'}")
            parts.append(f"{current_v}{_build_subs_drawtext_chain(H, style_norm, segments, font_path_subs)}[vout]")
        else:
            parts.append(f"{current_v}null[vout]")

        fade_in_dur, fade_out_dur = 0.30, 0.60
        fade_out_start = max(0.0, total_video - fade_out_dur)

        audio_inputs_offset = n_slides + (1 if use_title and staged_title_overlay else 0)
        if has_voice and has_bg:
            idx_voice, idx_bg = audio_inputs_offset, audio_inputs_offset + 1
            v_chain = ["loudnorm=I=-15:TP=-1.0:LRA=11"] if VOICE_LOUDNORM and _ffmpeg_has_filter("loudnorm") else []
            v_chain += [f"aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={AUDIO_SR}", f"aresample={AUDIO_SR}:async=1"]
            parts.append(f"[{idx_voice}:a]{','.join(v_chain)},asplit=2[voice_main][voice_sc]")
            parts.append(f"[{idx_bg}:a]volume={BG_MIX_VOLUME},aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={AUDIO_SR},aresample={AUDIO_SR}:async=1[bg]")
            if DUCK_ENABLE and _ffmpeg_has_filter("sidechaincompress"):
                parts.append(f"[bg][voice_sc]sidechaincompress=threshold={DUCK_THRESH}:ratio={DUCK_RATIO}:attack={DUCK_ATTACK_MS}:release={DUCK_RELEASE_MS}[bg_duck]")
                parts.append(f"[voice_main][bg_duck]amix=inputs=2:duration=first:dropout_transition=0[mixa]")
            else:
                parts.append(f"[voice_main][bg]amix=inputs=2:duration=first:dropout_transition=0[mixa]")
            parts.append(f"[mixa]atrim=end={total_video:.3f},asetpts=PTS-STARTPTS,afade=in:st=0:d={fade_in_dur:.2f},afade=out:st={fade_out_start:.2f}:d={fade_out_dur:.2f}[aout]")
        elif has_voice or has_bg:
            idx = audio_inputs_offset
            chain = []
            if has_voice and VOICE_LOUDNORM and _ffmpeg_has_filter("loudnorm"): chain.append("loudnorm=I=-15:TP=-1.0:LRA=11")
            elif has_bg: chain.append(f"volume={BG_MIX_VOLUME}")
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
            "-r", str(FPS_OUT), "-vsync", "cfr", "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "superfast", "-tune", "stillimage",
            "-b:v", BR_V, "-maxrate", BR_V, "-bufsize", "6M",
            "-profile:v", "high", "-level", LEVEL,
            "-c:a", "aac", "-b:a", BR_A, "-ar", str(AUDIO_SR), "-ac", "2",
            "-movflags", "+faststart+use_metadata_tags",
            "-x264-params", f"keyint={FPS_OUT*2}:min-keyint={FPS_OUT*2}:scenecut=0",
            "-map_metadata", "-1",
            "-threads", str(max(1, os.cpu_count()//2)),
        ]

        # 1ª tentativa: usar script (evita limites do cmd no Windows)
        cmd = list(cmd_base) + ["-filter_complex_script", fc_path, "-map", "[vout]", "-map", "[aout]", "-t", f"{total_video:.3f}"] + common_out + [saida_path]
        logger.info("🎬 FFmpeg:\n%s", " ".join(cmd))
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            # Fallback para -filter_complex (algumas builds antigas não entendem *_script)
            logger.warning("⚠️  -filter_complex_script falhou (%s). Tentando fallback com -filter_complex…", e)
            cmd_fb = list(cmd_base) + ["-filter_complex", filter_complex, "-map", "[vout]", "-map", "[aout]", "-t", f"{total_video:.3f}"] + common_out + [saida_path]
            subprocess.run(cmd_fb, check=True)

        logger.info("✅ Vídeo salvo: %s", saida_path)

    finally:
        for fp in staged_images + extra_to_cleanup:
            try: os.remove(fp)
            except Exception: pass
        if staged_tts:
            try: os.remove(staged_tts)
            except Exception: pass
        if staged_title_overlay:
            try: os.remove(staged_title_overlay)
            except Exception: pass
