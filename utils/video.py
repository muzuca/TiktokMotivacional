#video.py
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

# -------------------- ENV --------------------
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

BG_MIX_VOLUME      = _env_float("BG_MIX_VOLUME", 0.10)
DEFAULT_TRANSITION = os.getenv("TRANSITION", "fade").strip().lower()
VOICE_LOUDNORM     = os.getenv("VOICE_LOUDNORM", "1").strip().lower() in ("1", "true", "yes")

# Intensidades de movimento
KENBURNS_ZOOM_MAX = _env_float("KENBURNS_ZOOM_MAX", 1.22)  # 22%
PAN_ZOOM          = _env_float("PAN_ZOOM", 1.18)

# FPS interno do movimento (capado para não travar máquina)
_MOTION_FPS_ENV = _env_int("MOTION_FPS", 45)
MOTION_FPS = max(24, min(90, _MOTION_FPS_ENV))
if MOTION_FPS != _MOTION_FPS_ENV:
    logger.info("ℹ️ MOTION_FPS ajustado para %d (cap 24..90).", MOTION_FPS)

# Whisper (opcional — só para sincronizar legendas com a fala)
WHISPER_ENABLE = os.getenv("WHISPER_ENABLE", "0").strip().lower() in ("1", "true", "yes")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_BEAM_SIZE = int(os.getenv("WHISPER_BEAM_SIZE", "1"))
WHISPER_VAD = os.getenv("WHISPER_VAD", "1").strip().lower() in ("1", "true", "yes")
WHISPER_MODEL_CACHE = os.getenv("WHISPER_MODEL_CACHE", "./whisper_models")

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

def _idioma_norm(idioma: str) -> str:
    s = (idioma or "en").lower()
    return "pt" if s.startswith("pt") else "en"

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

def _uuid_suffix() -> str:
    return uuid.uuid4().hex[:8]

def _stage_to_dir(src_path: str, target_dir: str, prefix: str) -> str:
    """
    Copia o arquivo para 'target_dir' com um nome temporário seguro,
    e retorna o caminho do arquivo encenado (staged).
    """
    os.makedirs(target_dir, exist_ok=True)
    base, ext = os.path.splitext(os.path.basename(src_path))
    dst_name = f"{prefix}_{base}_{_uuid_suffix()}{ext or '.jpg'}"
    dst_path = os.path.join(target_dir, dst_name)
    shutil.copy2(src_path, dst_path)
    return dst_path

# ---------- Segmentação para legendas ----------
import re as _re
def _tokenize_words(text: str) -> List[str]:
    text = _re.sub(r"[\r\n]+", " ", (text or "")).strip()
    tokens = _re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9'’\-]+", text)
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
    pesos = [max(1, len(f.split())) for f in frases]
    soma = sum(pesos)
    brutas = [(p / soma) * total_disp for p in pesos]
    clamped = [min(max(b, min_seg), max_seg) for b in brutas]
    fator = min(1.0, (total_disp / sum(clamped))) if sum(clamped) > 0 else 1.0
    return [d * fator for d in clamped]

