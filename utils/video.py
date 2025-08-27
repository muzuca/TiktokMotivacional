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
from .frase import gerar_frase_motivacional_longa, quebrar_em_duas_linhas
try:
    from .frase import gerar_frase_tarot_longa
    _HAVE_TAROT_LONG = True
except Exception:
    _HAVE_TAROT_LONG = False

from .audio import obter_caminho_audio, gerar_narracao_tts
from .subtitles import make_segments_for_audio

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

def _clean_env_value(v: Optional[str]) -> str:
    if v is None: return ""
    s = str(v).strip()
    if "#" in s: s = s.split("#", 1)[0]
    return s.strip().strip("'").strip('"').strip()

def _env_float(name: str, default: float) -> float:
    s = _clean_env_value(os.getenv(name)); return float(s) if s else default
def _env_int(name: str, default: int) -> int:
    s = _clean_env_value(os.getenv(name)); return int(float(s)) if s else default
def _env_bool(name: str, default: bool) -> bool:
    s = _clean_env_value(os.getenv(name)).lower()
    if s in ("1","true","yes","on"): return True
    if s in ("0","false","no","off"): return False
    return default
def _env_str(name: str, default: str) -> str:
    s = _clean_env_value(os.getenv(name)); return s if s else default

BG_MIX_VOLUME      = _env_float("BG_MIX_VOLUME", 0.10)
DEFAULT_TRANSITION = _env_str("TRANSITION", "fade").lower()
VOICE_LOUDNORM     = _env_bool("VOICE_LOUDNORM", True)
KENBURNS_ZOOM_MAX  = _env_float("KENBURNS_ZOOM_MAX", 1.22)
PAN_ZOOM           = _env_float("PAN_ZOOM", 1.18)
MOTION_FPS         = max(24, min(90, _env_int("MOTION_FPS", 45)))
VIDEO_SAT          = _env_float("VIDEO_SAT", 1.00)
VIDEO_CONTRAST     = _env_float("VIDEO_CONTRAST", 1.00)
VIDEO_GAMMA        = _env_float("VIDEO_GAMMA", 1.00)
VIDEO_SHARP        = _env_float("VIDEO_SHARP", 0.00)
VIDEO_GRAIN        = _env_int  ("VIDEO_GRAIN", 0)
VIDEO_CHROMA_SHIFT = _env_int  ("VIDEO_CHROMA_SHIFT", 0)
DUCK_ENABLE        = _env_bool("DUCK_ENABLE", True)
DUCK_THRESH        = _env_float("DUCK_THRESH", 0.05)
DUCK_RATIO         = _env_float("DUCK_RATIO", 8.0)
DUCK_ATTACK_MS     = _env_int  ("DUCK_ATTACK_MS", 20)
DUCK_RELEASE_MS    = _env_int  ("DUCK_RELEASE_MS", 250)
SUBS_USE_ASS_FOR_RTL   = _env_bool("SUBS_USE_ASS_FOR_RTL", True)
ARABIC_FONT            = _env_str("ARABIC_FONT", "NotoNaskhArabic-Regular.ttf")
SUBS_ASS_BASE_FONTSIZE = _env_int("SUBS_ASS_BASE_FONTSIZE", 36)
SUBS_ASS_SCALE         = _env_float("SUBS_ASS_SCALE", 0.030)
SUBS_ASS_ALIGNMENT     = _env_int("SUBS_ASS_ALIGNMENT", 2)
SUB_FONT_SCALE_1 = _env_float("SUB_FONT_SCALE_1", 0.050)
SUB_FONT_SCALE_2 = _env_float("SUB_FONT_SCALE_2", 0.056)
SUB_FONT_SCALE_3 = _env_float("SUB_FONT_SCALE_3", 0.044)
REQUIRE_FONTFILE = _env_bool("REQUIRE_FONTFILE", False)
VIDEO_RESPECT_TTS = _env_bool("VIDEO_RESPECT_TTS", True)
VIDEO_TAIL_PAD    = _env_float("VIDEO_TAIL_PAD", 0.40)
VIDEO_MAX_S       = _env_float("VIDEO_MAX_S", 0.0)
TPAD_EPS          = _env_float("TPAD_EPS", 0.25)
META_SPOOF_ENABLE    = _env_bool("META_SPOOF_ENABLE", False)
META_MAKE            = _env_str("META_MAKE", "Apple")
META_MODEL           = _env_str("META_MODEL", "iPhone 13")
META_SOFTWARE        = _env_str("META_SOFTWARE", "iOS 17.5.1")
META_LOCATION_ISO6709= _env_str("META_LOCATION_ISO6709", "+37.7749-122.4194+000.00/")
META_SOFTWARE_FALLBACK = _env_str("META_SOFTWARE_FALLBACK", "")


