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
# Tarot (opcional – fallback seguro)
try:
    from .frase import gerar_frase_tarot_longa
    _HAVE_TAROT_LONG = True
except Exception:
    _HAVE_TAROT_LONG = False

from .audio import obter_caminho_audio, gerar_narracao_tts

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# -------------------- Presets / Constantes --------------------
PRESETS = {
    "sd":     {"w": 540,  "h": 960,  "br_v": "1400k", "br_a": "128k", "level": "3.1"},
    "hd":     {"w": 720,  "h": 1280, "br_v": "2200k", "br_a": "128k", "level": "3.1"},
    "fullhd": {"w": 1080, "h": 1920, "br_v": "5000k", "br_a": "192k", "level": "4.0"},
}

# Mantido por compatibilidade; não usamos como teto duro
DURACAO_MAXIMA_VIDEO = 20.0
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

# -------------------- ENV helpers --------------------
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip().lower()
    if v in ("1","true","yes","on"):
        return True
    if v in ("0","false","no","off"):
        return False
    return default

# -------------------- ENV (áudio/motion/looks/ducking) --------------------
BG_MIX_VOLUME      = _env_float("BG_MIX_VOLUME", 0.10)
DEFAULT_TRANSITION = os.getenv("TRANSITION", "fade").strip().lower()
VOICE_LOUDNORM     = _env_bool("VOICE_LOUDNORM", True)

# Intensidades de movimento
KENBURNS_ZOOM_MAX = _env_float("KENBURNS_ZOOM_MAX", 1.22)
PAN_ZOOM          = _env_float("PAN_ZOOM", 1.18)

# FPS interno do movimento (capado)
_MOTION_FPS_ENV = _env_int("MOTION_FPS", 45)
MOTION_FPS = max(24, min(90, _MOTION_FPS_ENV))
if MOTION_FPS != _MOTION_FPS_ENV:
    logger.info("ℹ️ MOTION_FPS ajustado para %d (cap 24..90).", MOTION_FPS)

# Look "orgânico"
VIDEO_SAT          = _env_float("VIDEO_SAT", 1.00)
VIDEO_CONTRAST     = _env_float("VIDEO_CONTRAST", 1.00)
VIDEO_GAMMA        = _env_float("VIDEO_GAMMA", 1.00)
VIDEO_SHARP        = _env_float("VIDEO_SHARP", 0.00)
VIDEO_GRAIN        = _env_int  ("VIDEO_GRAIN", 0)
VIDEO_CHROMA_SHIFT = _env_int  ("VIDEO_CHROMA_SHIFT", 0)

# Ducking
DUCK_ENABLE     = _env_bool("DUCK_ENABLE", True)
DUCK_THRESH     = _env_float("DUCK_THRESH", 0.05)
DUCK_RATIO      = _env_float("DUCK_RATIO", 8.0)
DUCK_ATTACK_MS  = _env_int  ("DUCK_ATTACK_MS", 20)
DUCK_RELEASE_MS = _env_int  ("DUCK_RELEASE_MS", 250)

# Whisper (opcional)
WHISPER_ENABLE = _env_bool("WHISPER_ENABLE", False)
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_BEAM_SIZE = _env_int("WHISPER_BEAM_SIZE", 1)
WHISPER_VAD = _env_bool("WHISPER_VAD", True)
WHISPER_MODEL_CACHE = os.getenv("WHISPER_MODEL_CACHE", "./whisper_models")

# Meta spoof (opcional)
META_SPOOF_ENABLE    = _env_bool("META_SPOOF_ENABLE", False)
META_MAKE            = os.getenv("META_MAKE", "Apple")
META_MODEL           = os.getenv("META_MODEL", "iPhone 13")
META_SOFTWARE        = os.getenv("META_SOFTWARE", "iOS 17.5.1")
META_LOCATION_ISO6709= os.getenv("META_LOCATION_ISO6709", "+37.7749-122.4194+000.00/")

# >>> Respeitar TTS
VIDEO_RESPECT_TTS = _env_bool("VIDEO_RESPECT_TTS", True)
VIDEO_TAIL_PAD    = _env_float("VIDEO_TAIL_PAD", 0.40)   # margem após a fala
VIDEO_MAX_S       = _env_float("VIDEO_MAX_S", 0.0)       # 0 = sem teto
TPAD_EPS          = _env_float("TPAD_EPS", 0.25)         # segurança para arredondamentos

# --------- Fase 3: Árabe / ASS legendas ----------
SUBS_USE_ASS_FOR_RTL   = _env_bool("SUBS_USE_ASS_FOR_RTL", True)
ARABIC_FONT            = os.getenv("ARABIC_FONT", "NotoNaskhArabic-Regular.ttf")
SUBS_ASS_BASE_FONTSIZE = _env_int("SUBS_ASS_BASE_FONTSIZE", 36)
META_SOFTWARE_FALLBACK = os.getenv("META_SOFTWARE_FALLBACK", "").strip()  # se vazio, não escreve

