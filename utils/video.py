# utils/video.py

import os
import re
import math
import glob
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

# =============================================================================
# Presets de encode
# =============================================================================
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

# =============================================================================
# Utilidades gerais
# =============================================================================
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

def _ff_normpath(path: str) -> str:
    """Gera caminho RELATIVO com '/' (estável no Windows para filtergraph)."""
    try:
        rel = os.path.relpath(path)
    except Exception:
        rel = path
    return rel.replace("\\", "/")

def _clear_drawtext_cache():
    """Remove cache/drawtext_*.txt antigos para evitar lixo."""
    for f in glob.glob(os.path.join(CACHE_DIR, "drawtext_*.txt")):
        try:
            os.remove(f)
        except Exception:
            pass

# =============================================================================
# Tipografia / estilos
# =============================================================================
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
    """
    classic/1: Montserrat-Regular → fallback BebasNeue-Regular (pedido seu)
    """
    s = (style_key or "").lower()
    if s in ("classic", "1", "clean", "5"):
        return _first_existing_font(
            "Montserrat-Regular.ttf",
            "BebasNeue-Regular.ttf",      # fallback direto
            "Montserrat-Reegular.ttf",    # tolera typo
            "Inter-Bold.ttf"
        )
    if s in ("modern", "2"):
        return _first_existing_font("BebasNeue-Regular.ttf", "Inter-Bold.ttf", "Montserrat-ExtraBold.ttf")
    if s in ("serif", "3"):
        return _first_existing_font("PlayfairDisplay-Regular.ttf", "Cinzel-Bold.ttf")
    if s in ("mono", "4"):
        return _first_existing_font("Inter-Bold.ttf", "Montserrat-Regular.ttf")
    return _first_existing_font("Montserrat-Regular.ttf", "BebasNeue-Regular.ttf", "Inter-Bold.ttf")

# =============================================================================
# Legendas sincronizadas — via faster-whisper (ASR no TTS)
# =============================================================================
_ASR_MODEL = None
def _load_asr_model():
    global _ASR_MODEL
    if _ASR_MODEL is not None:
        return _ASR_MODEL
    try:
        from faster_whisper import WhisperModel
    except Exception as e:
        logger.warning("faster-whisper não disponível: %s", e)
        _ASR_MODEL = False
        return _ASR_MODEL

    model_size = os.getenv("WHISPER_MODEL", "base")
    device = os.getenv("WHISPER_DEVICE", None)
    if not device:
        # usa CUDA se disponível
        device = "cuda" if shutil.which("nvidia-smi") else "cpu"
    compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "float16" if device == "cuda" else "int8")

    try:
        _ASR_MODEL = WhisperModel(model_size, device=device, compute_type=compute_type)
        logger.info("🗣️ faster-whisper carregado: model=%s device=%s compute=%s", model_size, device, compute_type)
    except Exception as e:
        logger.warning("Falha ao carregar faster-whisper (%s). Usando fallback simples.", e)
        _ASR_MODEL = False
    return _ASR_MODEL

def _clean_token(tok: str) -> str:
    # mantém letras, números e apóstrofo; remove pontuação solta
    t = re.sub(r"[^\w'’\-]+", "", tok, flags=re.UNICODE)
    return t

def _asr_word_segments(audio_path: str, idioma: str) -> List[Tuple[float, float, str]]:
    """
    Retorna [(start, end, word), ...] em CAIXA ALTA usando faster-whisper.
    Se indisponível/falha, retorna lista vazia (caller faz fallback).
    """
    model = _load_asr_model()
    if not model:
        return []

    lang = "pt" if (idioma or "").lower().startswith("pt") else "en"

    try:
        # Config: word timestamps e VAD ajudam a segmentar melhor
        segments, info = model.transcribe(
            audio_path,
            language=lang,
            beam_size=5,
            vad_filter=True,
            word_timestamps=True
        )
        words: List[Tuple[float, float, str]] = []
        for seg in segments:
            if not getattr(seg, "words", None):
                continue
            for w in seg.words:
                if w.start is None or w.end is None:
                    continue
                token = _clean_token(getattr(w, "word", "") or "")
                if not token:
                    continue
                words.append((float(w.start), float(w.end), token.upper()))
        return words
    except Exception as e:
        logger.warning("ASR (faster-whisper) falhou: %s", e)
        return []

def _chunk_asr_words(words: List[Tuple[float, float, str]]) -> List[Tuple[float, float, str]]:
    """
    Junta palavras ASR em blocos de 2–3 palavras com tempos reais.
    Regras:
      - Se houver palavra longa (>=10 chars) no trio, usa 2 em vez de 3.
      - Garante duração mínima ~0.55s (merge simples com próximo).
    """
    if not words:
        return []

    chunks: List[Tuple[float, float, str]] = []
    i = 0
    n = len(words)
    while i < n:
        take = 3
        # se houver palavra muito longa no próximo trio, reduz
        has_long = any(len(words[j][2]) >= 10 for j in range(i, min(i+3, n)))
        if has_long:
            take = 2
        if i + take > n:
            take = max(1, n - i)

        group = words[i:i+take]
        start = group[0][0]
        end = group[-1][1]
        text = " ".join(w[2] for w in group)
        chunks.append((start, end, text))
        i += take

    # saneia durações muito curtas juntando com próximo
    j = 0
    MIN_DUR = 0.55
    while j < len(chunks) - 1:
        s, e, t = chunks[j]
        if (e - s) < MIN_DUR:
            s2, e2, t2 = chunks[j+1]
            # tenta expandir até o início do próximo bloco
            if s2 - e > 0.05:
                e = min(e + 0.05, s2 - 0.02)
            # se ainda curto, faz merge
            if (e - s) < MIN_DUR:
                chunks[j] = (s, e2, f"{t} {t2}")
                del chunks[j+1]
                continue
            else:
                chunks[j] = (s, e, t)
        j += 1

    return chunks