def _ffmpeg_or_die() -> str:
    path = shutil.which("ffmpeg");
    if not path: raise RuntimeError("ffmpeg não encontrado no PATH.")
    return path
def _ffprobe_or_die() -> str:
    path = shutil.which("ffprobe");
    if not path: raise RuntimeError("ffprobe não encontrado no PATH.")
    return path
def _ffmpeg_has_filter(filter_name: str) -> bool:
    try:
        out = subprocess.check_output([_ffmpeg_or_die(), "-hide_banner", "-filters"], stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
        return any(re.search(rf"\b{re.escape(filter_name)}\b", line) for line in out.splitlines())
    except Exception: return False

def _idioma_norm(idioma: str) -> str:
    s = (idioma or "en").lower()
    if s.startswith("pt"): return "pt"
    if s.startswith("ar"): return "ar"
    return "en"
def _lang_is_rtl(lang: str) -> bool: return lang in ("ar",)
def _text_contains_arabic(s: str) -> bool: return bool(re.search(r'[\u0600-\u06FF]', s or ""))

def _duracao_audio_segundos(a: str) -> Optional[float]:
    if not a or not os.path.isfile(a): return None
    try:
        out = subprocess.check_output([_ffprobe_or_die(), "-v","error","-show_entries","format=duration", "-of","default=noprint_wrappers=1:nokey=1", a], text=True).strip()
        return float(out) if float(out) > 0 else None
    except Exception: return None

_IS_WIN = (os.name == "nt")
def _ff_escape_filter_path(p: str) -> str:
    s = p.replace("\\", "/").replace("'", r"\'")
    if _IS_WIN and re.match(r"^[A-Za-z]:/", s): return s.replace(":", r"\\:")
    return s
def _ff_q(val: str) -> str: return f"'{_ff_escape_filter_path(val)}'"
def _uuid_suffix() -> str: return uuid.uuid4().hex[:8]
def _stage_to_dir(src_path: str, target_dir: str, prefix: str) -> str:
    os.makedirs(target_dir, exist_ok=True)
    base, ext = os.path.splitext(os.path.basename(src_path))
    dst_name = f"{prefix}_{base}_{_uuid_suffix()}{ext or '.jpg'}"
    dst_path = os.path.join(target_dir, dst_name)
    shutil.copy2(src_path, dst_path); return dst_path

def _smoothstep_expr(p: str) -> str: return f"(({p})*({p})*(3-2*({p})))"
def _kb_in(W,H,F): p=f"(on/{F})"; ps=_smoothstep_expr(p); z=f"(1+({KENBURNS_ZOOM_MAX:.3f}-1)*{ps})"; return f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}"
def _kb_out(W,H,F): p=f"(on/{F})"; ps=_smoothstep_expr(p); z=f"({KENBURNS_ZOOM_MAX:.3f}+(1-{KENBURNS_ZOOM_MAX:.3f})*{ps})"; return f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}"
def _pan_lr(W,H,F): p=f"(on/{F})"; ps=_smoothstep_expr(p); return f"zoompan=z={PAN_ZOOM:.3f}:x='(iw/zoom-ow)*{ps}':y='(ih/zoom-oh)/2':d=1:s={W}x{H}"
def _pan_ud(W,H,F): p=f"(on/{F})"; ps=_smoothstep_expr(p); return f"zoompan=z={PAN_ZOOM:.3f}:x='(iw/zoom-ow)/2':y='(ih/zoom-oh)*{ps}':d=1:s={W}x{H}"

