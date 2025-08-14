# utils/video.py

import os
import re
import math
import logging
import datetime
import shutil
import subprocess
from typing import Optional, List, Tuple
from dotenv import load_dotenv

from .frase import gerar_frase_motivacional_longa, quebrar_em_duas_linhas
from .audio import obter_caminho_audio, gerar_narracao_tts

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

# ---------------- helpers ----------------
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

def _ffprobe_or_die() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise RuntimeError("ffprobe não encontrado no PATH. Instale o FFmpeg (inclui ffprobe).")
    return path

def _idioma_norm(idioma: str) -> str:
    s = (idioma or "en").lower()
    return "pt" if s.startswith("pt") else "en"

def _duracao_audio_segundos(audio_path: str) -> Optional[float]:
    """Obtém duração do áudio via ffprobe (segundos)."""
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

def _split_em_frases(texto: str) -> List[str]:
    """
    Divide por pontuação forte. Se vier uma frase única, quebra em blocos de ~12 palavras.
    """
    texto = (texto or "").strip()
    if not texto:
        return []
    partes = re.split(r'(?<=[\.\!\?\:;])\s+', texto)
    partes = [p.strip() for p in partes if p.strip()]
    if len(partes) <= 1:
        # fallback por palavras
        palavras = texto.split()
        if not palavras:
            return []
        blocos, passo = [], 12
        for i in range(0, len(palavras), passo):
            blocos.append(" ".join(palavras[i:i+passo]))
        return blocos
    return partes

