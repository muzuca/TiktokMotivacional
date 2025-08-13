# utils/video.py

import os
import re
import logging
import datetime
import shutil
import subprocess
from dotenv import load_dotenv
from typing import Optional
from elevenlabs.client import ElevenLabs
from elevenlabs import stream
from .frase import gerar_frase_motivacional_longa  # Importar a função de frase longa

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# Carregar variáveis de ambiente
load_dotenv()

# -----------------------------------------------------------------------------
# Presets (resolução/bitrates)
# -----------------------------------------------------------------------------
PRESETS = {
    "sd":     {"w": 540,  "h": 960,  "br_v": "1400k", "br_a": "128k", "level": "3.1"},
    "hd":     {"w": 720,  "h": 1280, "br_v": "2200k", "br_a": "128k", "level": "3.1"},
    "fullhd": {"w": 1080, "h": 1920, "br_v": "5000k", "br_a": "192k", "level": "4.0"},
}

# Perfil comum
DURACAO_MAXIMA_VIDEO = 20.0   # Aumentado para 20s para suportar narração longa
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

def _try_get_audio_path() -> Optional[str]:
    try:
        from utils.audio import obter_caminho_audio
        p = obter_caminho_audio()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    return None

def generate_tts_audio(text: str, output_path: str, idioma: str) -> Optional[str]:
    """Gera áudio usando a API do ElevenLabs com base no idioma."""
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        logger.warning("Chave API do ElevenLabs não encontrada. Usando fallback de áudio.")
        return None

    client = ElevenLabs(api_key=api_key)

    # Seleção de voice_id com base no idioma
    voice_ids = {
        "en": "y2Y5MeVPm6ZQXK64WUui",
        "pt": "rnJZLKxtlBZt77uIED10"
    }
    voice_id = voice_ids.get(idioma.lower()[:2], "BNgbHR0DNeZixGQVzloa")  # Fallback para o fornecido

    model_id = "eleven_multilingual_v2"  # Modelo multilingual

    try:
        audio_stream = client.text_to_speech.stream(
            text=text,
            voice_id=voice_id,
            model_id=model_id
        )

        with open(output_path, "wb") as f:
            for chunk in audio_stream:
                if isinstance(chunk, bytes) and chunk:
                    f.write(chunk)

        if os.path.exists(output_path):
            logger.info("🎧 Áudio gerado com ElevenLabs: %s", output_path)
            return output_path
        else:
            logger.warning("Falha ao salvar áudio gerado com ElevenLabs.")
            return None
    except Exception as e:
        logger.warning("Erro na API do ElevenLabs: %s. Usando fallback.", str(e))
        return None

# -----------------------------------------------------------------------------
# Principal
# -----------------------------------------------------------------------------
def gerar_video(imagem_path: str, saida_path: str, preset: str = "hd", idioma: str = "auto"):
    """
    Gera MP4 compatível com TikTok/Android rapidamente usando FFmpeg:
      - Resolução: preset ('sd'=540x960, 'hd'=720x1280, 'fullhd'=1080x1920)
      - H.264 yuv420p, CFR 30 fps, AAC 44.1 kHz
      - +faststart, keyframe a cada 2s
      - Duração: até 20s (ou menos se o áudio for mais curto)
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

    # Gerar texto motivacional longo usando frase.py
    long_text = gerar_frase_motivacional_longa(idioma)

    # Gerar áudio de narração com ElevenLabs
    voice_audio_path = None
    if idioma.lower() in ("en", "en-us", "us", "usa", "eua", "ingles", "inglês", "english"):
        voice_audio_path = "temp_voice_en.mp3"
        voice_audio_path = generate_tts_audio(long_text, voice_audio_path, "en")
    elif idioma.lower() in ("pt", "pt-br", "br", "brasil", "portugues", "português"):
        voice_audio_path = "temp_voice_pt.mp3"
        voice_audio_path = generate_tts_audio(long_text, voice_audio_path, "pt")

    # Obter música de fundo do Freesound
    background_audio_path = _try_get_audio_path()
    if not background_audio_path:
        logger.warning("Nenhum áudio de fundo encontrado. Usando fallback silencioso.")

    has_voice = bool(voice_audio_path)
    has_background = bool(background_audio_path)

    # Filtro de vídeo: scale + pad + fps + yuv420p
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

    audio_inputs = []
    if has_voice:
        audio_inputs.append(voice_audio_path)
    if has_background:
        audio_inputs.append(background_audio_path)

    if audio_inputs:
        cmd.extend(["-i", audio_inputs[0]])
        if len(audio_inputs) > 1:
            cmd.extend(["-i", audio_inputs[1]])
    else:
        # trilha silenciosa (estéreo 44.1 kHz) para manter compat
        cmd.extend(["-f", "lavfi", "-t", str(DURACAO_MAXIMA_VIDEO), "-i",
                   f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_SR}"])

    cmd.extend([
        "-t", str(DURACAO_MAXIMA_VIDEO),          # limite duro 20s
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
    ])

    # === MAPEAMENTO/MIXAGEM DE ÁUDIO ===
    if has_voice and has_background:
        # Índices: 0 = imagem (vídeo), 1 = voz, 2 = música de fundo
        cmd.extend([
            "-filter_complex",
            "[1:a]volume=1.0[va];[2:a]volume=0.20[ba];[va][ba]amix=inputs=2:duration=longest:dropout_transition=0[aout]",
            "-map", "0:v",      # vídeo (da imagem)
            "-map", "[aout]",   # áudio mixado (voz + música)
        ])
    elif has_voice:
        # 0 = vídeo, 1 = voz
        cmd.extend(["-map", "0:v", "-map", "1:a"])
    elif has_background:
        # 0 = vídeo, 1 = música (quando só ela foi adicionada)
        cmd.extend(["-map", "0:v", "-map", "1:a"])
    else:
        # trilha silenciosa entrou como input 1 (lavfi anullsrc)
        cmd.extend(["-map", "0:v", "-map", "1:a"])


    cmd.append(saida_path)

    logger.info("🎬 FFmpeg gerando %s (%dx%d, %s/%s, %dfps) → %s",
                preset.upper(), W, H, BR_V, BR_A, FPS, saida_path)
    try:
        subprocess.run(cmd, check=True)
        logger.info("✅ Vídeo salvo com sucesso: %s", saida_path)
    except subprocess.CalledProcessError as e:
        logger.error("❌ FFmpeg falhou (%s). Comando: %s", e, " ".join(cmd))
    finally:
        # Limpar arquivos temporários de áudio
        for temp_path in ["temp_voice_en.mp3", "temp_voice_pt.mp3"]:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass