# utils/subtitles.py
# Alinha legendas por palavra usando faster-whisper e cria blocos
# curtos de 2–3 palavras (1 linha), evitando juntar "fim de frase + começo da outra".

import os
import re
import math
import subprocess
import unicodedata
from dataclasses import dataclass
from typing import List, Tuple, Optional

# ===== ENV helpers =====
def _clean_env_value(v: Optional[str]) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if "#" in s:
        s = s.split("#", 1)[0]
    return s.strip().strip("'").strip('"').strip()

def _env_float(name: str, default: float) -> float:
    s = _clean_env_value(os.getenv(name))
    if s == "": return default
    try: return float(s)
    except Exception: return default

def _env_int(name: str, default: int) -> int:
    s = _clean_env_value(os.getenv(name))
    if s == "": return default
    try: return int(float(s))
    except Exception: return default

def _env_bool(name: str, default: bool) -> bool:
    s = _clean_env_value(os.getenv(name)).lower()
    if s == "": return default
    if s in ("1","true","yes","on"):  return True
    if s in ("0","false","no","off"): return False
    return default

def _env_str(name: str, default: str) -> str:
    s = _clean_env_value(os.getenv(name))
    return s if s != "" else default

# ===== Parâmetros (otimizados p/ 2–3 palavras) =====
SUB_MIN_DUR_SEC       = _env_float("SUB_MIN_DUR_SEC", 0.70)
SUB_MAX_DUR_SEC       = _env_float("SUB_MAX_DUR_SEC", 2.80)
SUB_GAP_SEC           = _env_float("SUB_GAP_SEC", 0.12)

# Vírgula/macetes: quando o gap entre palavras passa disso, ajuda a quebrar
WORD_HARD_GAP_SEC     = _env_float("WORD_HARD_GAP_SEC", 0.55)
WORD_SOFT_GAP_SEC     = _env_float("WORD_SOFT_GAP_SEC", 0.33)

# Tamanho “textual” (apenas para heurística de corte de palavras longas)
SUB_MAX_CHARS         = _env_int("SUB_MAX_CHARS", 36)

# Quantidade de palavras por bloco (principal exigência)
SUB_WORDS_PER_CHUNK_MIN = _env_int("SUB_WORDS_PER_CHUNK_MIN", 2)
SUB_WORDS_PER_CHUNK_MAX = _env_int("SUB_WORDS_PER_CHUNK_MAX", 3)

FFPROBE_BIN           = _env_str("FFPROBE_BIN", "ffprobe")

@dataclass
class Caption:
    idx: int
    start: float
    end: float
    text: str