def _tempo_srt(seg: float) -> str:
    if seg < 0: seg = 0
    h = int(seg // 3600)
    m = int((seg % 3600) // 60)
    s = int(seg % 60)
    ms = int((seg - math.floor(seg)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def _distribuir_duracoes_por_palavra(frases: List[str], total_disp: float) -> List[float]:
    """
    Duração proporcional à contagem de palavras, com clamp de 1.2–5.5s, normalizada ao total.
    """
    if not frases:
        return []
    min_seg, max_seg = 1.2, 5.5
    pesos = [max(1, len(f.split())) for f in frases]
    soma = sum(pesos)
    brutas = [(p / soma) * total_disp for p in pesos]
    clamped = [min(max(b, min_seg), max_seg) for b in brutas]
    fator = min(1.0, (total_disp / sum(clamped))) if sum(clamped) > 0 else 1.0
    return [d * fator for d in clamped]

def _gerar_srt(texto: str, duracao_audio: float, saida_dir: str, base_nome: str) -> Optional[str]:
    """
    Gera um .srt com janelas distribuídas ao longo da duração da narração.
    Retorna o caminho do .srt ou None.
    """
    try:
        frases = _split_em_frases(texto)
        if not frases:
            return None

        # reserva respiro no início/fim
        alvo = max(3.0, min(DURACAO_MAXIMA_VIDEO - 0.3, (duracao_audio or DURACAO_MAXIMA_VIDEO) - 0.2))
        gap = 0.15
        duracoes = _distribuir_duracoes_por_palavra(frases, alvo - gap * (len(frases) - 1))

        t = 0.25  # leve atraso antes da 1ª legenda
        blocos: List[Tuple[str, str, str]] = []
        for frase, d in zip(frases, duracoes):
            ini = t
            fim = t + d
            t = fim + gap
            # quebra em duas linhas legíveis
            frase2l = quebrar_em_duas_linhas(frase)
            blocos.append((_tempo_srt(ini), _tempo_srt(fim), frase2l))

        os.makedirs(saida_dir, exist_ok=True)
        srt_path = os.path.join(saida_dir, f"{base_nome}.srt")
        with open(srt_path, "w", encoding="utf-8") as srt:
            for idx, (ini, fim, txt) in enumerate(blocos, start=1):
                srt.write(f"{idx}\n{ini} --> {fim}\n{txt}\n\n")
        return srt_path
    except Exception as e:
        logger.warning("Falha ao gerar SRT: %s", e)
        return None

# --------------- principal ----------------
def gerar_video(imagem_path: str,
                saida_path: str,
                preset: str = "hd",
                idioma: str = "auto",
                tts_engine: str = "gemini",
                legendas: bool = True):
    """
    Gera MP4 vertical (TikTok/Android) via FFmpeg:
      - 30fps, H.264 yuv420p, AAC 44.1kHz
      - +faststart; keyframe a cada 2s
      - Duração máx. 20s
      - Narração TTS (Gemini/ElevenLabs) + música de fundo
      - (opcional) Legendas queimadas sincronizadas com a narração
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

    # Nome de saída automático (se diretório)
    if os.path.isdir(saida_path):
        base = _nome_limpo(os.path.splitext(os.path.basename(imagem_path))[0])
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        saida_path = os.path.join(saida_path, f"{base}-{ts}.mp4")
    os.makedirs(os.path.dirname(saida_path) or ".", exist_ok=True)

    # Texto longo + TTS
    long_text = gerar_frase_motivacional_longa(idioma)
    lang_norm = _idioma_norm(idioma)
    voice_audio_path: Optional[str] = gerar_narracao_tts(long_text, idioma=lang_norm, engine=tts_engine)
    dur_voz = _duracao_audio_segundos(voice_audio_path) if voice_audio_path else None

    # Música de fundo
    background_audio_path: Optional[str] = None
    try:
        background_audio_path = obter_caminho_audio()
    except Exception as e:
        logger.warning("Sem áudio de fundo válido: %s", e)

    has_voice = bool(voice_audio_path)
    has_bg = bool(background_audio_path)

    # Gerar SRT (se narração + legendas ativadas)
    srt_path: Optional[str] = None
    if legendas and has_voice:
        base_nome = os.path.splitext(os.path.basename(saida_path))[0]
        out_dir = os.path.dirname(saida_path) or "."
        srt_path = _gerar_srt(long_text, dur_voz or DURACAO_MAXIMA_VIDEO, out_dir, base_nome)

    # Filtro de vídeo base
    vf_base = (
        f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={FPS},format=yuv420p"
    )

    # Inputs
    cmd = [
        ffmpeg, "-y",
        "-loglevel", "error", "-stats",
        "-loop", "1", "-i", imagem_path,  # 0:v
    ]
    if has_voice:
        cmd.extend(["-i", voice_audio_path])      # 1:a
    if has_bg:
        cmd.extend(["-i", background_audio_path]) # 2:a
    if not has_voice and not has_bg:
        cmd.extend([
            "-f", "lavfi", "-t", str(DURACAO_MAXIMA_VIDEO),
            "-i", f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_SR}"
        ])  # 1:a (silêncio)

    # Encode comum
    common_out = [
        "-t", str(DURACAO_MAXIMA_VIDEO),
        "-r", str(FPS),
        "-c:v", "libx264",
        "-preset", "superfast",
        "-tune", "stillimage",
        "-b:v", BR_V,
        "-maxrate", BR_V, "-bufsize", "6M",
        "-profile:v", "high",
        "-level", LEVEL,
        "-c:a", "aac",
        "-b:a", BR_A,
        "-ar", str(AUDIO_SR),
        "-ac", "2",
        "-movflags", "+faststart",
        "-x264-params", f"keyint={FPS*2}:min-keyint={FPS*2}:scenecut=0",
        "-map_metadata", "-1",
    ]

    # Precisamos de filter_complex se houver SRT (subtitles) ou mix (voz+bg).
    need_fc = bool(srt_path) or (has_voice and has_bg)

    if need_fc:
        # Cadeia de vídeo
        vchain = f"[0:v]{vf_base}"
        if srt_path:
            # Use caminho relativo com barras / para evitar problemas no Windows
            srt_rel = os.path.relpath(srt_path).replace("\\", "/")
            vchain += f",subtitles={srt_rel}"
        vchain += "[vout]"

        # Cadeia de áudio
        if has_voice and has_bg:
            achain = "[1:a]volume=1.0[va];[2:a]volume=0.15[ba];[va][ba]amix=inputs=2:duration=longest:dropout_transition=0[aout]"
        else:
            # Há somente 1 fonte de áudio (voz OU bg OU silêncio)
            # O índice 1 é sempre o primeiro áudio adicionado ao cmd
            achain = "[1:a]anull[aout]"

        cmd.extend([
            "-filter_complex", f"{vchain};{achain}",
            "-map", "[vout]", "-map", "[aout]",
        ])
        cmd.extend(common_out)
    else:
        # Sem subtitles e sem mix — podemos usar -vf e mapear 1:a direto
        cmd.extend(["-vf", vf_base, "-map", "0:v", "-map", "1:a"])
        cmd.extend(common_out)

    cmd.append(saida_path)

    logger.info("🎬 FFmpeg gerando %s (%dx%d, %s/%s, %dfps) → %s",
                preset.upper(), W, H, BR_V, BR_A, FPS, saida_path)

    try:
        subprocess.run(cmd, check=True)
        logger.info("✅ Vídeo salvo: %s", saida_path)
    except subprocess.CalledProcessError as e:
        logger.error("❌ FFmpeg falhou (%s).\nComando: %s", e, " ".join(cmd))