def _segmentos_legenda_palavras(texto: str, duracao_audio: float) -> List[Tuple[float, float, str]]:
    tokens = _tokenize_words(texto)
    if not tokens:
        return []
    chunks = _chunk_words(tokens)
    lines = [" ".join(ch).upper() for ch in chunks]
    alvo = max(3.0, min(DURACAO_MAXIMA_VIDEO - 0.3, (duracao_audio or DURACAO_MAXIMA_VIDEO) - 0.2))
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
    lang_code = "pt" if (idioma or "").lower().startswith("pt") else "en"
    try:
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
            texto = " ".join([w[2] for w in chunk]).upper()
            if ini < DURACAO_MAXIMA_VIDEO and fim > 0:
                ini2 = max(0.20, ini)
                fim2 = min(DURACAO_MAXIMA_VIDEO - 0.05, fim)
                if fim2 > ini2 + 0.12:
                    out.append((ini2, fim2, texto))
            i += take
        out = [(s, e, t) for (s, e, t) in out if s < DURACAO_MAXIMA_VIDEO]
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
    font_opt = f":fontfile={_ff_normpath(font_path)}" if use_fontfile else ""
    if use_fontfile:
        logger.info("🔤 drawtext usando fonte: %s", font_path)
    else:
        logger.info("🔤 drawtext sem fontfile explícito (fonte do sistema).")

    blocks = []
    for idx, (ini, fim, txt) in enumerate(segments, start=1):
        tf_posix = _ff_normpath(_write_textfile(txt, idx))
        block = (
            "drawtext="
            f"textfile={tf_posix}"
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

# -------------------- Movimento (zoom/pan) --------------------
def _smoothstep_expr(p: str) -> str:
    # smoothstep: p*p*(3-2*p)
    return f"(({p})*({p})*(3-2*({p})))"

def _kb_in(W: int, H: int, frames_this_slide: int) -> str:
    # z = 1 + (Zmax-1)*smoothstep(p)
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
    # z = Zmax + (1-Zmax)*smoothstep(p)  (vai diminuindo até ~1)
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
    """
    Cada slide vira CFR estável:
      [input] -> zoompan/scale -> format -> setsar -> trim -> setpts -> fps=MOTION_FPS -> [v{idx}]
    """
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
        return f'"{arg.replace("\"", "\\\"")}"'
    return arg

def gerar_video(
    imagem_path: str,              # imagem de capa (será encenada em IMAGES_DIR)
    saida_path: str,
    *,
    preset: str = "hd",
    idioma: str = "auto",
    tts_engine: str = "gemini",
    legendas: bool = True,
    video_style: str = "1",
    motion: str = "none",
    slides_paths: Optional[List[str]] = None,  # lista de imagens extras (serão encenadas em IMAGES_DIR)
    transition: Optional[str] = None,
    # ===== NOVOS PARÂMETROS PARA METADADOS =====
    autor: Optional[str] = "Gerador IA",
    tags: Optional[str] = None
):
    # Coleta para cleanup
    staged_images: List[str] = []
    staged_tts: Optional[str] = None
    extra_to_cleanup: List[str] = []  # ex.: original TTS fora de audios/tts
    last_cmd: List[str] = []

    try:
        if preset not in PRESETS:
            logger.warning("Preset '%s' inválido. Usando 'hd'.", preset)
            preset = "hd"

        conf = PRESETS[preset]
        W, H = conf["w"], conf["h"]
        BR_V, BR_A, LEVEL = conf["br_v"], conf["br_a"], conf["level"]

        # Normaliza lista de slides (inclui a capa se necessário)
        if not slides_paths or not isinstance(slides_paths, list) or not any(os.path.isfile(p) for p in slides_paths):
            slides_paths = [imagem_path]
        slides_paths = [p for p in slides_paths if os.path.isfile(p)]
        n_slides = max(1, min(10, len(slides_paths)))

        ffmpeg = _ffmpeg_or_die()

        # Texto longo + TTS
        long_text = gerar_frase_motivacional_longa(idioma)
        lang_norm = _idioma_norm(idioma)
        style_norm = _normalize_style(video_style)

        voice_audio_path: Optional[str] = gerar_narracao_tts(long_text, idioma=lang_norm, engine=tts_engine)
        dur_voz = _duracao_audio_segundos(voice_audio_path) if voice_audio_path else None

        # === Stage do TTS para audios/tts (e vamos limpar depois) ===
        if voice_audio_path and os.path.isfile(voice_audio_path):
            try:
                base, ext = os.path.splitext(os.path.basename(voice_audio_path))
                staged_tts = os.path.join(AUDIO_TTS_DIR, f"{base}_{_uuid_suffix()}{ext or '.wav'}")
                shutil.copy2(voice_audio_path, staged_tts)
                # se o original estava em audios_tts/ raiz antiga, vamos limpar também
                old_parent = os.path.basename(os.path.dirname(voice_audio_path)).lower()
                if old_parent in ("audios_tts", "audio_tts"):
                    extra_to_cleanup.append(voice_audio_path)
                voice_audio_path = staged_tts  # usar o staged
            except Exception as e:
                logger.warning("Falha ao mover/copiar TTS para %s: %s. Usando original.", AUDIO_TTS_DIR, e)

        # Música de fundo (não limpar depois)
        background_audio_path: Optional[str] = None
        try:
            background_audio_path = obter_caminho_audio()
        except Exception as e:
            logger.warning("Sem áudio de fundo válido: %s", e)

        has_voice = bool(voice_audio_path)
        has_bg = bool(background_audio_path)

        # Segmentos de legenda
        segments: List[Tuple[float, float, str]] = []
        if legendas and has_voice:
            seg_whisper = _segmentos_via_whisper(voice_audio_path, lang_norm) if WHISPER_ENABLE else []
            segments = seg_whisper if seg_whisper else _segmentos_legenda_palavras(long_text, dur_voz or DURACAO_MAXIMA_VIDEO)
            logger.info("📝 %d segmentos de legenda.", len(segments))

        # Duração alvo do vídeo
        if segments:
            total_video = min(DURACAO_MAXIMA_VIDEO, max(8.0, segments[-1][1] + 0.45))
        elif dur_voz:
            total_video = min(DURACAO_MAXIMA_VIDEO, max(8.0, dur_voz + 0.25))
        else:
            total_video = min(DURACAO_MAXIMA_VIDEO, 12.0)

        # Durations por slide e transição
        trans = transition or DEFAULT_TRANSITION or "fade"
        per_slide = total_video / n_slides
        trans_dur = max(0.50, min(0.90, per_slide * 0.135)) if n_slides > 1 else 0.0

        logger.info("⚙️ legendas=%s | style='%s'(=%s) | motion=%s | slides=%d | transition=%s",
                    bool(segments), video_style, style_norm, motion, n_slides, trans)
        logger.info("⏱️ Durations: total≈%.2fs | slide≈%.3fs | trans=%.2fs | motion_fps=%d", total_video, per_slide, trans_dur, MOTION_FPS)

        # === Stage de TODAS as imagens para IMAGES_DIR e usar só esses caminhos >>> limpeza no final ===
        staged_inputs: List[str] = []
        for p in slides_paths:
            try:
                staged = _stage_to_dir(p, IMAGES_DIR, "stage")
                staged_inputs.append(staged)
                staged_images.append(staged)  # marcar para limpar
            except Exception as e:
                logger.warning("Falha ao encenar imagem '%s': %s (ignorando este slide)", p, e)
        if not staged_inputs:
            raise RuntimeError("Nenhuma imagem disponível para montar o vídeo.")

        # -------- construir filter_complex --------
        parts: List[str] = []

        # 1) Branch por slide
        for i in range(len(staged_inputs)):
            parts.append(_build_slide_branch(i, W, H, motion, per_slide))

        # 2) Transições xfade (streams já CFR e com PTS zerado)
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

        # 3) Normalização final + drawtext
        parts.append(f"{final_video_label}format=yuv420p,setsar=1/1,fps={FPS_OUT}[vf]")

        draw_chain = ""
        if segments:
            font_for_sub = _pick_drawtext_font(video_style)
            draw_chain = _build_drawtext_chain(H, style_norm, segments, font_for_sub)

        if draw_chain:
            parts.append(f"[vf]{draw_chain},format=yuv420p[vout]")
        else:
            parts.append(f"[vf]format=yuv420p[vout]")

        # 4) Áudio
        if has_voice and has_bg:
            va_idx = len(staged_inputs)
            ba_idx = len(staged_inputs) + 1
            voice_filters = []
            if VOICE_LOUDNORM:
                voice_filters.append("loudnorm=I=-15:TP=-1.0:LRA=11")
            voice_filters.append(f"aresample={AUDIO_SR}:async=1")
            vf_str = ",".join(voice_filters)

            parts.append(f"[{va_idx}:a]{vf_str}[va]")
            parts.append(f"[{ba_idx}:a]volume={BG_MIX_VOLUME},aresample={AUDIO_SR}:async=1[ba]")
            parts.append(f"[va][ba]amix=inputs=2:duration=longest:dropout_transition=0[aout]")
        elif has_voice or has_bg:
            a_idx = len(staged_inputs)
            if has_voice and VOICE_LOUDNORM:
                parts.append(f"[{a_idx}:a]loudnorm=I=-15:TP=-1.0:LRA=11,aresample={AUDIO_SR}:async=1[aout]")
            else:
                parts.append(f"[{a_idx}:a]aresample={AUDIO_SR}:async=1[aout]")
        else:
            parts.append(f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_SR}:d={total_video:.3f}[aout]")

        filter_complex = ";".join(parts)

        # dump do filtergraph para debug
        fc_path = os.path.join(CACHE_DIR, "last_filter.txt")
        with open(fc_path, "w", encoding="utf-8") as f:
            f.write(filter_complex)
        logger.info("🧩 filter_complex salvo em %s", fc_path)

        # --------- montar comando ffmpeg ----------
        cmd = [ffmpeg := _ffmpeg_or_die(), "-y", "-loglevel", "error", "-stats"]

        # inputs de vídeo (um por slide) — sempre a partir dos encenados em IMAGES_DIR
        for sp in staged_inputs:
            cmd += ["-loop", "1", "-i", sp]

        # inputs de áudio
        if has_voice and voice_audio_path:
            cmd += ["-i", voice_audio_path]
        if has_bg and background_audio_path:
            cmd += ["-i", background_audio_path]
            
        # ==================== NOVA SEÇÃO: METADADOS ====================
        # Gera o título a partir do início da frase
        title = (long_text[:75] + '...') if len(long_text) > 75 else long_text
        default_tags = "motivacional, inspirador, reflexão, shorts, reels, AI"

        metadata = {
            "title": title,
            "artist": autor or "Autor Desconhecido",
            "album_artist": autor or "Autor Desconhecido",
            "composer": autor or "Autor Desconhecido",
            "comment": long_text,
            "description": long_text,
            "keywords": tags if tags else default_tags,
            "genre": "Inspirational",
            "creation_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "software": "Gerador de Vídeo Automático"
        }
        
        metadata_flags = []
        for key, value in metadata.items():
            if value:
                metadata_flags.extend(["-metadata", f"{key}={value}"])
        # =================================================================

        # Saída — usar poucas threads ajuda a não travar
        common_out = [
            "-t", f"{total_video:.3f}",
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
            "-movflags", "+faststart",
            "-x264-params", f"keyint={FPS_OUT*2}:min-keyint={FPS_OUT*2}:scenecut=0",
            "-map_metadata", "-1",  # Limpa metadados dos arquivos de entrada
            "-threads", "2",
        ]

        cmd += ["-filter_complex", filter_complex, "-map", "[vout]", "-map", "[aout]"]
        cmd += common_out
        cmd += metadata_flags  # <--- ADICIONA AS FLAGS DE METADADOS AQUI

        # saída
        if os.path.isdir(saida_path):
            base = os.path.splitext(os.path.basename(imagem_path))[0]
            ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            saida_path = os.path.join(saida_path, f"{base}-{ts}.mp4")
        os.makedirs(os.path.dirname(saida_path) or ".", exist_ok=True)
        cmd.append(saida_path)

        # salvar o comando para uso no except
        last_cmd = cmd[:]

        logger.info("📂 CWD: %s", os.getcwd())
        logger.info("🎬 FFmpeg: %s", " ".join(_quote(a) for a in cmd))

        subprocess.run(cmd, check=True)
        logger.info("✅ Vídeo salvo: %s", saida_path)

    except subprocess.CalledProcessError as e:
        logger.error("❌ FFmpeg falhou (%s).", e)
        # salva scripts de replay para facilitar teste manual
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
        # -------- LIMPEZA: imagens encenadas + TTS copiado + resíduos antigos --------
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

        # remove diretórios vazios opcionais
        for d in (AUDIO_TTS_DIR,):
            try:
                if os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
            except Exception:
                pass