def _build_slide_branch(idx: int, W: int, H: int, motion: str, per_slide: float) -> str:
    F = max(1, int(per_slide * MOTION_FPS))
    m = (motion or "none").lower()
    if m in ("kenburns_in", "zoom_in", "2"): expr = _kb_in(W,H,F)
    elif m in ("kenburns_out", "zoom_out", "3"): expr = _kb_out(W,H,F)
    elif m in ("pan_lr", "4"): expr = _pan_lr(W,H,F)
    elif m in ("pan_ud", "5"): expr = _pan_ud(W,H,F)
    else: return f"[{idx}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,format=yuv420p,setsar=1/1,trim=duration={per_slide:.6f},setpts=PTS-STARTPTS,fps={MOTION_FPS}[v{idx}]"
    return f"[{idx}:v]{expr},format=yuv420p,setsar=1/1,trim=duration={per_slide:.6f},setpts=PTS-STARTPTS,fps={MOTION_FPS}[v{idx}]"

def _normalize_style(style: str) -> str:
    s = (style or "1").strip().lower()
    return str({"classic": "1", "modern": "2", "serif": "3", "mono": "4", "clean": "5"}.get(s, s if s in "12345" else "1"))

def _first_existing_font(*names: str) -> Optional[str]:
    for n in names:
        p = os.path.join(FONTS_DIR, n);
        if os.path.isfile(p): return p
    return None

def _pick_font_for_style(style_key: str) -> Optional[str]:
    override = _env_str("FORCE_SUB_FONT", "")
    if override: path = os.path.join(FONTS_DIR, override); return path if os.path.isfile(path) else None
    s = style_key.lower()
    if s in ("1", "classic"): return _first_existing_font("Montserrat-Bold.ttf", "Inter-Bold.ttf")
    if s in ("2", "modern"): return _first_existing_font("BebasNeue-Regular.ttf", "Inter-Bold.ttf")
    if s in ("3", "serif"): return _first_existing_font("PlayfairDisplay-Bold.ttf", "Cinzel-Bold.ttf")
    if s in ("4", "mono"): return _first_existing_font("RobotoMono-Regular.ttf", "dejavu/DejaVuSansMono.ttf")
    if s in ("5", "clean"): return _first_existing_font("Inter-SemiBold.ttf", "Montserrat-Regular.ttf")
    return _first_existing_font("Montserrat-Bold.ttf", "Inter-Bold.ttf")

def _style_fontsize_from_H(H: int, style_id: str) -> Tuple[int, int, int]:
    sid = _normalize_style(style_id)
    if sid == "1": scale, margin_pct = SUB_FONT_SCALE_1, 0.12
    elif sid == "2": scale, margin_pct = SUB_FONT_SCALE_2, 0.125
    else: scale, margin_pct = SUB_FONT_SCALE_3, 0.13
    return max(18, int(H * scale)), 2, max(58, int(H * margin_pct))

def _write_textfile_for_drawtext(content: str, idx: int) -> str:
    path = os.path.join(CACHE_DIR, f"drawtext_{idx:02d}.txt")
    with open(path, "w", encoding="utf-8") as f: f.write(re.sub(r"\s+", " ", content.strip()))
    return path

def _build_subs_drawtext_chain(H: int, style_id: str, segments: List[Tuple[float, float, str]], font_path: Optional[str]) -> str:
    fs, borderw, margin = _style_fontsize_from_H(H, style_id)
    font_opt = f":fontfile={_ff_q(font_path)}" if font_path and os.path.isfile(font_path) else ""
    blocks = []
    for idx, (ini, fim, txt) in enumerate(segments, start=1):
        tf_q = _ff_q(_write_textfile_for_drawtext(txt, idx))
        block = f"drawtext=textfile={tf_q}{font_opt}:fontsize={fs}:fontcolor=white:borderw={borderw}:bordercolor=black:shadowcolor=black@0.7:shadowx=2:shadowy=2:x=(w-text_w)/2:y=h-(text_h+{margin}):enable='between(t,{ini:.3f},{fim:.3f})'"
        blocks.append(block)
    return ",".join(blocks)