# -------------------- Helpers --------------------
def _ffmpeg_or_die() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("ffmpeg não encontrado no PATH.")
    return path

def _ffprobe_or_die() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise RuntimeError("ffprobe não encontrado no PATH.")
    return path

def _ffmpeg_has_filter(filter_name: str) -> bool:
    try:
        out = subprocess.check_output(
            [_ffmpeg_or_die(), "-hide_banner", "-filters"],
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        for line in out.splitlines():
            if re.search(rf"\b{re.escape(filter_name)}\b", line):
                return True
        return False
    except Exception as e:
        logger.debug("Não consegui listar filtros do FFmpeg: %s", e)
        return False

def _idioma_norm(idioma: str) -> str:
    s = (idioma or "en").lower()
    if s.startswith("pt"):
        return "pt"
    if s.startswith("ar"):
        return "ar"
    return "en"

def _lang_is_rtl(lang: str) -> bool:
    return lang in ("ar",)

def _duracao_audio_segundos(audio_path: str) -> Optional[float]:
    if not audio_path or not os.path.isfile(audio_path):
        return None
    try:
        ffprobe = _ffprobe_or_die()
        out = subprocess.check_output(
            [ffprobe, "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             audio_path],
            text=True
        ).strip()
        dur = float(out)
        return dur if dur > 0 else None
    except Exception:
        return None

def _ff_normpath(path: str) -> str:
    try:
        rel = os.path.relpath(path)
    except Exception:
        rel = path
    return rel.replace("\\", "/")

# >>>>>>> ESCAPE SEGURO PARA FILTERGRAPH (Windows e Unix) <<<<<<<
_IS_WIN = (os.name == "nt")

def _ff_escape_filter_path(p: str) -> str:
    """
    Normaliza para '/', escapa o 'C:' -> 'C\\:' (Windows) e aspas simples.
    Útil para valores dentro de filtros (drawtext/subtitles).
    """
    s = _ff_normpath(p)
    if _IS_WIN and re.match(r"^[A-Za-z]:/", s):
        s = s[0] + r"\:" + s[2:]  # C:/... -> C\:/...
    s = s.replace("'", r"\'")
    return s

def _ff_q(val: str) -> str:
    """Coloca entre aspas simples para o parser do filtergraph."""
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

# ---------- Segmentação para legendas ----------
import re as _re
_AR_RANGES = r"\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF"

def _tokenize_words(text: str) -> List[str]:
    text = _re.sub(r"[\r\n]+", " ", (text or "")).strip()
    # Latin + extended + números + apóstrofos | blocos árabes
    tokens = _re.findall(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ'’\-]+|[" + _AR_RANGES + r"]+", text)
    return [t for t in tokens if t]

def _chunk_words(tokens: List[str]) -> List[List[str]]:
    chunks: List[List[str]] = []
    i = 0
    n = len(tokens)
    while i < n:
        take = 3
        long_word = any(len(tokens[j]) >= 10 for j in range(i, min(i + 3, n)))
        if long_word:
            take = 2
        if i + take > n:
            take = max(1, n - i)
        chunk = tokens[i:i + take]
        chunks.append(chunk)
        i += take
    return chunks

def _distribuir_duracoes_por_palavra(frases: List[str], total_disp: float) -> List[float]:
    if not frases:
        return []
    min_seg, max_seg = 0.7, 3.0
    pesos = [max(1, len(_tokenize_words(f))) for f in frases]
    soma = sum(pesos)
    brutas = [(p / soma) * total_disp for p in pesos]
    clamped = [min(max(b, min_seg), max_seg) for b in brutas]
    fator = min(1.0, (total_disp / sum(clamped))) if sum(clamped) > 0 else 1.0
    return [d * fator for d in clamped]

def _segmentos_legenda_palavras(texto: str, duracao_audio: float, uppercase: bool = True) -> List[Tuple[float, float, str]]:
    tokens = _tokenize_words(texto)
    if not tokens:
        return []
    chunks = _chunk_words(tokens)
    lines = [" ".join(ch).upper() if uppercase else " ".join(ch) for ch in chunks]
    alvo = max(3.0, (duracao_audio or 12.0) - 0.2)
    gap = 0.10
    duracoes = _distribuir_duracoes_por_palavra(lines, alvo - gap * (len(lines) - 1))
    t = 0.25
    segs: List[Tuple[float, float, str]] = []
    for line, d in zip(lines, duracoes):
        ini = t
        fim = t + d
        segs.append((ini, fim, line))
        t = fim + gap
    return segs

def _segmentos_via_whisper(audio_path: str, idioma: str) -> List[Tuple[float, float, str]]:
    if not WHISPER_ENABLE:
        return []
    try:
        from faster_whisper import WhisperModel
    except Exception as e:
        logger.warning("WHISPER_ENABLE=1 mas 'faster-whisper' não está disponível: %s", e)
        return []
    if (idioma or "").lower().startswith("pt"):
        lang_code = "pt"
    elif (idioma or "").lower().startswith("ar"):
        lang_code = "ar"
    else:
        lang_code = "en"
    try:
        dur_audio = _duracao_audio_segundos(audio_path) or 0.0
        cap = dur_audio + 0.10 if dur_audio > 0 else 60.0

        logger.info("🧩 Whisper: carregando modelo '%s' (%s, %s) ...", WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE)
        model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE, download_root=WHISPER_MODEL_CACHE)
        segments, _info = model.transcribe(audio_path, language=lang_code, beam_size=WHISPER_BEAM_SIZE, vad_filter=WHISPER_VAD, word_timestamps=True)
        words = []
        for seg in segments:
            if getattr(seg, "words", None):
                for w in seg.words:
                    if w.word and w.start is not None and w.end is not None:
                        token = _re.sub(r"\s+", "", w.word)
                        if token:
                            words.append((float(w.start), float(w.end), token))
        if not words:
            logger.warning("Whisper não retornou palavras com timestamp. Usando heurística.")
            return []
        out: List[Tuple[float, float, str]] = []
        i, n = 0, len(words)
        while i < n:
            take = 3
            if any(len(words[j][2]) >= 10 for j in range(i, min(i + 3, n))):
                take = 2
            if i + take > n:
                take = max(1, n - i)
            chunk = words[i:i + take]
            ini, fim = chunk[0][0], chunk[-1][1]
            texto = " ".join([w[2] for w in chunk])
            # não upper() para preservar script árabe
            if ini < cap and fim > 0:
                ini2 = max(0.20, ini)
                fim2 = min(cap - 0.05, fim)
                if fim2 > ini2 + 0.12:
                    out.append((ini2, fim2, texto))
            i += take
        out = [(s, e, t) for (s, e, t) in out if s < cap]
        logger.info("📝 %d segmentos via Whisper.", len(out))
        return out
    except Exception as e:
        logger.warning("Falha na sincronia via Whisper: %s. Usando heurística.", e)
        return []

# ---------------- estilos de legenda ----------------
VIDEO_STYLES = {
    "1": {"key": "minimal_compact", "label": "Compact (sem caixa, contorno fino)"},
    "2": {"key": "clean_outline",   "label": "Clean (um pouco maior)"},
    "3": {"key": "tiny_outline",    "label": "Tiny (bem pequeno)"},
    "classic": "1", "modern": "2", "serif": "2", "clean": "2", "mono": "3",
}

def _normalize_style(style: str) -> str:
    s = (style or "1").strip().lower()
    if s in ("1", "2", "3"):
        return s
    return str(VIDEO_STYLES.get(s, "1"))

def _first_existing_font(*names: str) -> Optional[str]:
    for n in names:
        p = os.path.join(FONTS_DIR, n)
        if os.path.isfile(p):
            return p
    return None

def _pick_drawtext_font(style_key: str) -> Optional[str]:
    s = (style_key or "").lower()
    if s in ("classic", "1", "clean", "5"):
        return _first_existing_font("Montserrat-Regular.ttf","Inter-Bold.ttf","BebasNeue-Regular.ttf")
    if s in ("modern", "2"):
        return _first_existing_font("BebasNeue-Regular.ttf","Inter-Bold.ttf","Montserrat-ExtraBold.ttf")
    if s in ("serif", "3"):
        return _first_existing_font("PlayfairDisplay-Regular.ttf","Cinzel-Bold.ttf")
    if s in ("mono", "4"):
        return _first_existing_font("Inter-Bold.ttf","Montserrat-Regular.ttf")
    return _first_existing_font("Montserrat-Regular.ttf","Inter-Bold.ttf","BebasNeue-Regular.ttf")

def _write_textfile(content: str, idx: int) -> str:
    path = os.path.join(CACHE_DIR, f"drawtext_{idx:02d}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path

def _build_drawtext_chain(H: int, style_id: str, segments: List[Tuple[float, float, str]], font_path: Optional[str]) -> str:
    sid = _normalize_style(style_id)
    if sid == "1":
        fs = max(20, int(H * 0.026)); borderw = 1; margin = max(48, int(H * 0.10))
    elif sid == "2":
        fs = max(20, int(H * 0.032)); borderw = 2; margin = max(54, int(H * 0.115))
    else:
        fs = max(18, int(H * 0.024)); borderw = 1; margin = max(56, int(H * 0.12))

    use_fontfile = (font_path and os.path.isfile(font_path))
    font_opt = f":fontfile={_ff_q(font_path)}" if use_fontfile else ""
    if use_fontfile:
        logger.info("🔤 drawtext usando fonte: %s", font_path)
    else:
        logger.info("🔤 drawtext sem fontfile explícito (fonte do sistema).")

    blocks = []
    for idx, (ini, fim, txt) in enumerate(segments, start=1):
        tf_posix = _ff_normpath(_write_textfile(txt, idx))
        tf_q = _ff_q(tf_posix)
        block = (
            "drawtext="
            f"textfile={tf_q}"
            f"{font_opt}"
            f":fontsize={fs}"
            f":fontcolor=white"
            f":borderw={borderw}:bordercolor=black"
            f":box=0"
            f":line_spacing=0"
            f":x=(w-text_w)/2"
            f":y=h-(text_h+{margin})"
            f":enable='between(t,{ini:.3f},{fim:.3f})'"
        )
        blocks.append(block)
    return ",".join(blocks)

# --------- ASS (libass) p/ RTL ----------
def _ass_escape(s: str) -> str:
    return s.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")

def _segments_to_ass_file(H: int, segments: List[Tuple[float, float, str]], font_name: str, base_fs: int) -> str:
    """
    Gera um arquivo .ass simples com estilo centrado no rodapé.
    """
    fs = max(18, int(base_fs if base_fs > 0 else 36))
    margin_v = max(54, int(H * 0.115))
    style = (
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{fs},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
        "0,0,0,0,100,100,0,0,1,2,0,2,20,20," + str(margin_v) + ",1\n"
    )
    hdr = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "\n"
    )
    events_hdr = (
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    def ts(t: float) -> str:
        t = max(0.0, t)
        h = int(t // 3600); t -= h * 3600
        m = int(t // 60);   t -= m * 60
        s = int(t);         cs = int(round((t - s) * 100))
        return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"

    lines = []
    for (ini, fim, txt) in segments:
        line = f"Dialogue: 0,{ts(ini)},{ts(fim)},Default,,0,0,0,,{_ass_escape(txt)}"
        lines.append(line)

    ass = hdr + style + "\n" + events_hdr + "\n".join(lines) + "\n"
    path = os.path.join(CACHE_DIR, f"subs_{_uuid_suffix()}.ass")
    with open(path, "w", encoding="utf-8") as f:
        f.write(ass)
    return path

# -------------------- Movimento (zoom/pan) --------------------
def _smoothstep_expr(p: str) -> str:
    return f"(({p})*({p})*(3-2*({p})))"

def _kb_in(W: int, H: int, frames_this_slide: int) -> str:
    p = f"(on/{frames_this_slide})"
    ps = _smoothstep_expr(p)
    z = f"(1+({KENBURNS_ZOOM_MAX:.3f}-1)*{ps})"
    return (
        "zoompan="
        f"z='{z}':"
        "x='iw/2-(iw/zoom/2)':"
        "y='ih/2-(ih/zoom/2)':"
        "d=1:"
        f"s={W}x{H}"
    )

def _kb_out(W: int, H: int, frames_this_slide: int) -> str:
    p = f"(on/{frames_this_slide})"
    ps = _smoothstep_expr(p)
    z = f"({KENBURNS_ZOOM_MAX:.3f} + (1-{KENBURNS_ZOOM_MAX:.3f})*{ps})"
    return (
        "zoompan="
        f"z='{z}':"
        "x='iw/2-(iw/zoom/2)':"
        "y='ih/2-(ih/zoom/2)':"
        "d=1:"
        f"s={W}x{H}"
    )

def _pan_lr(W: int, H: int, frames_this_slide: int) -> str:
    p = f"(on/{frames_this_slide})"
    ps = _smoothstep_expr(p)
    return (
        "zoompan="
        f"z={PAN_ZOOM:.3f}:"
        f"x='(iw/zoom-ow)*{ps}':"
        "y='(ih/zoom-oh)/2':"
        "d=1:"
        f"s={W}x{H}"
    )

def _pan_ud(W: int, H: int, frames_this_slide: int) -> str:
    p = f"(on/{frames_this_slide})"
    ps = _smoothstep_expr(p)
    return (
        "zoompan="
        f"z={PAN_ZOOM:.3f}:"
        "x='(iw/zoom-ow)/2':"
        f"y='(ih/zoom-oh)*{ps}':"
        "d=1:"
        f"s={W}x{H}"
    )

def _build_slide_branch(idx: int, W: int, H: int, motion: str, per_slide: float) -> str:
    frames_slide = max(1, int(per_slide * MOTION_FPS))
    m = (motion or "none").lower()

    if m in ("kenburns_in", "kenburns-in", "zoom_in", "zoom-in", "2"):
        motion_expr = _kb_in(W, H, frames_slide)
        chain = f"[{idx}:v]{motion_expr}"
    elif m in ("kenburns_out", "kenburns-out", "zoom_out", "zoom-out", "3"):
        motion_expr = _kb_out(W, H, frames_slide)
        chain = f"[{idx}:v]{motion_expr}"
    elif m in ("pan_lr", "pan-left-right", "4", "pan left→right", "pan left->right"):
        motion_expr = _pan_lr(W, H, frames_slide)
        chain = f"[{idx}:v]{motion_expr}"
    elif m in ("pan_ud", "pan-up-down", "5", "pan up→down", "pan up->down"):
        motion_expr = _pan_ud(W, H, frames_slide)
        chain = f"[{idx}:v]{motion_expr}"
    else:
        chain = (
            f"[{idx}:v]"
            f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black"
        )

    chain += (
        f",format=yuv420p,setsar=1/1,"
        f"trim=duration={per_slide:.6f},"
        f"setpts=PTS-STARTPTS,"
        f"fps={MOTION_FPS}[v{idx}]"
    )
    return chain

# -------------------- principal --------------------
def _quote(arg: str) -> str:
    if not arg:
        return '""'
    if any(c in arg for c in ' \t"\''):
        return f"\"{arg.replace('\"', '\\\"')}\""
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
    # ===== METADADOS =====
    autor: Optional[str] = "Capcut",
    tags: Optional[str] = None,
    # ===== Fase 3 =====
    content_mode: str = "motivacional"   # "motivacional" | "tarot"
):
    staged_images: List[str] = []
    staged_tts: Optional[str] = None
    extra_to_cleanup: List[str] = []
    last_cmd: List[str] = []

    try:
        if preset not in PRESETS:
            logger.warning("Preset '%s' inválido. Usando 'hd'.", preset)
            preset = "hd"

        conf = PRESETS[preset]
        W, H = conf["w"], conf["h"]
        BR_V, BR_A, LEVEL = conf["br_v"], conf["br_a"], conf["level"]

        if not slides_paths or not isinstance(slides_paths, list) or not any(os.path.isfile(p) for p in slides_paths):
            slides_paths = [imagem_path]
        slides_paths = [p for p in slides_paths if os.path.isfile(p)]
        n_slides = max(1, min(10, len(slides_paths)))

        ffmpeg = _ffmpeg_or_die()

        # ====== TEXTO LONGO (TTS) por modo ======
        lang_norm = _idioma_norm(idioma)
        if (content_mode or "").lower() == "tarot" and _HAVE_TAROT_LONG:
            long_text = gerar_frase_tarot_longa(idioma)
        else:
            long_text = gerar_frase_motivacional_longa(idioma)

        style_norm = _normalize_style(video_style)

        voice_audio_path: Optional[str] = gerar_narracao_tts(long_text, idioma=lang_norm, engine=tts_engine)
        dur_voz = _duracao_audio_segundos(voice_audio_path) if voice_audio_path else None
        logger.info("🎙️ Duração da voz (ffprobe): %.2fs", dur_voz or 0.0)

        if voice_audio_path and os.path.isfile(voice_audio_path):
            try:
                base, ext = os.path.splitext(os.path.basename(voice_audio_path))
                staged_tts = os.path.join(AUDIO_TTS_DIR, f"{base}_{_uuid_suffix()}{ext or '.wav'}")
                shutil.copy2(voice_audio_path, staged_tts)
                old_parent = os.path.basename(os.path.dirname(voice_audio_path)).lower()
                if old_parent in ("audios_tts", "audio_tts"):
                    extra_to_cleanup.append(voice_audio_path)
                voice_audio_path = staged_tts
            except Exception as e:
                logger.warning("Falha ao mover/copiar TTS para %s: %s. Usando original.", AUDIO_TTS_DIR, e)

        background_audio_path: Optional[str] = None
        try:
            background_audio_path = obter_caminho_audio()
        except Exception as e:
            logger.warning("Sem áudio de fundo válido: %s", e)

        has_voice = bool(voice_audio_path)
        has_bg = bool(background_audio_path)

        # ====== LEGENDAS (ASS p/ árabe; drawtext p/ en/pt) ======
        segments: List[Tuple[float, float, str]] = []
        if legendas and has_voice:
            seg_whisper = _segmentos_via_whisper(voice_audio_path, lang_norm) if WHISPER_ENABLE else []
            # Uppercase só para latinos
            segments = seg_whisper if seg_whisper else _segmentos_legenda_palavras(long_text, dur_voz or 12.0, uppercase=(lang_norm != "ar"))
            logger.info("📝 %d segmentos de legenda.", len(segments))

        # --- duração alvo do vídeo (segue TTS) ---
        if has_voice and VIDEO_RESPECT_TTS and (dur_voz or 0) > 0:
            base_len = max(5.0, (dur_voz or 0) + VIDEO_TAIL_PAD)
            total_video = min(base_len, VIDEO_MAX_S) if (VIDEO_MAX_S and VIDEO_MAX_S > 0) else base_len
        else:
            if segments:
                total_video = max(8.0, segments[-1][1] + 0.45)
            elif dur_voz:
                total_video = max(8.0, (dur_voz or 0) + 0.25)
            else:
                total_video = 12.0

        # 1) calcula transição a partir de um per_slide aproximado
        per_slide_rough = total_video / n_slides
        trans = transition or DEFAULT_TRANSITION or "fade"
        trans_dur = max(0.50, min(0.90, per_slide_rough * 0.135)) if n_slides > 1 else 0.0

        # 2) corrige per_slide para compensar o overlap do xfade:
        per_slide = (total_video + (n_slides - 1) * trans_dur) / n_slides

        logger.info("⏱️ Target: total=%.3fs | n=%d | per_slide=%.3fs | xfade=%.3fs | perda_overlap=%.3fs",
                    total_video, n_slides, per_slide, trans_dur, (n_slides - 1) * trans_dur)

        staged_inputs: List[str] = []
        for p in slides_paths:
            try:
                staged = _stage_to_dir(p, IMAGES_DIR, "stage")
                staged_inputs.append(staged)
                staged_images.append(staged)
            except Exception as e:
                logger.warning("Falha ao encenar imagem '%s': %s (ignorando este slide)", p, e)
        if not staged_inputs:
            raise RuntimeError("Nenhuma imagem disponível para montar o vídeo.")

        # -------- construir filter_complex --------
        parts: List[str] = []

        # 1) Branch por slide
        for i in range(len(staged_inputs)):
            parts.append(_build_slide_branch(i, W, H, motion, per_slide))

        # 2) Transições xfade
        last_label = "[v0]"
        if len(staged_inputs) >= 2:
            offset = per_slide - trans_dur
            out_label = ""
            for idx in range(1, len(staged_inputs)):
                out_label = f"[x{idx}]"
                parts.append(
                    f"{last_label}[v{idx}]xfade=transition={trans}:duration={trans_dur:.3f}:offset={offset:.3f}{out_label}"
                )
                last_label = out_label
                offset += (per_slide - trans_dur)
            final_video_label = out_label
        else:
            final_video_label = last_label

        # 3) Look orgânico + normalização final + tpad+trim para garantir duração exata
        look_ops = []
        if (VIDEO_SAT != 1.0) or (VIDEO_CONTRAST != 1.0) or (VIDEO_GAMMA != 1.0):
            look_ops.append(f"eq=saturation={VIDEO_SAT:.3f}:contrast={VIDEO_CONTRAST:.3f}:gamma={VIDEO_GAMMA:.3f}")
        if VIDEO_SHARP > 0:
            look_ops.append(f"unsharp=3:3:{VIDEO_SHARP:.3f}:3:3:0.0")
        if VIDEO_GRAIN > 0:
            look_ops.append(f"noise=alls={VIDEO_GRAIN}:allf=t")
        if VIDEO_CHROMA_SHIFT > 0:
            if _ffmpeg_has_filter("chromashift"):
                shift = int(VIDEO_CHROMA_SHIFT)
                look_ops.append(f"chromashift=cbh={shift}:crh={-shift}")
            else:
                logger.warning("FFmpeg sem 'chromashift'; seguindo sem deslocamento de croma.")

        filters = look_ops + [f"format=yuv420p,setsar=1/1,fps={FPS_OUT}"]
        parts.append(
            f"{final_video_label}{','.join(filters)},"
            f"tpad=stop_mode=clone:stop_duration={max(TPAD_EPS, 0.01):.3f},"
            f"trim=duration={total_video:.3f},setpts=PTS-STARTPTS[vf]"
        )

        # 4) Legendas: ASS para árabe (se possível), drawtext para en/pt
        use_ass = (legendas and segments and _lang_is_rtl(lang_norm) and SUBS_USE_ASS_FOR_RTL)
        if use_ass:
            try:
                font_name = os.path.splitext(os.path.basename(ARABIC_FONT))[0] or "NotoNaskhArabic"
                ass_path = _segments_to_ass_file(H, segments, font_name, SUBS_ASS_BASE_FONTSIZE)
                parts.append(
                    f"[vf]subtitles=filename={_ff_q(ass_path)}:fontsdir={_ff_q(FONTS_DIR)}[vout]"
                )
            except Exception as e:
                logger.warning("Falha no ASS/RTL (%s). Voltando ao drawtext simples sem uppercase.", e)
                font_for_sub = _pick_drawtext_font(video_style)
                draw_chain = _build_drawtext_chain(H, style_norm, segments, font_for_sub)
                parts.append(f"[vf]{draw_chain},format=yuv420p[vout]")
        else:
            draw_chain = ""
            if legendas and segments:
                font_for_sub = _pick_drawtext_font(video_style)
                draw_chain = _build_drawtext_chain(H, style_norm, segments, font_for_sub)
            if draw_chain:
                parts.append(f"[vf]{draw_chain},format=yuv420p[vout]")
            else:
                parts.append(f"[vf]format=yuv420p[vout]")

        # 5) Áudio — segue a VOZ e casa com o total do vídeo
        fade_in_dur = 0.30
        fade_out_dur = 0.60
        fade_out_start = max(0.0, total_video - fade_out_dur)

        if has_voice and has_bg:
            idx_voice = len(staged_inputs)
            idx_bg    = len(staged_inputs) + 1

            v_chain = []
            if VOICE_LOUDNORM and _ffmpeg_has_filter("loudnorm"):
                v_chain.append("loudnorm=I=-15:TP=-1.0:LRA=11")
            elif VOICE_LOUDNORM:
                logger.warning("FFmpeg sem 'loudnorm'; seguindo sem normalização.")
            v_chain.append(f"aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={AUDIO_SR}")
            v_chain.append(f"aresample={AUDIO_SR}:async=1")
            parts.append(f"[{idx_voice}:a]{','.join(v_chain)},asplit=2[voice_main][voice_sc]")

            parts.append(
                f"[{idx_bg}:a]"
                f"volume={BG_MIX_VOLUME},"
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
                if DUCK_ENABLE:
                    logger.warning("FFmpeg sem 'sidechaincompress'; usando apenas amix (sem ducking).")
                parts.append(f"[voice_main][bg]amix=inputs=2:duration=first:dropout_transition=0[mixa]")

            parts.append(
                f"[mixa]"
                f"atrim=end={total_video:.3f},asetpts=PTS-STARTPTS,"
                f"afade=in:st=0:d={fade_in_dur:.2f},"
                f"afade=out:st={fade_out_start:.2f}:d={fade_out_dur:.2f}[aout]"
            )

        elif has_voice or has_bg:
            idx = len(staged_inputs)
            if has_voice:
                solo = []
                if VOICE_LOUDNORM and _ffmpeg_has_filter("loudnorm"):
                    solo.append("loudnorm=I=-15:TP=-1.0:LRA=11")
                elif VOICE_LOUDNORM:
                    logger.warning("FFmpeg sem 'loudnorm'; seguindo sem normalização.")
                solo.append(f"aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={AUDIO_SR}")
                solo.append(f"aresample={AUDIO_SR}:async=1")
                parts.append(f"[{idx}:a]{','.join(solo)}[amono]")
            else:
                parts.append(
                    f"[{idx}:a]"
                    f"volume={BG_MIX_VOLUME},"
                    f"aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={AUDIO_SR},"
                    f"aresample={AUDIO_SR}:async=1[amono]"
                )
            parts.append(
                f"[amono]"
                f"atrim=end={total_video:.3f},asetpts=PTS-STARTPTS,"
                f"afade=in:st=0:d={fade_in_dur:.2f},"
                f"afade=out:st={fade_out_start:.2f}:d={fade_out_dur:.2f}[aout]"
            )
        else:
            parts.append(f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_SR}:d={total_video:.3f}[aout]")

        filter_complex = ";".join(parts)

        # salva para debug e usa como script (evita escapagem do Windows)
        fc_path = os.path.join(CACHE_DIR, "last_filter.txt")
        with open(fc_path, "w", encoding="utf-8") as f:
            f.write(filter_complex.rstrip() + "\n")
        logger.info("🧩 filter_complex salvo em %s", fc_path)

        # --------- comando ffmpeg ----------
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-stats"]

        # imagens (cada uma com -loop 1)
        for sp in staged_inputs:
            cmd += ["-loop", "1", "-i", sp]

        # entradas de áudio
        if has_voice and voice_audio_path:
            cmd += ["-i", voice_audio_path]
        if has_bg and background_audio_path:
            cmd += ["-i", background_audio_path]

        # ---------- Metadados 100% em inglês ----------
        if (content_mode or "").lower() == "tarot":
            default_tags = "tarot, fortune teller, reading, spirituality"
            genre = "Spiritual"
            meta_title = "Tarot reading — daily guidance"
        else:
            default_tags = "motivational, inspirational, reflection, shorts, reels, ai"
            genre = "Inspirational"
            meta_title = "Motivational quote"

        meta_comment = f"Auto-generated narration video. Mode={content_mode}, VO language={_idioma_norm(idioma)}."
        meta_description = meta_comment

        metadata = {
            "title": meta_title,
            "artist": autor or "Unknown Artist",
            "album_artist": autor or "Unknown Artist",
            "composer": autor or "Unknown Artist",
            "comment": meta_comment,
            "description": meta_description,
            "keywords": tags if tags else default_tags,
            "genre": genre,
            "creation_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

        if META_SPOOF_ENABLE:
            metadata.update({
                "make": META_MAKE,
                "model": META_MODEL,
                "software": META_SOFTWARE,
                "location": META_LOCATION_ISO6709,
                "location-eng": META_LOCATION_ISO6709,
                "com.apple.quicktime.location.ISO6709": META_LOCATION_ISO6709,
            })
        else:
            # NADA de "Gerador de IA". Se estiver vazio, não adiciona a chave.
            if META_SOFTWARE_FALLBACK:
                metadata["software"] = META_SOFTWARE_FALLBACK

        meta_flags: List[str] = []
        for key, value in metadata.items():
            if value:
                meta_flags.extend(["-metadata", f"{key}={value}"])

        common_out = [
            "-r", str(FPS_OUT),
            "-vsync", "cfr",
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264",
            "-preset", "superfast",
            "-tune", "stillimage",
            "-b:v", BR_V,
            "-maxrate", BR_V, "-bufsize", "6M",
            "-profile:v", "high",
            "-level", LEVEL,
            "-c:a", "aac",
            "-b:a", conf["br_a"],
            "-ar", str(AUDIO_SR),
            "-ac", "2",
            "-movflags", "+faststart+use_metadata_tags",
            "-x264-params", f"keyint={FPS_OUT*2}:min-keyint={FPS_OUT*2}:scenecut=0",
            "-map_metadata", "-1",
            "-threads", "2",
        ]

        cmd += ["-filter_complex_script", fc_path, "-map", "[vout]", "-map", "[aout]"]
        cmd += ["-t", f"{total_video:.3f}"]
        cmd += common_out
        cmd += meta_flags

        if os.path.isdir(saida_path):
            base = os.path.splitext(os.path.basename(imagem_path))[0]
            ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            saida_path = os.path.join(saida_path, f"{base}-{ts}.mp4")
        os.makedirs(os.path.dirname(saida_path) or ".", exist_ok=True)
        cmd.append(saida_path)

        last_cmd = cmd[:]

        logger.info("📂 CWD: %s", os.getcwd())
        logger.info("🎬 FFmpeg args:\n%s", "\n".join(_quote(a) for a in cmd))

        subprocess.run(cmd, check=True)
        logger.info("✅ Vídeo salvo: %s", saida_path)

    except subprocess.CalledProcessError as e:
        logger.error("❌ FFmpeg falhou (%s).", e)
        try:
            bat_path = os.path.join(CACHE_DIR, "replay_ffmpeg.bat")
            sh_path  = os.path.join(CACHE_DIR, "replay_ffmpeg.sh")
            if last_cmd:
                with open(bat_path, "w", encoding="utf-8") as f:
                    f.write("@echo off\n")
                    f.write("REM Reexecuta o último comando FFmpeg\n")
                    f.write(" ".join(_quote(a) for a in last_cmd) + "\n")
                with open(sh_path, "w", encoding="utf-8") as f:
                    f.write("#!/usr/bin/env bash\n")
                    f.write("set -e\n")
                    f.write(" ".join(_quote(a) for a in last_cmd) + "\n")
            logger.info("📝 Scripts de replay salvos em: %s | %s", bat_path, sh_path)
        except Exception as w:
            logger.debug("Falha ao salvar scripts de replay: %s", w)
        raise
    finally:
        for fp in staged_images:
            try:
                if fp and os.path.isfile(fp):
                    os.remove(fp)
            except Exception as e:
                logger.debug("Não consegui apagar imagem staged '%s': %s", fp, e)

        if staged_tts and os.path.isfile(staged_tts):
            try:
                os.remove(staged_tts)
            except Exception as e:
                logger.debug("Não consegui apagar TTS staged '%s': %s", staged_tts, e)

        for old in extra_to_cleanup:
            try:
                if old and os.path.isfile(old):
                    os.remove(old)
            except Exception as e:
                logger.debug("Não consegui apagar TTS antigo '%s': %s", old, e)

        for d in (AUDIO_TTS_DIR,):
            try:
                if os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
            except Exception:
                pass