# ---------- Fallback (sem ASR): proporcional por palavras ----------
def _tokenize_words(text: str) -> List[str]:
    text = re.sub(r"[\r\n]+", " ", text).strip()
    tokens = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9'’\-]+", text)
    return [t for t in tokens if t]

def _chunk_words_simple(tokens: List[str]) -> List[str]:
    lines: List[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        take = 3
        if any(len(tokens[j]) >= 10 for j in range(i, min(i + 3, n))):
            take = 2
        if i + take > n:
            take = max(1, n - i)
        lines.append(" ".join(tokens[i:i+take]).upper())
        i += take
    return lines

def _distribuir_duracoes_por_palavra(blocos: List[str], total_disp: float) -> List[float]:
    if not blocos:
        return []
    min_seg, max_seg = 0.7, 3.0
    pesos = [max(1, len(b.split())) for b in blocos]
    soma = sum(pesos)
    brutas = [(p / soma) * total_disp for p in pesos]
    clamped = [min(max(b, min_seg), max_seg) for b in brutas]
    fator = min(1.0, (total_disp / sum(clamped))) if sum(clamped) > 0 else 1.0
    return [d * fator for d in clamped]

def _segmentos_fallback_por_palavras(texto: str, duracao_audio: float) -> List[Tuple[float, float, str]]:
    tokens = _tokenize_words(texto)
    if not tokens:
        return []
    lines = _chunk_words_simple(tokens)
    alvo = max(3.0, min(DURACAO_MAXIMA_VIDEO - 0.3, (duracao_audio or DURACAO_MAXIMA_VIDEO) - 0.2))
    gap = 0.10
    durs = _distribuir_duracoes_por_palavra(lines, alvo - gap * (len(lines) - 1))
    t = 0.25
    segs: List[Tuple[float, float, str]] = []
    for line, d in zip(lines, durs):
        ini = t
        fim = t + d
        segs.append((ini, fim, line))
        t = fim + gap
    return segs

def _segmentos_legenda_sincronizados(audio_path: str, texto: str, idioma: str, duracao_audio: float) -> List[Tuple[float, float, str]]:
    """
    Usa faster-whisper (se disponível) para sincronizar em 2–3 palavras por bloco.
    Cai no fallback proporcional se falhar.
    """
    words = _asr_word_segments(audio_path, idioma)
    if not words:
        logger.info("⚠️ ASR indisponível/falhou — usando fallback proporcional por palavras.")
        return _segmentos_fallback_por_palavras(texto, duracao_audio)

    chunks = _chunk_asr_words(words)

    # recorta/limita ao intervalo útil do vídeo
    MAX_END = max(3.0, min(DURACAO_MAXIMA_VIDEO - 0.2, (duracao_audio or DURACAO_MAXIMA_VIDEO) - 0.1))
    t0 = 0.25  # desloca levemente para evitar aparecer no frame 0
    segs: List[Tuple[float, float, str]] = []
    for (s, e, txt) in chunks:
        ini = max(t0, float(s) + 0.0)
        fim = float(e) + 0.02
        if fim <= t0 or ini >= MAX_END:
            continue
        ini = max(ini, t0)
        fim = min(fim, MAX_END)
        if fim - ini >= 0.45:  # descarta restos muito curtos
            segs.append((ini, fim, txt.upper()))

    # Se por algum motivo ficou vazio após cortes, volta ao fallback
    if not segs:
        logger.info("⚠️ Nenhum segmento útil após corte — usando fallback proporcional.")
        return _segmentos_fallback_por_palavras(texto, duracao_audio)

    return segs

# =============================================================================
# Montagem do filtergraph (drawtext)
# =============================================================================
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
    Cadeia drawtext usando textfile= (estável no Windows).
    Segmentos 1 linha; centralizados no rodapé; sem box; contorno fino.
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
            f":line_spacing=0"            # colado, sem espaço extra
            f":x=(w-text_w)/2"
            f":y=h-(text_h+{margin})"
            f":enable='between(t,{ini:.3f},{fim:.3f})'"
        )
        blocks.append(block)
    return ",".join(blocks)

# =============================================================================
# Pipeline principal
# =============================================================================
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
      - (opcional) Legendas sincronizadas (ASR) em blocos de 2–3 palavras
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

    # Segmentos de legenda
    segments: List[Tuple[float, float, str]] = []
    _clear_drawtext_cache()
    if legendas and has_voice:
        segments = _segmentos_legenda_sincronizados(voice_audio_path, long_text, lang_norm, dur_voz or DURACAO_MAXIMA_VIDEO)
        logger.info("📝 %d segmentos de legenda (sincronizados).", len(segments))

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
            # mix simples; se quiser ducking, podemos trocar por sidechaincompressor depois
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
