# utils/video.py

import os
import re
import logging
import datetime
import shutil
import subprocess
import uuid
from typing import Optional, List, Tuple

from dotenv import load_dotenv
from .frase import gerar_frase_motivacional_longa
try:
    from .frase import gerar_frase_tarot_longa
    _HAVE_TAROT_LONG = True
except Exception:
    _HAVE_TAROT_LONG = False

from .audio import obter_caminho_audio, gerar_narracao_tts
from .subtitles import make_segments_for_audio  # 2–3 palavras/1 linha

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

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

# ================= ENV helpers =================
def _clean_env_value(v: Optional[str]) -> str:
    """Remove comentário inline (# ...) e aspas ao redor."""
    if v is None:
        return ""
    s = str(v).strip()
    # corta em '#', se existir (comentário inline)
    if "#" in s:
        s = s.split("#", 1)[0]
    # remove aspas simples/duplas externas
    s = s.strip().strip("'").strip('"').strip()
    return s

def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    s = _clean_env_value(raw)
    if s == "":
        return default
    try:
        return float(s)
    except Exception:
        return default

def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    s = _clean_env_value(raw)
    if s == "":
        return default
    try:
        return int(float(s))
    except Exception:
        return default

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    s = _clean_env_value(raw).lower()
    if s in ("1","true","yes","on"):  return True
    if s in ("0","false","no","off"): return False
    return default

def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    s = _clean_env_value(raw)
    return s if s != "" else default

# =============== ENV (audio/look) ===============
BG_MIX_VOLUME      = _env_float("BG_MIX_VOLUME", 0.10)
DEFAULT_TRANSITION = _env_str("TRANSITION", "fade").lower()
VOICE_LOUDNORM     = _env_bool("VOICE_LOUDNORM", True)

KENBURNS_ZOOM_MAX = _env_float("KENBURNS_ZOOM_MAX", 1.22)
PAN_ZOOM          = _env_float("PAN_ZOOM", 1.18)

_MOTION_FPS_ENV = _env_int("MOTION_FPS", 45)
MOTION_FPS = max(24, min(90, _MOTION_FPS_ENV))
if MOTION_FPS != _MOTION_FPS_ENV:
    logger.info("ℹ️ MOTION_FPS ajustado para %d (cap 24..90).", MOTION_FPS)

VIDEO_SAT          = _env_float("VIDEO_SAT", 1.00)
VIDEO_CONTRAST     = _env_float("VIDEO_CONTRAST", 1.00)
VIDEO_GAMMA        = _env_float("VIDEO_GAMMA", 1.00)
VIDEO_SHARP        = _env_float("VIDEO_SHARP", 0.00)
VIDEO_GRAIN        = _env_int  ("VIDEO_GRAIN", 0)
VIDEO_CHROMA_SHIFT = _env_int  ("VIDEO_CHROMA_SHIFT", 0)

DUCK_ENABLE     = _env_bool("DUCK_ENABLE", True)
DUCK_THRESH     = _env_float("DUCK_THRESH", 0.05)
DUCK_RATIO      = _env_float("DUCK_RATIO", 8.0)
DUCK_ATTACK_MS  = _env_int  ("DUCK_ATTACK_MS", 20)
DUCK_RELEASE_MS = _env_int  ("DUCK_RELEASE_MS", 250)

