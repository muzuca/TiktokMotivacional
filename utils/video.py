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

from .frase import gerar_frase_motivacional_longa
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

# Caminhos RELATIVOS simples
FONTS_DIR = "fonts"
CACHE_DIR = "cache"
os.makedirs(CACHE_DIR, exist_ok=True)

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

# ---------- Segmentação das legendas (2–3 palavras por bloco, 1 linha) ----------
def _tokenize_words(text: str) -> List[str]:
    # preserva acentos; remove quebras de linha; mantém apóstrofo como parte da palavra
    text = re.sub(r"[\r\n]+", " ", text).strip()
    tokens = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9'’\-]+", text)
    return [t for t in tokens if t]

def _chunk_words(tokens: List[str]) -> List[List[str]]:
    """
    Quebra em blocos de 2 ou 3 palavras.
    Se houver palavra longa (>=10 chars) no bloco, limita a 2.
    """
    chunks: List[List[str]] = []
    i = 0
    n = len(tokens)
    while i < n:
        # tenta 3
        take = 3
        long_word = any(len(tokens[j]) >= 10 for j in range(i, min(i + 3, n)))
        if long_word:
            take = 2
        # evita ultrapassar fim
        if i + take > n:
            take = max(1, n - i)
        chunk = tokens[i:i + take]
        chunks.append(chunk)
        i += take
    return chunks

def _distribuir_duracoes_por_palavra(frases: List[str], total_disp: float) -> List[float]:
    """
    Duração proporcional à contagem de palavras, clamp 0.7–3.0s (para blocos curtíssimos),
    normalizada ao total disponível.
    """
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
    """
    Constrói segmentos 1-linha com 2–3 palavras em CAIXA ALTA.
    """
    tokens = _tokenize_words(texto)
    if not tokens:
        return []

    chunks = _chunk_words(tokens)
    lines = [" ".join(ch).upper() for ch in chunks]

    alvo = max(3.0, min(DURACAO_MAXIMA_VIDEO - 0.3, (duracao_audio or DURACAO_MAXIMA_VIDEO) - 0.2))
    gap = 0.10  # troca rápida entre blocos
    duracoes = _distribuir_duracoes_por_palavra(lines, alvo - gap * (len(lines) - 1))

    t = 0.25
    segs: List[Tuple[float, float, str]] = []
    for line, d in zip(lines, duracoes):
        ini = t
        fim = t + d
        segs.append((ini, fim, line))
        t = fim + gap
    return segs

# ---------------- estilos de vídeo (legendas) ----------------
VIDEO_STYLES = {
    "1": {"key": "minimal_compact", "label": "Compact (sem caixa, contorno fino)"},
    "2": {"key": "clean_outline",   "label": "Clean (um pouco maior)"},
    "3": {"key": "tiny_outline",    "label": "Tiny (bem pequeno)"},
    # apelidos aceitos
    "classic": "1",
    "modern":  "2",
    "serif":   "2",
    "clean":   "2",
    "mono":    "3",
}

def _normalize_style(style: str) -> str:
    s = (style or "1").strip().lower()
    if s in ("1", "2", "3"):
        return s
    return str(VIDEO_STYLES.get(s, "1"))

def listar_estilos_video() -> List[Tuple[str, str]]:
    return [(k, v["label"]) for k, v in VIDEO_STYLES.items() if k in ("1","2","3")]

def _first_existing_font(*names: str) -> Optional[str]:
    for n in names:
        p = os.path.join(FONTS_DIR, n)
        if os.path.isfile(p):
            return p
    return None

def _pick_drawtext_font(style_key: str) -> Optional[str]:
    s = (style_key or "").lower()
    if s in ("classic", "1", "clean", "5"):
        # Montserrat primeiro; se faltar, cai para Bebas; depois tenta variações.
        return _first_existing_font(
            "Montserrat-Regular.ttf",
            "BebasNeue-Regular.ttf",
            "Montserrat-Reegular.ttf",   # (corrige nomes “typo”)
            "Inter-Bold.ttf"
        )
    if s in ("modern", "2"):
        return _first_existing_font("BebasNeue-Regular.ttf", "Inter-Bold.ttf", "Montserrat-ExtraBold.ttf")
    if s in ("serif", "3"):
        return _first_existing_font("PlayfairDisplay-Regular.ttf", "Cinzel-Bold.ttf")
    if s in ("mono", "4"):
        return _first_existing_font("Inter-Bold.ttf", "Montserrat-Regular.ttf")
    return _first_existing_font("Montserrat-Regular.ttf", "BebasNeue-Regular.ttf", "Inter-Bold.ttf")


