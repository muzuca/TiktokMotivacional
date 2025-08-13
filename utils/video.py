# utils/video.py

import os
import re
import logging
import datetime
import shutil
import subprocess

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Presets (resolução/bitrates)
# -----------------------------------------------------------------------------
PRESETS = {
    "sd":     {"w": 540,  "h": 960,  "br_v": "1400k", "br_a": "128k", "level": "3.1"},
    "hd":     {"w": 720,  "h": 1280, "br_v": "2200k", "br_a": "128k", "level": "3.1"},
    "fullhd": {"w": 1080, "h": 1920, "br_v": "5000k", "br_a": "192k", "level": "4.0"},
}

# Perfil comum
DURACAO_MAXIMA_VIDEO = 10.0   # s
FPS = 30                      # CFR 30
AUDIO_SR = 44100              # 44.1 kHz

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _nome_limpo(base: str) -> str:
    base = (base or "video").strip().lower()
    base = re.sub(r"\s+", "-", base)
    base = re.sub(r"[^a-z0-9\-_]", "", base)
    return base or "video"

def _ffmpeg_or_die() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("ffmpeg não encontrado no PATH. Instale o FFmpeg e tente novamente.")
    return path

def _try_get_audio_path():
    try:
        from utils.audio import obter_caminho_audio
        p = obter_caminho_audio()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    return None

# -----------------------------------------------------------------------------
# Principal
# -----------------------------------------------------------------------------
def gerar_video(imagem_path: str, saida_path: str, preset: str = "hd"):
    """
    Gera MP4 compatível com TikTok/Android rapidamente usando FFmpeg:
      - Resolução: preset ('sd'=540x960, 'hd'=720x1280, 'fullhd'=1080x1920)
      - H.264 yuv420p, CFR 30 fps, AAC 44.1 kHz
      - +faststart, keyframe a cada 2s
      - Duração: até 10s (ou menos se o áudio for mais curto)
    """
    if preset not in PRESETS:
        logger.warning("Preset '%s' inválido. Usando 'hd'.", preset)
        preset = "hd"

    if not os.path.isfile(imagem_path):
        logger.error("❌ Imagem não encontrada: %s", imagem_path)
        return

    ffmpeg = _ffmpeg_or_die()

    conf = PRESETS[preset]
    W, H = conf["w"], conf["h"]
    BR_V = conf["br_v"]
    BR_A = conf["br_a"]
    LEVEL = conf["level"]

    # Se 'saida_path' for diretório, cria nome automático
    if os.path.isdir(saida_path):
        base = _nome_limpo(os.path.splitext(os.path.basename(imagem_path))[0])
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        saida_path = os.path.join(saida_path, f"{base}-{ts}.mp4")

    os.makedirs(os.path.dirname(saida_path) or ".", exist_ok=True)

    audio_path = _try_get_audio_path()
    has_audio = bool(audio_path)

    # Filtro de vídeo: scale + pad + fps + yuv420p
    # - scale força proporção 9:16 sem distorcer (decrease)
    # - pad centra com barras
    vf = (
        f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={FPS},format=yuv420p"
    )

    # Monta comando
    cmd = [
        ffmpeg, "-y",
        "-loglevel", "error", "-stats",
        "-loop", "1", "-i", imagem_path,
    ]

    if has_audio:
        cmd += ["-i", audio_path]
    else:
        # trilha silenciosa (estéreo 44.1 kHz) para manter compat
        cmd += ["-f", "lavfi", "-t", str(DURACAO_MAXIMA_VIDEO), "-i",
                f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_SR}"]

    cmd += [
        "-t", str(DURACAO_MAXIMA_VIDEO),          # limite duro 10s
        "-r", str(FPS),                           # CFR
        "-c:v", "libx264",
        "-preset", "superfast",                   # bem rápido
        "-tune", "stillimage",                    # otimiza para quadro estático
        "-b:v", BR_V,                             # taxa alvo (1-pass)
        "-maxrate", BR_V, "-bufsize", "6M",
        "-profile:v", "high",
        "-level", LEVEL,
        "-vf", vf,
        "-c:a", "aac",
        "-b:a", BR_A,
        "-ar", str(AUDIO_SR),
        "-ac", "2",
        "-movflags", "+faststart",
        "-x264-params", f"keyint={FPS*2}:min-keyint={FPS*2}:scenecut=0",
        "-map_metadata", "-1",
        "-shortest",                               # se áudio for menor, para junto
        saida_path
    ]

    logger.info("🎬 FFmpeg gerando %s (%dx%d, %s/%s, %dfps) → %s",
                preset.upper(), W, H, BR_V, BR_A, FPS, saida_path)
    try:
        subprocess.run(cmd, check=True)
        logger.info("✅ Vídeo salvo com sucesso: %s", saida_path)
    except subprocess.CalledProcessError as e:
        logger.error("❌ FFmpeg falhou (%s). Comando: %s", e, " ".join(cmd))