# =============== Whisper/faster-whisper ===============
WHISPER_MODEL = _env_str("WHISPER_MODEL", "base")
WHISPER_DEVICE = _env_str("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = _env_str("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_BEAM_SIZE = _env_int("WHISPER_BEAM_SIZE", 1)
WHISPER_VAD = _env_bool("WHISPER_VAD", True)
WHISPER_MODEL_CACHE = _env_str("WHISPER_MODEL_CACHE", "./whisper_models")

# =============== Metadados ===============
META_SPOOF_ENABLE    = _env_bool("META_SPOOF_ENABLE", False)
META_MAKE            = _env_str("META_MAKE", "Apple")
META_MODEL           = _env_str("META_MODEL", "iPhone 13")
META_SOFTWARE        = _env_str("META_SOFTWARE", "iOS 17.5.1")
META_LOCATION_ISO6709= _env_str("META_LOCATION_ISO6709", "+37.7749-122.4194+000.00/")

VIDEO_RESPECT_TTS = _env_bool("VIDEO_RESPECT_TTS", True)
VIDEO_TAIL_PAD    = _env_float("VIDEO_TAIL_PAD", 0.40)
VIDEO_MAX_S       = _env_float("VIDEO_MAX_S", 0.0)
TPAD_EPS          = _env_float("TPAD_EPS", 0.25)

# =============== RTL/ASS ===============
SUBS_USE_ASS_FOR_RTL   = _env_bool("SUBS_USE_ASS_FOR_RTL", True)
ARABIC_FONT            = _env_str("ARABIC_FONT", "NotoNaskhArabic-Regular.ttf")
SUBS_ASS_BASE_FONTSIZE = _env_int("SUBS_ASS_BASE_FONTSIZE", 36)
SUBS_ASS_SCALE         = _env_float("SUBS_ASS_SCALE", 0.030)  # teto: % da altura do vídeo
SUBS_ASS_ALIGNMENT     = _env_int("SUBS_ASS_ALIGNMENT", 2)  # 1..9 (ASS). 2 = bottom-center
META_SOFTWARE_FALLBACK = _env_str("META_SOFTWARE_FALLBACK", "")

# =============== Fonte/estilo da legenda (drawtext) ===============
VIDEO_STYLES = {
    "1": {"key": "minimal_compact", "label": "Compact (sem caixa, contorno fino)"},
    "2": {"key": "clean_outline",   "label": "Clean (um pouco maior)"},
    "3": {"key": "tiny_outline",    "label": "Tiny (bem pequeno)"},
    "classic": "1", "modern": "2", "serif": "2", "clean": "2", "mono": "3",
}
SUB_FONT_SCALE_1 = _env_float("SUB_FONT_SCALE_1", 0.020)
SUB_FONT_SCALE_2 = _env_float("SUB_FONT_SCALE_2", 0.024)
SUB_FONT_SCALE_3 = _env_float("SUB_FONT_SCALE_3", 0.018)
REQUIRE_FONTFILE = _env_bool("REQUIRE_FONTFILE", False)  # se True e não achar TTF, aborta

def _normalize_style(style: str) -> str:
    s = (style or "1").strip().lower()
    if s in ("1","2","3"): return s
    return str(VIDEO_STYLES.get(s, "1"))

def _first_existing_font(*names: str) -> Optional[str]:
    for n in names:
        p = os.path.join(FONTS_DIR, n)
        if os.path.isfile(p):
            return p
    return None

def _pick_drawtext_font(style_key: str) -> Optional[str]:
    # override via env
    override = _env_str("FORCE_SUB_FONT", "")
    if override:
        path = os.path.join(FONTS_DIR, override)
        if os.path.isfile(path):
            return path
        logger.warning("FORCE_SUB_FONT=%s não encontrado em %s", override, FONTS_DIR)

    s = (style_key or "").lower()
    if s in ("classic", "1", "clean", "5"):
        return _first_existing_font("BebasNeue-Regular.ttf","Inter-Bold.ttf","Montserrat-Regular.ttf")
    if s in ("modern", "2"):
        return _first_existing_font("BebasNeue-Regular.ttf","Inter-Bold.ttf","Montserrat-ExtraBold.ttf")
    if s in ("serif", "3"):
        return _first_existing_font("PlayfairDisplay-Regular.ttf","Cinzel-Bold.ttf","Inter-Bold.ttf")
    if s in ("mono", "4"):
        return _first_existing_font("Inter-Bold.ttf","Montserrat-Regular.ttf")
    return _first_existing_font("BebasNeue-Regular.ttf","Inter-Bold.ttf","Montserrat-Regular.ttf")

# =============== FFmpeg helpers ===============
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
        out = subprocess.check_output([_ffmpeg_or_die(), "-hide_banner", "-filters"],
                                      stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
        return any(re.search(rf"\b{re.escape(filter_name)}\b", line) for line in out.splitlines())
    except Exception:
        return False

# =============== Idioma helpers ===============
def _idioma_norm(idioma: str) -> str:
    s = (idioma or "en").lower()
    if s.startswith("pt"): return "pt"
    if s.startswith("ar"): return "ar"
    return "en"
def _lang_is_rtl(lang: str) -> bool:
    return lang in ("ar",)
def _text_contains_arabic(s: str) -> bool:
    """Detecta pontos de código relevantes para escrita árabe."""
    return bool(re.search(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]', s or ""))

# =============== Diversos ===============
def _duracao_audio_segundos(a: str) -> Optional[float]:
    if not a or not os.path.isfile(a): return None
    try:
        out = subprocess.check_output([_ffprobe_or_die(), "-v","error","-show_entries","format=duration",
                                       "-of","default=noprint_wrappers=1:nokey=1", a], text=True).strip()
        dur = float(out)
        return dur if dur > 0 else None
    except Exception:
        return None
def _ff_normpath(path: str) -> str:
    try: rel = os.path.relpath(path)
    except Exception: rel = path
    return rel.replace("\\","/")
_IS_WIN = (os.name == "nt")
def _ff_escape_filter_path(p: str) -> str:
    s = _ff_normpath(p)
    if _IS_WIN and re.match(r"^[A-Za-z]:/", s): s = s[0] + r"\:" + s[2:]
    return s.replace("'", r"\'")
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

# =============== Motion helpers ===============
def _smoothstep_expr(p: str) -> str:
    return f"(({p})*({p})*(3-2*({p})))"
def _kb_in(W,H,F):
    p=f"(on/{F})"; ps=_smoothstep_expr(p); z=f"(1+({KENBURNS_ZOOM_MAX:.3f}-1)*{ps})"
    return f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}"
def _kb_out(W,H,F):
    p=f"(on/{F})"; ps=_smoothstep_expr(p); z=f"({KENBURNS_ZOOM_MAX:.3f} + (1-{KENBURNS_ZOOM_MAX:.3f})*{ps})"
    return f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}"
def _pan_lr(W,H,F):
    p=f"(on/{F})"; ps=_smoothstep_expr(p)
    return f"zoompan=z={PAN_ZOOM:.3f}:x='(iw/zoom-ow)*{ps}':y='(ih/zoom-oh)/2':d=1:s={W}x{H}"
def _pan_ud(W,H,F):
    p=f"(on/{F})"; ps=_smoothstep_expr(p)
    return f"zoompan=z={PAN_ZOOM:.3f}:x='(iw/zoom-ow)/2':y='(ih/zoom-oh)*{ps}':d=1:s={W}x{H}"

def _build_slide_branch(idx: int, W: int, H: int, motion: str, per_slide: float) -> str:
    F = max(1, int(per_slide * MOTION_FPS))
    m = (motion or "none").lower()
    if   m in ("kenburns_in","kenburns-in","zoom_in","zoom-in","2"): expr = _kb_in(W,H,F)
    elif m in ("kenburns_out","kenburns-out","zoom_out","zoom-out","3"): expr = _kb_out(W,H,F)
    elif m in ("pan_lr","pan-left-right","4","pan left→right","pan left->right"): expr = _pan_lr(W,H,F)
    elif m in ("pan_ud","pan-up-down","5","pan up→down","pan up->down"): expr = _pan_ud(W,H,F)
    else:
        return (f"[{idx}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,format=yuv420p,setsar=1/1,"
                f"trim=duration={per_slide:.6f},setpts=PTS-STARTPTS,fps={MOTION_FPS}[v{idx}]")
    return (f"[{idx}:v]{expr},format=yuv420p,setsar=1/1,trim=duration={per_slide:.6f},"
            f"setpts=PTS-STARTPTS,fps={MOTION_FPS}[v{idx}]")

# =============== Legendas (drawtext) ===============
def _style_fontsize_from_H(H: int, style_id: str) -> Tuple[int, int, int]:
    sid = _normalize_style(style_id)
    if sid == "1":
        fs = max(18, int(H * SUB_FONT_SCALE_1)); borderw = 2; margin = max(58, int(H * 0.12))
    elif sid == "2":
        fs = max(18, int(H * SUB_FONT_SCALE_2)); borderw = 2; margin = max(60, int(H * 0.125))
    else:
        fs = max(16, int(H * SUB_FONT_SCALE_3)); borderw = 2; margin = max(62, int(H * 0.13))
    return fs, borderw, margin

def _write_textfile_one_line(content: str, idx: int) -> str:
    path = os.path.join(CACHE_DIR, f"drawtext_{idx:02d}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(re.sub(r"\s+", " ", content.strip()))
    return path

def _build_drawtext_chain(H: int, style_id: str, segments: List[Tuple[float, float, str]], font_path: Optional[str]) -> str:
    fs, borderw, margin = _style_fontsize_from_H(H, style_id)
    use_fontfile = (font_path and os.path.isfile(font_path))
    if not use_fontfile:
        msg = "⚠️ Nenhuma fonte específica encontrada; usando fonte do sistema (pode ficar feio)."
        if REQUIRE_FONTFILE:
            raise RuntimeError("Fonte de legenda não encontrada em ./fonts; defina FORCE_SUB_FONT ou adicione TTF.")
        logger.warning(msg)
    font_opt = f":fontfile={_ff_q(font_path)}" if use_fontfile else ""

    blocks = []
    for idx, (ini, fim, txt) in enumerate(segments, start=1):
        tf_posix = _ff_normpath(_write_textfile_one_line(txt, idx))
        tf_q = _ff_q(tf_posix)
        block = (
            "drawtext="
            f"textfile={tf_q}"
            f"{font_opt}"
            f":fontsize={fs}"
            f":fontcolor=white"
            f":borderw={borderw}:bordercolor=black"
            f":shadowcolor=black:shadowx=2:shadowy=2"
            f":box=0"
            f":x=(w-text_w)/2"
            f":y=h-(text_h+{margin})"
            f":enable='between(t,{ini:.3f},{fim:.3f})'"
        )
        blocks.append(block)
    return ",".join(blocks)

# =============== ASS (RTL) ===============
def _ass_escape(s: str) -> str:
    return s.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")

def _ass_font_face_from_filename(fontfile: str) -> str:
    base = os.path.basename(fontfile or "").lower()
    if "notonaskharabic" in base: return "Noto Naskh Arabic"
    if "amiri" in base:           return "Amiri"
    if "inter" in base:           return "Inter"
    if "montserrat" in base:      return "Montserrat"
    if "playfairdisplay" in base: return "Playfair Display"
    if "cinzel" in base:          return "Cinzel"
    name = os.path.splitext(os.path.basename(fontfile))[0]
    name = re.sub(r"-(Regular|Bold|ExtraBold|SemiBold|Medium|Light|Black)$", "", name, flags=re.I)
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return name.strip() or "Noto Naskh Arabic"

def _segments_to_ass_file(W: int, H: int, segments: List[Tuple[float, float, str]], font_name: str, base_fs: int) -> str:
    """
    Gera .ass com:
      - alinhamento configurável (SUBS_ASS_ALIGNMENT, default=2 bottom-center)
      - tamanho final = max(base_fs, H*SUBS_ASS_SCALE)  -> SUBS_ASS_SCALE é piso e NÃO teto
    """
    # piso proporcional (ex.: 0.08 * 1920 = 153 px)
    fs_floor = int(H * SUBS_ASS_SCALE) if SUBS_ASS_SCALE > 0 else 0
    fs_base  = int(base_fs if base_fs and base_fs > 0 else 34)
    fs = max(22, fs_base, fs_floor)

    logger.info("🅰️ ASS: font='%s' base=%d piso=%.0fpx (%.1f%% de %d) => fs=%d, align=%d",
                font_name, fs_base, float(fs_floor), SUBS_ASS_SCALE*100.0, H, fs, SUBS_ASS_ALIGNMENT)

    # margens
    margin_v = max(54, int(H * 0.115))
    margin_h = max(28, int(W * 0.06))

    style = (
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{fs},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
        "0,0,0,0,100,100,0,0,1,2,0,"
        f"{SUBS_ASS_ALIGNMENT},20,{margin_h},{margin_v},1\n"
        #            ^^^^^^^^^ alinhamento 1..9 (2 = bottom-center)
    )

    hdr = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {W}\n"
        f"PlayResY: {H}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n\n"
    )
    events_hdr = "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"

    def ts(t: float) -> str:
        t = max(0.0, t)
        h = int(t // 3600); t -= h * 3600
        m = int(t // 60);   t -= m * 60
        s = int(t);         cs = int(round((t - s) * 100))
        return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"

    lines = [f"Dialogue: 0,{ts(s)},{ts(e)},Default,,0,0,0,,{_ass_escape(t)}" for (s,e,t) in segments]

    ass = hdr + style + "\n" + events_hdr + "\n".join(lines) + "\n"
    path = os.path.join(CACHE_DIR, f"subs_{uuid.uuid4().hex[:8]}.ass")
    with open(path, "w", encoding="utf-8") as f:
        f.write(ass)
    return path

# =============== Principal ===============
def _quote(arg: str) -> str:
    if not arg: return '""'
    if any(c in arg for c in ' \t"\''): return f"\"{arg.replace('\"', '\\\"')}\""
    return arg

def gerar_video(
    imagem_path: str,
    saida_path: str,
    *,
    preset: str = "hd",
    idioma: str = "auto",
    tts_engine: str = "gemini",
    legendas: bool = True,
    video_style: str = "1",
    motion: str = "none",
    slides_paths: Optional[List[str]] = None,
    transition: Optional[str] = None,
    autor: Optional[str] = "Capcut",
    tags: Optional[str] = None,
    content_mode: str = "motivacional"
):
    staged_images: List[str] = []
    staged_tts: Optional[str] = None
    extra_to_cleanup: List[str] = []
    last_cmd: List[str] = []

    try:
        if preset not in PRESETS:
            logger.warning("Preset '%s' inválido. Usando 'hd'.", preset); preset = "hd"
        conf = PRESETS[preset]
        W, H = conf["w"], conf["h"]; BR_V, BR_A, LEVEL = conf["br_v"], conf["br_a"], conf["level"]

        if not slides_paths or not isinstance(slides_paths, list) or not any(os.path.isfile(p) for p in slides_paths):
            slides_paths = [imagem_path]
        slides_paths = [p for p in slides_paths if os.path.isfile(p)]
        n_slides = max(1, min(10, len(slides_paths)))

        ffmpeg = _ffmpeg_or_die()

        # ===== Texto longo (TTS) =====
        lang_norm = _idioma_norm(idioma)
        if (content_mode or "").lower() == "tarot" and _HAVE_TAROT_LONG:
            long_text = gerar_frase_tarot_longa(idioma)
        else:
            long_text = gerar_frase_motivacional_longa(idioma)

        # se houver árabe no texto, força RTL/ASS
        if _text_contains_arabic(long_text):
            lang_norm = "ar"

        style_norm = _normalize_style(video_style)

        # ===== Narração TTS =====
        voice_audio_path: Optional[str] = gerar_narracao_tts(long_text, idioma=lang_norm, engine=tts_engine)
        dur_voz = _duracao_audio_segundos(voice_audio_path) if voice_audio_path else None
        logger.info("🎙️ Duração da voz (ffprobe): %.2fs", dur_voz or 0.0)

        if voice_audio_path and os.path.isfile(voice_audio_path):
            try:
                base, ext = os.path.splitext(os.path.basename(voice_audio_path))
                staged_tts = os.path.join(AUDIO_TTS_DIR, f"{base}_{uuid.uuid4().hex[:8]}{ext or '.wav'}")
                shutil.copy2(voice_audio_path, staged_tts)
                old_parent = os.path.basename(os.path.dirname(voice_audio_path)).lower()
                if old_parent in ("audios_tts", "audio_tts"): extra_to_cleanup.append(voice_audio_path)
                voice_audio_path = staged_tts
            except Exception as e:
                logger.warning("Falha ao mover TTS: %s (usando original).", e)

        # ===== Trilha de fundo =====
        background_audio_path: Optional[str] = None
        try:
            background_audio_path = obter_caminho_audio(content_mode=(content_mode or "motivacional"), idioma=lang_norm)
        except TypeError:
            try: background_audio_path = obter_caminho_audio(content_mode=(content_mode or "motivacional"))
            except TypeError: background_audio_path = obter_caminho_audio()
        except Exception as e:
            logger.warning("Sem áudio de fundo: %s", e)

        has_voice = bool(voice_audio_path)
        has_bg = bool(background_audio_path)

        # ===== LEGENDAS =====
        segments: List[Tuple[float, float, str]] = []
        if legendas and has_voice:
            segments = make_segments_for_audio(long_text, voice_audio_path, idioma=lang_norm)
            logger.info("📝 %d segmentos de legenda (2–3 palavras).", len(segments))

        # ===== Duração alvo =====
        if has_voice and VIDEO_RESPECT_TTS and (dur_voz or 0) > 0:
            base_len = max(5.0, (dur_voz or 0) + 0.35)
            total_video = min(base_len, VIDEO_MAX_S) if (VIDEO_MAX_S and VIDEO_MAX_S > 0) else base_len
        else:
            total_video = (segments[-1][1] + 0.35) if segments else (dur_voz or 12.0)

        per_slide_rough = total_video / n_slides
        trans = transition or DEFAULT_TRANSITION or "fade"
        trans_dur = max(0.45, min(0.85, per_slide_rough * 0.135)) if n_slides > 1 else 0.0
        per_slide = (total_video + (n_slides - 1) * trans_dur) / n_slides

        staged_inputs: List[str] = []
        for p in slides_paths:
            try:
                staged = _stage_to_dir(p, IMAGES_DIR, "stage")
                staged_inputs.append(staged); staged_images.append(staged)
            except Exception as e:
                logger.warning("Falha ao encenar '%s': %s", p, e)
        if not staged_inputs:
            raise RuntimeError("Nenhuma imagem disponível para montar o vídeo.")

        # ===== filter_complex =====
        parts: List[str] = []

        # 1) Slides
        for i in range(len(staged_inputs)):
            parts.append(_build_slide_branch(i, W, H, motion, per_slide))

        # 2) Xfade
        last_label = "[v0]"
        if len(staged_inputs) >= 2:
            offset = per_slide - trans_dur
            out_label = ""
            for idx in range(1, len(staged_inputs)):
                out_label = f"[x{idx}]"
                parts.append(f"{last_label}[v{idx}]xfade=transition={trans}:duration={trans_dur:.3f}:offset={offset:.3f}{out_label}")
                last_label = out_label; offset += (per_slide - trans_dur)
            final_video_label = out_label
        else:
            final_video_label = last_label

        # 3) Look + duração
        look_ops = []
        if (VIDEO_SAT != 1.0) or (VIDEO_CONTRAST != 1.0) or (VIDEO_GAMMA != 1.0):
            look_ops.append(f"eq=saturation={VIDEO_SAT:.3f}:contrast={VIDEO_CONTRAST:.3f}:gamma={VIDEO_GAMMA:.3f}")
        if VIDEO_SHARP > 0: look_ops.append(f"unsharp=3:3:{VIDEO_SHARP:.3f}:3:3:0.0")
        if VIDEO_GRAIN > 0: look_ops.append(f"noise=alls={VIDEO_GRAIN}:allf=t")
        if VIDEO_CHROMA_SHIFT > 0 and _ffmpeg_has_filter("chromashift"):
            shift = int(VIDEO_CHROMA_SHIFT); look_ops.append(f"chromashift=cbh={shift}:crh={-shift}")
        filters = look_ops + [f"format=yuv420p,setsar=1/1,fps={FPS_OUT}"]
        parts.append(
            f"{final_video_label}{','.join(filters)},tpad=stop_mode=clone:stop_duration={max(TPAD_EPS,0.01):.3f},"
            f"trim=duration={total_video:.3f},setpts=PTS-STARTPTS[vf]"
        )

        # 4) Legendas (ASS p/ RTL ou drawtext p/ latim)
        def _font_or_fail():
            fp = _pick_drawtext_font(video_style)
            if not fp and REQUIRE_FONTFILE:
                raise RuntimeError(
                    "Nenhuma fonte válida encontrada em ./fonts. "
                    "Coloque por ex. 'BebasNeue-Regular.ttf' e/ou defina FORCE_SUB_FONT no .env."
                )
            if not fp:
                logger.warning("⚠️ Fonte de legenda não encontrada; usando fonte do sistema (pode ficar estranho).")
            else:
                logger.info("🔤 Fonte da legenda: %s", fp)
            return fp

        use_ass = (legendas and segments and _lang_is_rtl(lang_norm) and SUBS_USE_ASS_FOR_RTL)
        if use_ass:
            try:
                arabic_face = _ass_font_face_from_filename(ARABIC_FONT)
                ass_path = _segments_to_ass_file(W, H, segments, arabic_face, SUBS_ASS_BASE_FONTSIZE)
                force_style = f"Alignment=3,MarginR={max(28,int(W*0.06))},MarginV={max(54,int(H*0.115))}"
                parts.append(
                    f"[vf]subtitles=filename={_ff_q(ass_path)}:fontsdir={_ff_q(FONTS_DIR)}:force_style={_ff_q(force_style)}[vout]"
                )
            except Exception as e:
                logger.warning("Falha no ASS/RTL (%s). Voltando ao drawtext simples.", e)
                font_for_sub = _font_or_fail()
                draw_chain = _build_drawtext_chain(H, style_norm, segments, font_for_sub)
                parts.append(f"[vf]{draw_chain},format=yuv420p[vout]")
        else:
            draw_chain = ""
            if legendas and segments:
                font_for_sub = _font_or_fail()
                draw_chain = _build_drawtext_chain(H, style_norm, segments, font_for_sub)
            parts.append(f"[vf]{draw_chain},format=yuv420p[vout]" if draw_chain else f"[vf]format=yuv420p[vout]")

        # 5) Áudio
        fade_in_dur = 0.30
        fade_out_dur = 0.60
        fade_out_start = max(0.0, total_video - fade_out_dur)
        if has_voice and has_bg:
            idx_voice = len(staged_inputs); idx_bg = len(staged_inputs) + 1
            v_chain = []
            if VOICE_LOUDNORM and _ffmpeg_has_filter("loudnorm"):
                v_chain.append("loudnorm=I=-15:TP=-1.0:LRA=11")
            v_chain += [
                f"aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={AUDIO_SR}",
                f"aresample={AUDIO_SR}:async=1"
            ]
            parts.append(f"[{idx_voice}:a]{','.join(v_chain)},asplit=2[voice_main][voice_sc]")
            parts.append(
                f"[{idx_bg}:a]volume={BG_MIX_VOLUME},"
                f"aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={AUDIO_SR},"
                f"aresample={AUDIO_SR}:async=1[bg]"
            )
            if DUCK_ENABLE and _ffmpeg_has_filter("sidechaincompress"):
                parts.append(
                    f"[bg][voice_sc]sidechaincompress="
                    f"threshold={DUCK_THRESH}:ratio={DUCK_RATIO}:attack={DUCK_ATTACK_MS}:release={DUCK_RELEASE_MS}[bg_duck]"
                )
                parts.append(f"[voice_main][bg_duck]amix=inputs=2:duration=first:dropout_transition=0[mixa]")
            else:
                parts.append(f"[voice_main][bg]amix=inputs=2:duration=first:dropout_transition=0[mixa]")
            parts.append(
                f"[mixa]atrim=end={total_video:.3f},asetpts=PTS-STARTPTS,"
                f"afade=in:st=0:d={fade_in_dur:.2f},afade=out:st={fade_out_start:.2f}:d={fade_out_dur:.2f}[aout]"
            )
        elif has_voice or has_bg:
            idx = len(staged_inputs)
            if has_voice:
                solo = []
                if VOICE_LOUDNORM and _ffmpeg_has_filter("loudnorm"):
                    solo.append("loudnorm=I=-15:TP=-1.0:LRA=11")
                solo += [
                    f"aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={AUDIO_SR}",
                    f"aresample={AUDIO_SR}:async=1"
                ]
                parts.append(f"[{idx}:a]{','.join(solo)}[amono]")
            else:
                parts.append(
                    f"[{idx}:a]volume={BG_MIX_VOLUME},"
                    f"aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={AUDIO_SR},"
                    f"aresample={AUDIO_SR}:async=1[amono]"
                )
            parts.append(
                f"[amono]atrim=end={total_video:.3f},asetpts=PTS-STARTPTS,"
                f"afade=in:st=0:d={fade_in_dur:.2f},afade=out:st={fade_out_start:.2f}:d={fade_out_dur:.2f}[aout]"
            )
        else:
            parts.append(f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_SR}:d={total_video:.3f}[aout]")

        filter_complex = ";".join(parts)
        fc_path = os.path.join(CACHE_DIR, "last_filter.txt")
        with open(fc_path, "w", encoding="utf-8") as f:
            f.write(filter_complex.rstrip() + "\n")

        # ===== comando ffmpeg =====
        cmd = [_ffmpeg_or_die(), "-y", "-loglevel", "error", "-stats"]
        for sp in staged_inputs:
            cmd += ["-loop", "1", "-i", sp]
        if has_voice and voice_audio_path:
            cmd += ["-i", voice_audio_path]
        if has_bg and background_audio_path:
            cmd += ["-i", background_audio_path]

        # metadados
        if (content_mode or "").lower() == "tarot":
            default_tags = "tarot, fortune teller, reading, spirituality"
            genre = "Spiritual"
            meta_title = "Tarot reading — daily guidance"
        else:
            default_tags = "motivational, inspirational, reflection, shorts, reels, ai"
            genre = "Inspirational"
            meta_title = "Motivational quote"
        meta_comment = f"Auto-generated narration video. Mode={content_mode}, VO language={_idioma_norm(idioma)}."
        metadata = {
            "title": meta_title,
            "artist": autor or "Unknown Artist",
            "album_artist": autor or "Unknown Artist",
            "composer": autor or "Unknown Artist",
            "comment": meta_comment,
            "description": meta_comment,
            "keywords": tags if tags else default_tags,
            "genre": genre,
            "creation_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        if META_SPOOF_ENABLE:
            metadata.update({
                "make": os.getenv("META_MAKE","Apple"),
                "model": os.getenv("META_MODEL","iPhone 13"),
                "software": os.getenv("META_SOFTWARE","iOS 17.5.1"),
                "location": META_LOCATION_ISO6709,
                "location-eng": META_LOCATION_ISO6709,
                "com.apple.quicktime.location.ISO6709": META_LOCATION_ISO6709,
            })
        else:
            if META_SOFTWARE_FALLBACK:
                metadata["software"] = META_SOFTWARE_FALLBACK

        meta_flags: List[str] = []
        for k, v in metadata.items():
            if v:
                meta_flags += ["-metadata", f"{k}={v}"]

        common_out = [
            "-r", str(FPS_OUT), "-vsync", "cfr", "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "superfast", "-tune", "stillimage",
            "-b:v", BR_V, "-maxrate", BR_V, "-bufsize", "6M",
            "-profile:v", "high", "-level", LEVEL,
            "-c:a", "aac", "-b:a", conf["br_a"], "-ar", str(AUDIO_SR), "-ac", "2",
            "-movflags", "+faststart+use_metadata_tags",
            "-x264-params", f"keyint={FPS_OUT*2}:min-keyint={FPS_OUT*2}:scenecut=0",
            "-map_metadata", "-1",
            "-threads", "2",
        ]

        cmd += ["-filter_complex_script", fc_path, "-map", "[vout]", "-map", "[aout]"]
        cmd += ["-t", f"{total_video:.3f}"]
        cmd += common_out + meta_flags

        if os.path.isdir(saida_path):
            base = os.path.splitext(os.path.basename(imagem_path))[0]
            ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            saida_path = os.path.join(saida_path, f"{base}-{ts}.mp4")
        os.makedirs(os.path.dirname(saida_path) or ".", exist_ok=True)
        cmd.append(saida_path)

        last_cmd = cmd[:]
        logger.info("🎬 FFmpeg:\n%s", "\n".join(_quote(a) for a in cmd))
        subprocess.run(cmd, check=True)
        logger.info("✅ Vídeo salvo: %s", saida_path)

    except subprocess.CalledProcessError as e:
        logger.error("❌ FFmpeg falhou (%s).", e)
        try:
            bat_path = os.path.join(CACHE_DIR, "replay_ffmpeg.bat")
            sh_path  = os.path.join(CACHE_DIR, "replay_ffmpeg.sh")
            if last_cmd:
                with open(bat_path, "w", encoding="utf-8") as f:
                    f.write("@echo off\nREM Reexecuta o último FFmpeg\n" + " ".join(_quote(a) for a in last_cmd) + "\n")
                with open(sh_path, "w", encoding="utf-8") as f:
                    f.write("#!/usr/bin/env bash\nset -e\n" + " ".join(_quote(a) for a in last_cmd) + "\n")
            logger.info("📝 Scripts de replay salvos em: %s | %s", bat_path, sh_path)
        except Exception:
            pass
        raise
    finally:
        for fp in staged_images:
            try:
                if fp and os.path.isfile(fp): os.remove(fp)
            except Exception:
                pass
        if staged_tts and os.path.isfile(staged_tts):
            try:
                os.remove(staged_tts)
            except Exception:
                pass
        for old in extra_to_cleanup:
            try:
                if old and os.path.isfile(old): os.remove(old)
            except Exception:
                pass
        for d in (AUDIO_TTS_DIR,):
            try:
                if os.path.isdir(d) and not os.listdir(d): os.rmdir(d)
            except Exception:
                pass