def _fmt_ts(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

def build_srt(caps: List[Caption]) -> str:
    out = []
    for c in caps:
        out.append(str(c.idx))
        out.append(f"{_fmt_ts(c.start)} --> {_fmt_ts(c.end)}")
        out.append(c.text.strip())
        out.append("")
    return "\n".join(out).strip() + "\n"

def _ffprobe_duration(path: str) -> float:
    out = subprocess.check_output(
        [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        stderr=subprocess.DEVNULL, text=True
    ).strip()
    dur = float(out)
    if not math.isfinite(dur) or dur <= 0:
        raise RuntimeError("Duração inválida")
    return dur

def _norm_lang(idioma: str) -> str:
    i = (idioma or "en").lower()
    if i.startswith("pt"): return "pt"
    if i.startswith("ar"): return "ar"
    return "en"

def _strip_invisibles(s: str) -> str:
    """
    Remove caracteres de controle/invisíveis que viram '□' na renderização:
    - Categoria Unicode Cf/Cc/Cs (bidi marks, ZWJ/ZWNJ, etc.)
    - Inclui U+200B (ZWSP) e U+FEFF (BOM)
    """
    cleaned = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat in ("Cf", "Cc", "Cs"):
            if ch in (" ", "\n"):
                cleaned.append(ch)
        else:
            cleaned.append(ch)
    return "".join(cleaned)

def _join_tokens(tokens: List[str]) -> str:
    s = " ".join(tokens)
    s = re.sub(r"\s+([.,!?;:…])", r"\1", s)                    # ocidental
    s = re.sub(r"\s+([\u061F\u061B\u060C])", r"\1", s)         # árabe
    s = _strip_invisibles(s)
    return re.sub(r"\s+", " ", s).strip()

def _align_words(audio_path: str, idioma: str) -> List[Tuple[float, float, str]]:
    """Retorna lista de (start, end, token_text)."""
    try:
        from faster_whisper import WhisperModel
    except Exception:
        return []

    lang         = _norm_lang(idioma)
    model_name   = _env_str("WHISPER_MODEL", "base")
    device       = _env_str("WHISPER_DEVICE", "cpu")
    compute_type = _env_str("WHISPER_COMPUTE_TYPE", "int8")
    beam         = _env_int("WHISPER_BEAM_SIZE", 1)
    vad          = _env_bool("WHISPER_VAD", True)
    cache        = _env_str("WHISPER_MODEL_CACHE", "./whisper_models")

    model = WhisperModel(model_name, device=device, compute_type=compute_type, download_root=cache)
    segments, _ = model.transcribe(
        audio_path,
        language=lang,
        beam_size=beam,
        vad_filter=vad,
        word_timestamps=True,
    )

    words = []
    for seg in segments:
        if getattr(seg, "words", None):
            for w in seg.words:
                if (w.start is not None) and (w.end is not None) and (w.word is not None):
                    tok = re.sub(r"\s+", " ", str(w.word)).strip()
                    if tok:
                        words.append((float(w.start), float(w.end), tok))
    return words

def _is_hard_punct(ch: str) -> bool:
    return ch in ".!?…\u061F"

def _is_soft_punct(ch: str) -> bool:
    return ch in ",;:—-\u061B\u060C"

def _make_caps_from_words(words: List[Tuple[float, float, str]]) -> List[Caption]:
    """Agrupa em blocos de 2–3 palavras, respeitando pontuação e gaps."""
    if not words:
        return []

    caps: List[Caption] = []
    cur_tok: List[str] = []
    cur_start: Optional[float] = None
    last_end: Optional[float] = None
    idx = 1

    def cur_dur(now_end: Optional[float]) -> float:
        if cur_start is None or now_end is None: return 0.0
        return max(0.0, now_end - cur_start)

    def flush(force_min=False):
        nonlocal cur_tok, cur_start, last_end, idx
        if not cur_tok:
            return
        text = _join_tokens(cur_tok)
        s = cur_start if cur_start is not None else (last_end or 0.0)
        e = last_end if last_end is not None else (s + SUB_MIN_DUR_SEC)
        if force_min and (e - s) < SUB_MIN_DUR_SEC:
            e = s + SUB_MIN_DUR_SEC
        e = min(s + SUB_MAX_DUR_SEC, e)
        caps.append(Caption(idx=idx, start=s, end=e, text=text))
        idx += 1
        cur_tok = []
        cur_start = None
        last_end = None

    for i, (ws, we, tok) in enumerate(words):
        if last_end is not None and (ws - last_end) >= WORD_HARD_GAP_SEC:
            flush(force_min=True)

        if cur_start is None:
            cur_start = ws
        last_end = we
        cur_tok.append(tok)

        txt = _join_tokens(cur_tok)
        wc = len(txt.split())
        dur_now = cur_dur(we)
        last_char = tok[-1]

        if wc >= SUB_WORDS_PER_CHUNK_MAX:
            flush(force_min=True)
            continue

        if _is_hard_punct(last_char) and wc >= SUB_WORDS_PER_CHUNK_MIN:
            flush(force_min=True)
            continue

        if dur_now >= SUB_MAX_DUR_SEC:
            flush(force_min=False)
            continue

        if wc >= SUB_WORDS_PER_CHUNK_MIN:
            nxt_start = words[i+1][0] if i+1 < len(words) else None
            if nxt_start is None or (nxt_start - we) >= WORD_SOFT_GAP_SEC or _is_soft_punct(last_char):
                flush(force_min=True)
                continue

    flush(force_min=True)

    # Pós-processamento: garante gaps e sem overlap
    fixed: List[Caption] = []
    prev_end = 0.0
    new_idx = 1
    for c in caps:
        s = max(prev_end + (SUB_GAP_SEC if fixed else 0.0), c.start)
        e = max(s + SUB_MIN_DUR_SEC, min(c.end, s + SUB_MAX_DUR_SEC))
        if fixed and s < fixed[-1].end + 1e-3:
            s = fixed[-1].end + SUB_GAP_SEC
            e = max(s + SUB_MIN_DUR_SEC, e)
        fixed.append(Caption(idx=new_idx, start=s, end=e, text=c.text))
        new_idx += 1
        prev_end = e

    return fixed

def make_segments_for_audio(text: str, audio_path: str, *, idioma: str = "pt-br") -> List[Tuple[float, float, str]]:
    """Retorna [(start, end, text)] com 2–3 palavras por bloco."""
    if not audio_path or not os.path.isfile(audio_path):
        return []

    words = _align_words(audio_path, idioma)
    if words:
        caps = _make_caps_from_words(words)
        return [(c.start, c.end, c.text) for c in caps]

    # Fallback simples sem alinhamento
    try:
        _ = _ffprobe_duration(audio_path)
    except Exception:
        pass
    toks = [t for t in re.split(r"\s+", (text or "").strip()) if t]
    if not toks:
        return []
    out: List[Tuple[float, float, str]] = []
    i = 0
    t = 0.0
    dur = max(SUB_MIN_DUR_SEC, min(SUB_MAX_DUR_SEC, 1.4))
    while i < len(toks):
        n = min(SUB_WORDS_PER_CHUNK_MAX, max(SUB_WORDS_PER_CHUNK_MIN, len(toks) - i))
        chunk = " ".join(toks[i:i+n])
        s = t
        e = s + dur
        out.append((s, e, chunk))
        t = e + SUB_GAP_SEC
        i += n
    return out

def make_srt_for_audio(text: str, audio_path: str, *, idioma: str = "pt-br") -> str:
    segs = make_segments_for_audio(text, audio_path, idioma=idioma)
    caps = [Caption(idx=i+1, start=s, end=e, text=t) for i, (s, e, t) in enumerate(segs)]
    return build_srt(caps)
