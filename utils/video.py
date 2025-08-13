# utils/video.py

import os
import re
import logging
import datetime
import shutil
import subprocess
from typing import Optional

from dotenv import load_dotenv
from .frase import gerar_frase_motivacional_longa
from .audio import obter_caminho_audio, gerar_narracao_tts

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

load_dotenv()

PRESETS = {
    "sd":     {"w": 540,  "h": 960,  "br_v": "1400k", "br_a": "128k", "level": "3.1"},
    "hd":     {"w": 720,  "h": 1280, "br_v": "2200k", "br_a": "128k", "level": "3.1"},
    "fullhd": {"w": 1080, "h": 1920, "br_v": "5000k", "br_a": "192k", "level": "4.0"},
}

DURACAO_MAXIMA_VIDEO = 20.0
FPS = 30
AUDIO_SR = 44100

# ----------------- helpers -----------------
def _nome_limpo(base: str) -> str:
    base = (base or "video").strip().lower()
    base = re.sub(r"\s+", "-", base)
    base = re.sub(r"[^a-z0-9\-_]", "", base)
    return base or "video"

def _ffmpeg_or_die() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("ffmpeg não encontrado no PATH. Instale o FFmpeg.")
    return path

def _idioma_norm(idioma: str) -> str:
    s = (idioma or "en").lower()
    return "pt" if s.startswith("pt") else "en"

# --------------- principal -----------------
def gerar_video(imagem_path: str, saida_path: str, preset: str = "hd", idioma: str = "auto"):
    """
    Gera MP4 vertical (TikTok/Android) rápido via FFmpeg:
    - Resol. por preset (sd/hd/fullhd), 30fps, H.264 yuv420p, AAC 44.1kHz
    - +faststart; keyframe a cada 2s
    - Duração máx. 20s
    - Narração TTS (ElevenLabs) + música de fundo (Freesound/local)
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
    BR_V, BR_A, LEVEL = conf["br_v"], conf["br_a"], conf["level"]

    # Nome de saída automático se 'saida_path' for diretório
    if os.path.isdir(saida_path):
        base = _nome_limpo(os.path.splitext(os.path.basename(imagem_path))[0])
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        saida_path = os.path.join(saida_path, f"{base}-{ts}.mp4")
    os.makedirs(os.path.dirname(saida_path) or ".", exist_ok=True)

    # Texto longo
    long_text = gerar_frase_motivacional_longa(idioma)
    lang_norm = _idioma_norm(idioma)

    # TTS (narração)
    voice_audio_path: Optional[str] = gerar_narracao_tts(long_text, idioma=lang_norm)

    # Música de fundo
    background_audio_path: Optional[str] = None
    try:
        background_audio_path = obter_caminho_audio()
    except Exception as e:
        logger.warning("Sem áudio de fundo válido: %s", e)

    has_voice = bool(voice_audio_path)
    has_bg = bool(background_audio_path)

    # Filtro de vídeo
    vf = (
        f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={FPS},format=yuv420p"
    )

    # Inputs
    cmd = [
        ffmpeg, "-y",
        "-loglevel", "error", "-stats",
        "-loop", "1", "-i", imagem_path,  # 0: vídeo (imagem)
    ]

    # ordem: 1 = voz (se existir), 2 = bg (se existir)
    if has_voice:
        cmd.extend(["-i", voice_audio_path])      # 1
    if has_bg:
        cmd.extend(["-i", background_audio_path]) # 2

    if not has_voice and not has_bg:
        # gera silêncio (estéreo) como input 1
        cmd.extend(["-f", "lavfi", "-t", str(DURACAO_MAXIMA_VIDEO),
                    "-i", f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_SR}"])

    # Parâmetros comuns de encodes
    cmd.extend([
        "-t", str(DURACAO_MAXIMA_VIDEO),
        "-r", str(FPS),
        "-c:v", "libx264",
        "-preset", "superfast",
        "-tune", "stillimage",
        "-b:v", BR_V,
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
        # "-shortest",  # opcional se quiser terminar no stream mais curto
    ])

    # Mapeamento/Mixagem
    if has_voice and has_bg:
        # 0:v = vídeo; 1:a = voz; 2:a = bg
        cmd.extend([
            "-filter_complex",
            "[1:a]volume=1.0[va];[2:a]volume=0.20[ba];[va][ba]amix=inputs=2:duration=longest:dropout_transition=0[aout]",
            "-map", "0:v",
            "-map", "[aout]",
        ])
    else:
        # quando há 1 único áudio ou silêncio (input 1)
        cmd.extend(["-map", "0:v", "-map", "1:a"])

    cmd.append(saida_path)

    logger.info("🎬 FFmpeg gerando %s (%dx%d, %s/%s, %dfps) → %s",
                preset.upper(), W, H, BR_V, BR_A, FPS, saida_path)

    try:
        subprocess.run(cmd, check=True)
        logger.info("✅ Vídeo salvo com sucesso: %s", saida_path)
    except subprocess.CalledProcessError as e:
        logger.error("❌ FFmpeg falhou (%s).\nComando: %s", e, " ".join(cmd))
    finally:
        # Se quiser limpar TTS temporário (quando gravado fora de audios_tts)
        pass