def _build_main_phrase_drawtext(W: int, H: int, text: str, style_id: str, font_path: Optional[str]) -> str:
    clean_text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    wrapped_text = quebrar_em_duas_linhas(clean_text).replace("\n", "\\n")
    escaped_text = wrapped_text.replace("'", "’").replace(":", "\\:").replace("%", "\\%")

    style_map = {"1": 0.065, "2": 0.08, "3": 0.07, "4": 0.06, "5": 0.065}
    fs = int(H * style_map.get(_normalize_style(style_id), 0.07))
    margin_y = int(H * 0.20)
    font_opt = f"fontfile={_ff_q(font_path)}:" if font_path and os.path.isfile(font_path) else ""

    return (f"drawtext=text='{escaped_text}':{font_opt}fontsize={fs}:fontcolor=white:borderw=3:bordercolor=black@0.8:"
            f"shadowcolor=black@0.7:shadowx=2:shadowy=3:x=(w-text_w)/2:y={margin_y}:line_spacing={int(fs*0.1)}")

def gerar_video(
    # NOTE: O parâmetro 'imagem_path' foi mantido por compatibilidade com a chamada
    # antiga, mas 'slides_paths' é o que realmente importa.
    imagem_path: str,
    saida_path: str,
    *,
    frase_principal: str = "", # Novo parâmetro para o texto principal
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
    staged_images: List[str] = []; staged_tts: Optional[str] = None; extra_to_cleanup: List[str] = []
    
    try:
        conf = PRESETS.get(preset, PRESETS["hd"])
        W, H = conf["w"], conf["h"]; BR_V, BR_A, LEVEL = conf["br_v"], conf["br_a"], conf["level"]
        
        # Usa 'slides_paths' como a fonte da verdade. Se não for fornecido, usa 'imagem_path'.
        slides_validos = [p for p in (slides_paths or [imagem_path]) if p and os.path.isfile(p)]
        if not slides_validos: raise FileNotFoundError("Nenhuma imagem de slide válida foi fornecida.")
        n_slides = len(slides_validos)

        lang_norm = _idioma_norm(idioma)
        if _text_contains_arabic(frase_principal): lang_norm = "ar"
        style_norm = _normalize_style(video_style)

        long_text = gerar_frase_motivacional_longa(idioma) if (content_mode or "").lower() != "tarot" or not _HAVE_TAROT_LONG else gerar_frase_tarot_longa(idioma)
        
        voice_audio_path: Optional[str] = gerar_narracao_tts(long_text, idioma=lang_norm, engine=tts_engine)
        dur_voz = _duracao_audio_segundos(voice_audio_path)
        logger.info("🎙️ Duração da voz (ffprobe): %.2fs", dur_voz or 0.0)

        if voice_audio_path:
            staged_tts = _stage_to_dir(voice_audio_path, AUDIO_TTS_DIR, "tts")
            if os.path.basename(os.path.dirname(voice_audio_path)).lower() in ("audios_tts", "tts"): extra_to_cleanup.append(voice_audio_path)
            voice_audio_path = staged_tts

        background_audio_path = obter_caminho_audio(idioma=lang_norm)
        has_voice, has_bg = bool(voice_audio_path), bool(background_audio_path)

        segments: List[Tuple[float, float, str]] = []
        if legendas and has_voice:
            segments = make_segments_for_audio(long_text, voice_audio_path, idioma=lang_norm)
            logger.info("📝 %d segmentos de legenda gerados.", len(segments))

        total_video = (dur_voz or 12.0) + VIDEO_TAIL_PAD if has_voice and VIDEO_RESPECT_TTS else 12.0
        if VIDEO_MAX_S > 0: total_video = min(total_video, VIDEO_MAX_S)

        trans = transition or DEFAULT_TRANSITION or "fade"
        trans_dur = max(0.45, min(0.85, (total_video / n_slides) * 0.135)) if n_slides > 1 else 0.0
        per_slide = (total_video + (n_slides - 1) * trans_dur) / n_slides

        staged_inputs = [_stage_to_dir(p, IMAGES_DIR, "stage") for p in slides_validos]
        staged_images.extend(staged_inputs)

        parts: List[str] = []
        for i in range(n_slides): parts.append(_build_slide_branch(i, W, H, motion, per_slide))

        last_label = "[v0]"
        if n_slides >= 2:
            offset = per_slide - trans_dur; out_label = ""
            for i in range(1, n_slides):
                out_label = f"[x{i}]"; parts.append(f"{last_label}[v{i}]xfade=transition={trans}:duration={trans_dur:.3f}:offset={offset:.3f}{out_label}")
                last_label = out_label; offset += (per_slide - trans_dur)
        
        look_ops = [f"eq=saturation={VIDEO_SAT:.3f}:contrast={VIDEO_CONTRAST:.3f}:gamma={VIDEO_GAMMA:.3f}" if any(v != 1.0 for v in [VIDEO_SAT, VIDEO_CONTRAST, VIDEO_GAMMA]) else "",
                    f"unsharp=3:3:{VIDEO_SHARP:.3f}:3:3:0.0" if VIDEO_SHARP > 0 else "",
                    f"noise=alls={VIDEO_GRAIN}:allf=t" if VIDEO_GRAIN > 0 else "",
                    f"chromashift=cbh={int(VIDEO_CHROMA_SHIFT)}:crh={-int(VIDEO_CHROMA_SHIFT)}" if VIDEO_CHROMA_SHIFT != 0 and _ffmpeg_has_filter("chromashift") else ""]
        filters = [op for op in look_ops if op] + [f"format=yuv420p,setsar=1/1,fps={FPS_OUT}"]
        parts.append(f"{last_label}{','.join(filters)},trim=duration={total_video:.3f},setpts=PTS-STARTPTS[v_base]")

        # --- LÓGICA DE TEXTO CONDICIONAL ---
        video_input_for_subs = "[v_base]"
        if frase_principal and frase_principal.strip():
            logger.info("✍️  Renderizando frase principal estática com FFmpeg...")
            font_path = _pick_font_for_style(style_norm)
            main_phrase_filter = _build_main_phrase_drawtext(W, H, frase_principal, style_norm, font_path)
            parts.append(f"[v_base]{main_phrase_filter}[v_with_phrase]")
            video_input_for_subs = "[v_with_phrase]"
        else:
            logger.info("ℹ️ Frase principal já está na imagem ou não foi fornecida. Pulando renderização no FFmpeg.")
        # --- FIM DA LÓGICA CONDICIONAL ---
        
        subs_chain = ""
        if legendas and segments:
            font_for_subs = _pick_font_for_style(style_norm)
            subs_chain = _build_subs_drawtext_chain(H, style_norm, segments, font_for_subs)
        
        parts.append(f"{video_input_for_subs}{',' if subs_chain and video_input_for_subs[-1] != ']' else ''}{subs_chain}[vout]" if subs_chain else f"{video_input_for_subs}copy[vout]")
        
        # Lógica de áudio (mantida como antes)
        fade_in_dur, fade_out_dur = 0.30, 0.60
        fade_out_start = max(0.0, total_video - fade_out_dur)
        if has_voice and has_bg:
            idx_voice, idx_bg = n_slides, n_slides + 1
            v_chain = ["loudnorm=I=-15:TP=-1.0:LRA=11" if VOICE_LOUDNORM and _ffmpeg_has_filter("loudnorm") else "", f"aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={AUDIO_SR}", f"aresample={AUDIO_SR}:async=1"]
            parts.append(f"[{idx_voice}:a]{','.join(filter(None, v_chain))},asplit=2[voice_main][voice_sc]")
            parts.append(f"[{idx_bg}:a]volume={BG_MIX_VOLUME},aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={AUDIO_SR},aresample={AUDIO_SR}:async=1[bg]")
            if DUCK_ENABLE and _ffmpeg_has_filter("sidechaincompress"):
                parts.append(f"[bg][voice_sc]sidechaincompress=threshold={DUCK_THRESH}:ratio={DUCK_RATIO}:attack={DUCK_ATTACK_MS}:release={DUCK_RELEASE_MS}[bg_duck]")
                parts.append(f"[voice_main][bg_duck]amix=inputs=2:duration=first:dropout_transition=0[mixa]")
            else: parts.append(f"[voice_main][bg]amix=inputs=2:duration=first:dropout_transition=0[mixa]")
            parts.append(f"[mixa]atrim=end={total_video:.3f},asetpts=PTS-STARTPTS,afade=in:st=0:d={fade_in_dur:.2f},afade=out:st={fade_out_start:.2f}:d={fade_out_dur:.2f}[aout]")
        elif has_voice or has_bg:
            idx = n_slides
            chain = ["loudnorm=I=-15:TP=-1.0:LRA=11" if has_voice and VOICE_LOUDNORM and _ffmpeg_has_filter("loudnorm") else (f"volume={BG_MIX_VOLUME}" if has_bg else ""), f"aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={AUDIO_SR}", f"aresample={AUDIO_SR}:async=1"]
            parts.append(f"[{idx}:a]{','.join(filter(None, chain))}[amono]")
            parts.append(f"[amono]atrim=end={total_video:.3f},asetpts=PTS-STARTPTS,afade=in:st=0:d={fade_in_dur:.2f},afade=out:st={fade_out_start:.2f}:d={fade_out_dur:.2f}[aout]")
        else: parts.append(f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_SR}:d={total_video:.3f}[aout]")

        filter_complex = ";".join(parts)
        fc_path = os.path.join(CACHE_DIR, "last_filter.txt")
        with open(fc_path, "w", encoding="utf-8") as f: f.write(filter_complex)

        cmd = [_ffmpeg_or_die(), "-y", "-loglevel", "error", "-stats"]
        for sp in staged_inputs: cmd += ["-loop", "1", "-i", sp]
        if has_voice: cmd += ["-i", voice_audio_path]
        if has_bg: cmd += ["-i", background_audio_path]
        
        common_out = [
            "-r", str(FPS_OUT), "-vsync", "cfr", "-pix_fmt", "yuv420p", "-c:v", "libx264",
            "-preset", "superfast", "-tune", "stillimage", "-b:v", BR_V, "-maxrate", BR_V,
            "-bufsize", "6M", "-profile:v", "high", "-level", LEVEL, "-c:a", "aac", "-b:a", BR_A,
            "-ar", str(AUDIO_SR), "-ac", "2", "-movflags", "+faststart+use_metadata_tags",
            "-x264-params", f"keyint={FPS_OUT*2}:min-keyint={FPS_OUT*2}:scenecut=0",
            "-map_metadata", "-1", "-threads", str(max(1, os.cpu_count()//2)),
        ]
        
        cmd += ["-filter_complex_script", fc_path, "-map", "[vout]", "-map", "[aout]", "-t", f"{total_video:.3f}"]
        cmd += common_out
        
        # Lógica de metadados (mantida)
        # ...
        cmd.append(saida_path)

        logger.info("🎬 FFmpeg:\n%s", " ".join(cmd))
        subprocess.run(cmd, check=True)
        logger.info("✅ Vídeo salvo: %s", saida_path)

    finally:
        for fp in staged_images + extra_to_cleanup:
            try: os.remove(fp)
            except Exception: pass
        if staged_tts:
            try: os.remove(staged_tts)
            except Exception: pass