def _ff_normpath(path: str) -> str:
    try:
        rel = os.path.relpath(path)
    except Exception:
        rel = path
    return rel.replace("\\", "/")

def _write_textfile(content: str, idx: int) -> str:
    path = os.path.join(CACHE_DIR, f"drawtext_{idx:02d}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path

def _build_drawtext_chain(H: int,
                          style_id: str,
                          segments: List[Tuple[float, float, str]],
                          font_path: Optional[str]) -> str:
    """
    Monta a cadeia de drawtext usando textfile= (estável no Windows).
    Segmentos são 1 linha; centralizados no rodapé.
    """
    sid = _normalize_style(style_id)

    if sid == "1":       # compacto
        fs = max(18, int(H * 0.024))
        borderw = 1
        margin = max(48, int(H * 0.10))
    elif sid == "2":     # um pouco maior
        fs = max(18, int(H * 0.030))
        borderw = 2
        margin = max(54, int(H * 0.115))
    else:                # tiny
        fs = max(16, int(H * 0.023))
        borderw = 1
        margin = max(56, int(H * 0.12))

    use_fontfile = (font_path and os.path.isfile(font_path))
    if use_fontfile:
        font_opt = f":fontfile={_ff_normpath(font_path)}"
        logger.info("🔤 drawtext usando fonte: %s", font_path)
    else:
        font_opt = ""
        logger.info("🔤 drawtext sem fontfile explícito (usando fonte do sistema).")

    blocks = []
    for idx, (ini, fim, txt) in enumerate(segments, start=1):
        tf_path = _write_textfile(txt, idx)
        tf_posix = _ff_normpath(tf_path)
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

# --------------- principal ----------------
def gerar_video(imagem_path: str,
                saida_path: str,
                preset: str = "hd",
                idioma: str = "auto",
                tts_engine: str = "gemini",
                legendas: bool = True,
                video_style: str = "1"):
    """
    Gera MP4 vertical:
      - 30fps, H.264 yuv420p, AAC 44.1kHz
      - +faststart; keyframe a cada 2s
      - Duração máx. 20s
      - Narração TTS (Gemini/ElevenLabs) + música de fundo
      - (opcional) Legendas em blocos de 2–3 palavras (1 linha) via drawtext+textfile
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
    style_norm = _normalize_style(video_style)
    logger.info("⚙️ Opções: legendas=%s | video_style='%s' (norm=%s)", legendas, video_style, style_norm)

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

    # Segmentos de legenda – blocos de 2–3 palavras
    segments: List[Tuple[float, float, str]] = []
    if legendas and has_voice:
        segments = _segmentos_legenda_palavras(long_text, dur_voz or DURACAO_MAXIMA_VIDEO)
        logger.info("📝 %d segmentos de legenda (word-chunks).", len(segments))

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

    # Parâmetros comuns de encode
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

    # Precisamos de filter_complex se houver drawtext (legendas) ou mix (voz+bg).
    need_fc = bool(segments) or (has_voice and has_bg)

    if need_fc:
        # Cadeia de vídeo
        vchain = f"[0:v]{vf_base}"
        if segments:
            font_for_sub = _pick_drawtext_font(video_style)
            vchain += "," + _build_drawtext_chain(H, style_norm, segments, font_for_sub)
        vchain += "[vout]"

        # Cadeia de áudio (mix ou direto)
        if has_voice and has_bg:
            achain = "[1:a]volume=1.0[va];[2:a]volume=0.15[ba];[va][ba]amix=inputs=2:duration=longest:dropout_transition=0[aout]"
        else:
            achain = "[1:a]anull[aout]"

        cmd.extend([
            "-filter_complex", f"{vchain};{achain}",
            "-map", "[vout]", "-map", "[aout]",
        ])
        cmd.extend(common_out)
    else:
        # Sem legendas e sem mix — usa -vf direto e mapeia 1:a
        cmd.extend(["-vf", vf_base, "-map", "0:v", "-map", "1:a"])
        cmd.extend(common_out)

    cmd.append(saida_path)

    logger.info("📂 CWD no momento do ffmpeg: %s", os.getcwd())
    logger.info("🎬 FFmpeg gerando %s (%dx%d, %s/%s, %dfps) → %s",
                preset.upper(), W, H, BR_V, BR_A, FPS, saida_path)

    try:
        subprocess.run(cmd, check=True)
        logger.info("✅ Vídeo salvo: %s", saida_path)
    except subprocess.CalledProcessError as e:
        logger.error("❌ FFmpeg falhou (%s).\nComando: %s", e, " ".join(cmd))
