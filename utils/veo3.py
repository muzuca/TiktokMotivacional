# utils/veo3.py
# Veo3 (Flow via Selenium OU Gemini API) + FFmpeg
# - Gera 1..N cenas; no Flow usa generate_many_via_flow (mesma sessão, baixa e exclui cada vídeo)
# - Normaliza cada cena para 9:16 (1080x1920) via scale+pad (SEM zoom)
# - Junta cenas (concat) + BGM opcional (com Audio Ducking); aplica PÓS-ZOOM opcional no final
# - Menus com "b/voltar": testar, gerar+postar, reutilizar, **exportar (sem postar)** e auto-config do modo automático
# - Resume inteligente: só gera cenas faltantes/sem áudio
# - Limpeza pós-postagem remove os arquivos de cena (cN.mp4) p/ não reutilizar no automático
from __future__ import annotations

import os, re, json, time, shutil, random, logging, subprocess, glob
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
from pathlib import Path
import yaml

logger = logging.getLogger(__name__)

# ========= Gemini =========
try:
    from google import genai
    from google.genai import types
    _HAVE_GENAI = True
except Exception:
    _HAVE_GENAI = False

# ========= Aux (bgm) =========
try:
    from utils.audio import escolher_trilha_local_ou_freesound as _pick_bgm_util
except Exception:
    _pick_bgm_util = None

from utils.frase import gerar_frase_motivacional, gerar_hashtags_virais
from utils.tiktok import postar_no_tiktok_e_renomear

# ========= Pastas / Constantes =========
VIDEOS_DIR   = "videos"
IMAGENS_DIR  = "imagens"
PROMPTS_DIR  = os.path.join(VIDEOS_DIR, "prompts")
os.makedirs(VIDEOS_DIR, exist_ok=True)
os.makedirs(IMAGENS_DIR, exist_ok=True)
os.makedirs(PROMPTS_DIR, exist_ok=True)

OUT_W, OUT_H = 1080, 1920
FPS_OUT      = 30
AUDIO_SR     = 44100
BR_V = "5000k"
BR_A = "192k"
H264_LEVEL = "4.0"

POST_ZOOM = float(os.getenv("VEO3_POST_ZOOM", "1.0"))
BG_MIX_DB = float(os.getenv("BG_MIX_DB", "-20.0"))

VEO3_MODEL  = os.getenv("VEO3_MODEL", "veo-3.0-generate-preview").strip()
TEXT_MODEL  = os.getenv("VEO3_TEXT_MODEL", "gemini-2.0-flash-001").strip()

# Limites do diálogo
D_MIN = int(os.getenv("VEO3_DIALOGUE_MIN_WORDS", "18"))
D_MAX = int(os.getenv("VEO3_DIALOGUE_MAX_WORDS", "22"))
HASHTAGS_TOP_N = 3

NEGATIVE_PROMPT = (
    "cartoon, drawing, low quality, branding, readable text, fantasy effects, "
    "facial change, outfit inconsistency, visual distortion, watermark, logo"
)

USE_BACKEND = os.getenv("VEO3_BACKEND", "flow").strip().lower()
FLOW_PROJECT_URL  = os.getenv("FLOW_PROJECT_URL", "").strip()
FLOW_COOKIES_FILE = os.getenv("FLOW_COOKIES_FILE", "cookies_veo3.txt").strip()
VEO3_HEADLESS     = os.getenv("VEO3_CHROME_HEADLESS", "1").strip() != "0"  # default do .env

# ==== NOVO: número de tentativas para chamadas ao Gemini ====
GEMINI_MAX_RETRIES = max(1, int(os.getenv("GEMINI_MAX_RETRIES", "3")))

# Retry automático em caso de falha (modo automático)
def _auto_retry_minutes() -> int:
    try:
        return int(float(os.getenv("AUTO_RETRY_MINUTES", "10")))
    except Exception:
        return 10

# Tokens de "voltar"
_BACK_TOKENS = {"b", "voltar", "back"}

_HAVE_FLOW = False
_HAVE_FLOW_MANY = False
try:
    if USE_BACKEND == "flow":
        from utils.veo3_flow import generate_single_via_flow, generate_many_via_flow
        _HAVE_FLOW = True
        _HAVE_FLOW_MANY = True
except Exception as e:
    logger.warning("Flow backend indisponível ou incompleto: %s", e)

def _ffmpeg_or_die() -> str:
    p = shutil.which("ffmpeg")
    if not p:
        raise RuntimeError("ffmpeg não encontrado no PATH.")
    return p

def _ffprobe_or_die() -> str:
    p = shutil.which("ffprobe")
    if not p:
        raise RuntimeError("ffprobe não encontrado no PATH.")
    return p

# ---------- ffprobe helpers ----------
def _probe_json(path: str) -> dict:
    try:
        out = subprocess.check_output([
            _ffprobe_or_die(),
            "-v", "error", "-show_streams", "-show_format", "-of", "json", path
        ], stderr=subprocess.STDOUT)
        return json.loads(out.decode("utf-8", "replace"))
    except Exception as e:
        logger.debug("ffprobe falhou em %s: %s", path, e)
        return {}

def _has_audio_stream(path: str) -> bool:
    info = _probe_json(path)
    for s in info.get("streams", []):
        if s.get("codec_type") == "audio":
            return True
    return False

def _video_duration(path: str) -> float:
    info = _probe_json(path)
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            try:
                return float(s.get("duration")) if s.get("duration") else float(info.get("format", {}).get("duration", 0.0))
            except Exception:
                pass
    try:
        return float(info.get("format", {}).get("duration", 0.0))
    except Exception:
        return 0.0

def _extract_frame_ffmpeg(video_path: str, out_jpg: str, t: float = 1.0) -> str:
    try:
        subprocess.run([
            _ffmpeg_or_die(), "-y", "-ss", f"{max(0.0, t):.3f}", "-i", video_path,
            "-frames:v", "1", "-q:v", "2", out_jpg
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return out_jpg if os.path.isfile(out_jpg) else ""
    except Exception:
        return ""

def _pick_bgm_path() -> str:
    if callable(_pick_bgm_util):
        try:
            p = _pick_bgm_util()
            if p and os.path.isfile(p):
                return p
        except Exception as e:
            logger.debug("Falha ao escolher trilha via utils.audio: %s", e)
    base = "audios"
    if os.path.isdir(base):
        exts = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")
        cands = [os.path.join(base, f) for f in os.listdir(base) if f.lower().endswith(exts)]
        if cands:
            return random.choice(cands)
    return ""

# ------------------------ Templates / Persona ------------------------
def _load_persona_config(persona_name: str) -> dict:
    config_path = Path(__file__).parent / "personas" / f"{(persona_name or 'luisa').lower()}.json"
    if not config_path.exists():
        logger.warning("Persona '%s' não encontrada. Usando 'luisa' como padrão.", persona_name)
        config_path = Path(__file__).parent / "personas" / "luisa.json"
        if not config_path.exists():
            raise FileNotFoundError("Arquivo de persona padrão 'luisa.json' não encontrado.")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)

def _load_prompt_template(template_name: str) -> str:
    config_path = Path(__file__).parent / "prompts" / f"{template_name}.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Arquivo de template de prompt '{template_name}.yaml' não encontrado.")
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
        return data["template"]

def _get_client() -> "genai.Client":
    if not _HAVE_GENAI:
        raise RuntimeError("Instale: pip install -U google-genai")
    return genai.Client()

# ------------------------ Helpers p/ Gemini com retry ------------------------
def _gemini_generate_json(contents: str, stage_label: str, required_keys: Optional[List[str]] = None) -> Optional[dict]:
    """
    Chama o Gemini pedindo JSON (response_mime_type=application/json) com retry.
    Em cada falha de formato, imprime o texto bruto recebido.
    """
    client = _get_client()
    cfg = types.GenerateContentConfig(response_mime_type="application/json")

    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
        try:
            resp = client.models.generate_content(model=TEXT_MODEL, contents=contents, config=cfg)
            raw = (resp.text or "").strip()

            # Log/print do bruto sempre que der problema
            try:
                data = json.loads(raw) if raw else {}
            except Exception as je:
                logger.error("%s: JSON inválido (tentativa %d/%d): %s", stage_label, attempt, GEMINI_MAX_RETRIES, je)
                print("\n================= RAW JSON (", stage_label, f") tentativa {attempt}/{GEMINI_MAX_RETRIES} =================", sep="")
                print(raw)
                print("====================================================================================\n")
                continue

            if required_keys:
                if not all(k in data for k in required_keys):
                    logger.error("%s: JSON sem chaves necessárias (tentativa %d/%d).", stage_label, attempt, GEMINI_MAX_RETRIES)
                    print("\n================= RAW JSON (", stage_label, f") tentativa {attempt}/{GEMINI_MAX_RETRIES} =================", sep="")
                    print(json.dumps(data, indent=2, ensure_ascii=False))
                    print("====================================================================================\n")
                    continue

            # Sucesso
            return data

        except Exception as e:
            logger.error("%s: falha na chamada (tentativa %d/%d): %s", stage_label, attempt, GEMINI_MAX_RETRIES, e)

    # Depois de todas as tentativas, sem sucesso
    logger.error("%s: esgotadas %d tentativas sem JSON válido.", stage_label, GEMINI_MAX_RETRIES)
    return None

# ------------------------ Gemini (estágio 1) ------------------------
def _ask_gemini_viral_analysis(tema: str, persona: str, idioma: str) -> Dict[str, str]:
    logger.info("🧠 Estágio 1: Analisando tendências e definindo direção de cena para o tema '%s'...", tema)
    data_hoje = datetime.now().strftime("%Y-%m-%d")
    target_lang_map = {"pt": "Brazilian Portuguese", "ar": "Arabic", "en": "English", "ru": "Russian"}
    target_lang_name = target_lang_map.get(idioma.lower()[:2], "English")

    try:
        prompt_template = _load_prompt_template("stage_1_analysis")
        cmd = prompt_template.format(
            data_hoje=data_hoje, tema=tema, persona=persona, target_lang_name=target_lang_name
        )

        print("\n================ COMANDO ENVIADO AO GEMINI (ANÁLISE) ================\n")
        print(cmd)
        print("\n====================================================================\n")

        required = [
            "viral_angle", "keywords", "tone_of_voice", "visual_style",
            "cinematography_suggestions", "character_action", "hook_style", "cta_style"
        ]
        analysis = _gemini_generate_json(cmd, "Estágio 1 / Direção de Cena", required_keys=required)

        if analysis:
            logger.info("✅ Direção de cena definida com sucesso.")
            print("\n================ MANUAL DE DIREÇÃO (Estágio 1) ================\n")
            print(json.dumps(analysis, indent=2, ensure_ascii=False))
            print("\n==============================================================\n")
            return analysis

        # Fallback final
        raise ValueError("A resposta JSON do diretor não contém as chaves necessárias.")

    except Exception as e:
        logger.error("Falha no Estágio 1 (Direção de Cena): %s. Usando um fallback genérico.", e)
        return {
            "viral_angle": f"A personal message about {tema}.",
            "keywords": tema,
            "tone_of_voice": "Confident and welcoming.",
            "visual_style": "Cinematic selfie.",
            "cinematography_suggestions": "Alternate between Medium Shot and Close-up.",
            "character_action": "Speak directly to the camera with a gentle smile.",
            "hook_style": "Gentle and inviting.",
            "cta_style": "Soft and engaging."
        }

def _variation_policy_text(mode: str) -> str:
    if mode == "keep_all":
        return ("Keep the baseline wardrobe AND baseline background consistent across all scenes. "
                "Minor micro-variations allowed (camera angle / people in soft focus), but keep core look unchanged.")
    if mode == "change_bg":
        return "Keep baseline wardrobe; VARY the background between scenes (coherent locations)."
    if mode == "change_wardrobe":
        return "Keep baseline background; VARY wardrobe color/style between scenes, still coherent with character."
    if mode == "change_both":
        return "VARY both wardrobe (color/style) AND background, coherent with the character."
    return "Keep the baseline wardrobe AND baseline background consistent across all scenes."

def _scene_roles_text(n: int) -> str:
    if n <= 1:
        return "1) SINGLE — self-contained message with a soft engagement micro-CTA (like/comment/save), no links or services."
    if n == 2:
        return "1) HOOK — attention grab.\n2) MESSAGE — clear main theme (no CTA)."
    if n == 3:
        return "1) HOOK — attention grab.\n2) MESSAGE — clear main theme.\n3) CTA — single engagement call."
    middle = "\n".join([f"{i}) MESSAGE — main theme." for i in range(2, n)])
    return f"1) HOOK — attention grab.\n{middle}\n{n}) CTA — single engagement call."

def _build_gemini_command_full(
    persona: str, idioma: str, tema: str, n: int, variation_mode: str, viral_context: Dict[str, str]
) -> str:
    config = _load_persona_config(persona)
    role_persona = config["dossier"]
    target_lang_map = {
        "pt": "Brazilian Portuguese (pt-BR)",
        "ar": "Egyptian Arabic",
        "en": "English",
        "ru": "Russian",
    }
    target_lang = target_lang_map.get((idioma or "pt").lower()[:2], "English")
    example_prompt = config["example_prompt"]

    directorial_guidance = (
        f"DIRECTORIAL GUIDANCE (based on current trends):\n"
        f"- Viral Angle/Hook: {viral_context.get('viral_angle')}\n"
        f"- Keywords to include: {viral_context.get('keywords')}\n"
        f"- Tone of Voice: {viral_context.get('tone_of_voice')}\n"
        f"- Visual Style: {viral_context.get('visual_style')}\n"
        f"- Cinematography: {viral_context.get('cinematography_suggestions')}\n"
        f"- Character Action: {viral_context.get('character_action')}\n"
        f"- Hook Style: {viral_context.get('hook_style')}\n"
        f"- CTA Style: {viral_context.get('cta_style')}\n"
    )

    variation_text = _variation_policy_text(variation_mode)
    roles_text = _scene_roles_text(n)
    strict_rules_extra = (
        f"- Dialogue duration must be ≤ 8s. Word count must be strictly between {D_MIN}–{D_MAX}.\n"
        f"- End Dialogue with a full stop. No ellipses '...' or em dashes '—'.\n"
        f"- NEVER include the character name inside the Dialogue, and NEVER end the Dialogue with a name.\n"
    )
    cta_policy = (
        "CTA policy (engagement-only):\n"
        "- Only include a CTA when the Scene roles specify it.\n"
        "- CTA must request platform engagement ONLY (like, comment, save, share, follow).\n"
        "- FORBIDDEN in Dialogue: DMs, external links, “link na bio”, websites, services.\n"
    )

    prompt_template = _load_prompt_template("stage_2_scenes")
    return prompt_template.format(
        role_persona=role_persona, target_lang=target_lang, tema=tema,
        directorial_guidance=directorial_guidance, n=n,
        strict_rules_extra=strict_rules_extra, roles_text=roles_text,
        variation_text=variation_text, cta_policy=cta_policy,
        example_prompt=example_prompt
    )

def _parse_prompts_from_gemini_json(raw_text: str, n: int) -> List[str]:
    try:
        data = json.loads((raw_text or "").strip())
    except Exception as e:
        raise RuntimeError(f"Resposta do Gemini não é JSON válido: {e}")
    scenes = data.get("scenes") or []
    prompts = []
    for s in scenes:
        block = str(s.get("prompt", "")).strip()
        if not block:
            continue
        block = re.sub(r"^\s*```vbnet\s*|\s*```\s*$", "", block, flags=re.IGNORECASE).strip()
        if block:
            prompts.append(block)
    if prompts and len(prompts) != n:
        logger.warning("Gemini retornou um número de prompts diferente do esperado (%d/%d).", len(prompts), n)
    return prompts

def _regenerate_short_dialogue(client: "genai.Client", original_dialogue: str, max_words: int) -> Optional[str]:
    try:
        logger.info(f"🔄 Diálogo muito longo. Pedindo ao Gemini para reescrever com no máximo {max_words} palavras...")
        word_count = len(original_dialogue.split())
        prompt = (
            f"A frase a seguir tem {word_count} palavras, o que é muito longo. "
            f"Por favor, reescreva-a para transmitir exatamente a mesma ideia e tom, "
            f"mas usando um máximo absoluto de {max_words} palavras. "
            f"Responda APENAS com a frase reescrita, terminando com um ponto final.\n\n"
            f"Frase original: \"{original_dialogue}\""
        )
        resp = _get_client().models.generate_content(model=TEXT_MODEL, contents=prompt)
        new_dialogue = (resp.text or "").strip()
        if new_dialogue and len(new_dialogue.split()) <= max_words:
            logger.info(f"✅ Diálogo reescrito com sucesso: \"{new_dialogue}\"")
            return new_dialogue
        else:
            logger.warning("A reescrita do Gemini falhou ou ainda é muito longa. O fallback será usado.")
            return None
    except Exception as e:
        logger.error(f"Erro ao tentar regenerar diálogo: {e}")
        return None

def _strip_persona_name(dialogue: str, persona: str) -> str:
    if not dialogue:
        return dialogue
    names = {
        "yasmina": ["Yasmina", "ياسمينا", "YASMina"],
        "luisa": ["Luísa", "Luisa"],
        "alina": ["Alina", "Алина"],
    }
    alts = names.get((persona or "").lower(), [])
    if not alts:
        alts = [persona, persona.title()]
    tail = r"(?:\s*[-–—]?\s*(?:%s))\s*\.$" % "|".join(map(re.escape, alts))
    cleaned = re.sub(tail, ".", dialogue, flags=re.IGNORECASE | re.UNICODE)
    return cleaned

def _ask_gemini_scene_prompts(
    persona: str, idioma: str, tema: str, n: int, variation_mode: str, viral_context: Dict[str, str]
) -> List[str]:
    cmd = _build_gemini_command_full(persona, idioma, tema, n, variation_mode, viral_context)
    logger.info("🎬 Estágio 2: Gerando prompts de cena com base no manual de direção...")
    print("\n================ COMANDO ENVIADO AO GEMINI (CENAS) ================\n")
    print(cmd)
    print("\n===================================================================\n")

    client = _get_client()
    cfg = types.GenerateContentConfig(response_mime_type="application/json")

    last_raw = ""
    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
        try:
            resp = client.models.generate_content(model=TEXT_MODEL, contents=cmd, config=cfg)
            raw = (resp.text or "").strip()
            last_raw = raw

            # Mostrar SEMPRE o JSON bruto quando a tentativa falhar (abaixo)
            try:
                prompts = _parse_prompts_from_gemini_json(raw, n)
            except Exception as je:
                logger.error("Estágio 2: JSON inválido (tentativa %d/%d): %s", attempt, GEMINI_MAX_RETRIES, je)
                print("\n=============== RESPOSTA BRUTA DO GEMINI (CENAS) ===============\n")
                print(raw)
                print("\n=================================================================\n")
                continue

            if not prompts:
                logger.error("Estágio 2: JSON sem cenas (tentativa %d/%d).", attempt, GEMINI_MAX_RETRIES)
                print("\n=============== RESPOSTA BRUTA DO GEMINI (CENAS) ===============\n")
                print(raw)
                print("\n=================================================================\n")
                continue

            # Valida/ajusta os diálogos dentro do bloco
            dialogue_pattern = re.compile(
                r'(Dialogue:.*?spoken in .*?\n)(.*?)(?=\n\n|\nBackground sounds:)',
                re.DOTALL
            )

            def replacer(match: re.Match) -> str:
                header = match.group(1)
                original_dialogue = match.group(2).strip()
                original_dialogue = _strip_persona_name(original_dialogue, persona)
                word_count = len(original_dialogue.split())

                if D_MIN <= word_count <= D_MAX:
                    return f"{header}{original_dialogue}"

                if word_count > D_MAX:
                    logger.warning(f"Diálogo excedeu {D_MAX} palavras (tinha {word_count}). Tentando regenerar...")
                    new_dialogue = _regenerate_short_dialogue(client, original_dialogue, D_MAX)
                    if not new_dialogue:
                        words = original_dialogue.split()[:D_MAX]
                        new_dialogue = ' '.join(words)
                        if not new_dialogue.endswith('.'):
                            new_dialogue = re.sub(r'[,;:]\s*$', '', new_dialogue).strip() + '.'
                    return f"{header}{_strip_persona_name(new_dialogue, persona)}"

                return f"{header}{original_dialogue}"

            validated_prompts = [dialogue_pattern.sub(replacer, p_text) for p_text in prompts]
            return validated_prompts

        except Exception as e:
            logger.error("Estágio 2: falha na chamada (tentativa %d/%d): %s", attempt, GEMINI_MAX_RETRIES, e)

    # Se chegou aqui, esgotou tentativas — imprime último bruto pra análise
    if last_raw:
        print("\n=============== RESPOSTA BRUTA DO GEMINI (CENAS) — ÚLTIMA TENTATIVA ===============\n")
        print(last_raw)
        print("\n====================================================================================\n")
    return []

# ------------------------ FFmpeg helpers ------------------------
def _stitch_and_bgm_ffmpeg(mp4s: List[str], out_path: str, bgm_path: str = "", bgm_db: float = BG_MIX_DB) -> str:
    if not mp4s:
        raise ValueError("Lista de vídeos vazia.")
    ffmpeg = _ffmpeg_or_die()

    has_aud = [ _has_audio_stream(p) for p in mp4s ]
    durations = [ max(0.0, _video_duration(p)) for p in mp4s ]

    cmd = [ffmpeg, "-y", "-loglevel", "error", "-stats"]
    for p in mp4s:
        cmd += ["-i", p]

    bgm_idx = -1
    if bgm_path and os.path.isfile(bgm_path):
        cmd += ["-i", bgm_path]
        bgm_idx = len(mp4s)

    parts = []
    for i in range(len(mp4s)):
        parts.append(
            f"[{i}:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
            f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2,fps={FPS_OUT},format=yuv420p,setsar=1/1[v{i}];"
        )
        if has_aud[i]:
            parts.append(
                f"[{i}:a]aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={AUDIO_SR}[a{i}];"
            )
        else:
            dur = durations[i] if durations[i] > 0 else 8.0
            parts.append(
                f"anullsrc=r={AUDIO_SR}:cl=stereo,atrim=0:{dur:.3f},asetpts=N/SR/TB[a{i}];"
            )

    va = "".join([f"[v{i}][a{i}]" for i in range(len(mp4s))])
    parts.append(f"{va}concat=n={len(mp4s)}:v=1:a=1[vcat][acat];")

    if bgm_idx >= 0:
        parts.append(
            f"[{bgm_idx}:a]aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={AUDIO_SR},"
            f"aloop=loop=-1:size=2e9,aresample={AUDIO_SR}[bg_looped];"
        )
        parts.append("[acat]asplit[main_for_mix][voice_for_sc];")
        parts.append("[bg_looped][voice_for_sc]sidechaincompress=threshold=0.06:ratio=8:attack=100:release=500:detection=rms[bg_ducked];")
        vol = 10 ** (bgm_db / 20.0)
        parts.append(f"[main_for_mix][bg_ducked]amix=inputs=2:duration=first:dropout_transition=0:weights='1 {vol}'[aout]")
        vmap, amap = "[vcat]", "[aout]"
    else:
        vmap, amap = "[vcat]", "[acat]"

    filter_complex = "".join(parts)
    final = cmd + [
        "-filter_complex", filter_complex, "-map", vmap, "-map", amap,
        "-r", str(FPS_OUT), "-vsync", "cfr", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", "superfast", "-profile:v", "high", "-level", H264_LEVEL,
        "-b:v", BR_V, "-maxrate", BR_V, "-bufsize", "6M",
        "-c:a", "aac", "-b:a", BR_A, "-ar", str(AUDIO_SR), "-ac", "2",
        "-movflags", "+faststart+use_metadata_tags", "-map_metadata", "-1",
        "-x264-params", "keyint=60:min-keyint=60:scenecut=0",
        "-threads", str(max(1, os.cpu_count()//2)), out_path
    ]
    logger.info("🎬 FFmpeg (stitch com audio ducking):\n%s", " ".join(final))
    subprocess.run(final, check=True)
    return out_path

def _post_zoom_ffmpeg(src_path: str, dst_path: str, zoom: float) -> str:
    zoom = float(zoom or 1.0)
    if zoom <= 1.0001:
        shutil.copy2(src_path, dst_path)
        return dst_path
    filter_v = (
        f"[0:v]scale=iw*{zoom}:ih*{zoom},crop={OUT_W}:{OUT_H}:(iw-{OUT_W})/2:(ih-{OUT_H})/2,"
        f"fps={FPS_OUT},format=yuv420p,setsar=1/1[v]"
    )
    cmd = [
        _ffmpeg_or_die(), "-y", "-loglevel", "error", "-stats", "-i", src_path,
        "-filter_complex", filter_v, "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "superfast", "-profile:v", "high", "-level", H264_LEVEL,
        "-b:v", BR_V, "-maxrate", BR_V, "-bufsize", "6M",
        "-c:a", "aac", "-b:a", BR_A, "-ar", str(AUDIO_SR), "-ac", "2",
        "-movflags", "+faststart+use_metadata_tags", "-map_metadata", "-1",
        "-x264-params", "keyint=60:min-keyint=60:scenecut=0",
        "-threads", str(max(1, os.cpu_count()//2)), dst_path
    ]
    logger.info("🔍 FFmpeg (post-zoom %.2fx)", zoom)
    subprocess.run(cmd, check=True)
    return dst_path

# ------------------------ Limpeza pós-postagem ------------------------
_VID_RE = re.compile(r"^veo3_(?:test_)?(?P<slug>.+?)(?:_c(?P<idx>\d+)|_final.*)?\.mp4$", re.IGNORECASE)

def _cleanup_core_videos_for_slug(slug: str) -> None:
    """Remove os arquivos de cena (cN.mp4) e o final 'sem zoom', mantendo
    somente o arquivo postado (que normalmente já é removido pelo uploader)."""
    try:
        # cenas
        pattern = os.path.join(VIDEOS_DIR, f"veo3_{slug}_c*.mp4")
        removed = 0
        for p in glob.glob(pattern):
            try:
                os.remove(p)
                logger.info("🗑️ Arquivo removido (cena): %s", p)
                removed += 1
            except Exception as e:
                logger.warning("Não consegui remover %s: %s", p, e)

        # final "sem zoom" (se existir)
        p_final = os.path.join(VIDEOS_DIR, f"veo3_{slug}_final.mp4")
        if os.path.isfile(p_final):
            try:
                os.remove(p_final)
                logger.info("🗑️ Arquivo removido (final): %s", p_final)
            except Exception as e:
                logger.warning("Não consegui remover %s: %s", p_final, e)

        if removed == 0:
            logger.info("🧹 Limpeza: não havia cenas cN.mp4 para remover.")
    except Exception as e:
        logger.warning("Falha na limpeza pós-postagem: %s", e)

# ------------------------ TikTok posting ------------------------
_AR_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")
def _sanitize_arabic_hashtag(text: str) -> str:
    t = _AR_DIACRITICS.sub("", text or "")
    t = re.sub(r"\s+", "", t)
    return t

def _normalize_hashtags(hashtags, k: int = HASHTAGS_TOP_N) -> List[str]:
    out, seen = [], set()

    def _push(h: str):
        h = (h or "").strip()
        if not h:
            return
        if re.search(r"[\u0600-\u06FF]", h):
            h = _sanitize_arabic_hashtag(h)
        if not h.startswith("#"):
            h = "#" + re.sub(r"^[^\w\u0600-\u06FF]+", "", h, flags=re.UNICODE)
        if h and h not in seen:
            seen.add(h)
            out.append(h)

    if isinstance(hashtags, str):
        for t in re.split(r"\s+", hashtags.strip()):
            _push(t)
    else:
        try:
            for t in hashtags:
                _push(str(t))
        except Exception:
            pass
    return out[:k]

# EM veo3.py
def _postar_video(final_video: str, idioma: str, use_vpn: bool, *, cleanup_slug: bool = True) -> bool:
    # A chamada para _ensure_tiktok_headless_prompt foi removida.
    # A decisão do modo headless já foi feita no main.py.
    try:
        frase    = gerar_frase_motivacional(idioma=idioma)
        hashtags = gerar_hashtags_virais(frase, idioma=idioma, n=HASHTAGS_TOP_N)
        tags     = _normalize_hashtags(hashtags, k=HASHTAGS_TOP_N)
        desc     = (frase + (" " + " ".join(tags) if tags else "")).strip()
    except Exception as e:
        logger.warning("Falha ao gerar frase/hashtags; seguirei sem. (%s)", e)
        desc = ""

    capa_tmp = os.path.join(IMAGENS_DIR, f"capa_veo3_{abs(hash(final_video)) % (10**8)}.jpg")
    _extract_frame_ffmpeg(final_video, capa_tmp, t=1.0)

    ok = False
    try:
        ok = postar_no_tiktok_e_renomear(
            descricao_personalizada=desc,
            imagem_base=capa_tmp if os.path.isfile(capa_tmp) else "",
            imagem_final=capa_tmp if os.path.isfile(capa_tmp) else "",
            video_final=final_video,
            idioma=idioma,
            use_vpn=use_vpn
        )
    except Exception as e:
        logger.exception("❌ Falha ao postar no TikTok: %s", e)

    if ok:
        print("✅ Postado com sucesso.")
        if cleanup_slug:
            base = os.path.basename(final_video)
            m = _VID_RE.match(base)
            if m:
                _cleanup_core_videos_for_slug(m.group("slug"))
            else:
                logger.debug("Não consegui deduzir slug para limpeza a partir de: %s", base)
    else:
        print("⚠️ Upload não confirmado. Verifique os logs.")
    return bool(ok)

# ------------------------ Utilidades de arquivo ------------------------
def _listar_slugs() -> Dict[str, Dict[str, List[str]]]:
    items: Dict[str, Dict[str, List[str]]] = {}
    try:
        for name in os.listdir(VIDEOS_DIR):
            if not name.lower().endswith(".mp4"):
                continue
            m = _VID_RE.match(name)
            if not m:
                continue
            slug, idx = m.group("slug"), m.group("idx")
            d = items.setdefault(slug, {"cenas": [], "final": [], "test": []})
            path = os.path.join(VIDEOS_DIR, name)
            if idx:
                d["cenas"].append(path)
            elif "final" in name:
                d["final"].append(path)
            else:
                d["test"].append(path)
    except Exception as e:
        logger.debug("Falha ao listar vídeos: %s", e)

    for d in items.values():
        for k in d:
            d[k].sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return items

def _is_ok_video(path: str) -> bool:
    """Arquivo válido para pular geração: existe, duração > 1s e **tem trilha de áudio**."""
    try:
        if not os.path.isfile(path):
            return False
        if _video_duration(path) < 1.0:
            return False
        return _has_audio_stream(path)
    except Exception:
        return False

def _save_text(path: str, text: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

def _load_saved_prompts(slug: str) -> List[str]:
    prompts = []
    for i in range(1, 9):  # até 8 cenas se um dia aumentar
        p = os.path.join(PROMPTS_DIR, f"veo3_{slug}_c{i}.prompt.txt")
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                prompts.append(f.read())
        else:
            break
    return prompts

def _menu_reutilizar_videos(idioma: str, use_vpn: bool) -> None:
    slugs = _listar_slugs()
    if not slugs:
        print("\nNenhum vídeo Veo3 encontrado em ./videos.")
        return
    print("\nSelecione um conjunto (slug) para reutilizar:")
    ordered = sorted(
        slugs.items(),
        key=lambda kv: os.path.getmtime((kv[1]["final"] or kv[1]["cenas"] or kv[1]["test"])[0]),
        reverse=True
    )
    for i, (slug, d) in enumerate(ordered, 1):
        print(f"{i}. {slug}  — cenas:{len(d['cenas'])}  final:{len(d['final'])}  test:{len(d['test'])}")
    op = input("Número (Enter/b=cancelar): ").strip().lower()
    if not op or op in _BACK_TOKENS:
        print("Cancelado.")
        return
    if not op.isdigit() or not (1 <= int(op) <= len(ordered)):
        print("Cancelado.")
        return

    slug, d = ordered[int(op) - 1]
    if d["cenas"]:
        print("\nComo deseja proceder?")
        print("1. Juntar todas as cenas (cN) e POSTAR")
        print("2. POSTAR um arquivo específico")
        print("3. Juntar todas as cenas (cN) e **EXPORTAR** (sem postar)")
        print("b. Voltar")
        modo = input("Escolha 1, 2, 3 ou b: ").strip().lower()
        if modo in _BACK_TOKENS:
            return
    else:
        print("\nComo deseja proceder?")
        print("1. POSTAR um arquivo específico")
        print("2. **EXPORTAR** um arquivo específico (sem postar)")
        print("b. Voltar")
        tmp = input("Escolha 1, 2 ou b: ").strip().lower()
        if tmp in _BACK_TOKENS:
            return
        modo = {"1": "2", "2": "4"}.get(tmp, "2")

    if modo in ("1", "3"):
        if not d["cenas"]:
            print("Não há cenas para juntar.")
            return
        mp4s = sorted(d["cenas"], key=lambda p: int(_VID_RE.match(os.path.basename(p)).group("idx")))
        trilha, sufixo = _pick_bgm_path(), "final" if modo == "1" else "final_export"
        saida = os.path.join(VIDEOS_DIR, f"veo3_{slug}_{sufixo}.mp4")
        try:
            final_video = _stitch_and_bgm_ffmpeg(mp4s, saida, trilha, bgm_db=BG_MIX_DB)
        except Exception as e:
            logger.warning("Falha no stitch/BGM: %s", e)
            final_video = mp4s[0]
        if POST_ZOOM > 1.0001:
            z_out = os.path.splitext(final_video)[0] + f"_z{POST_ZOOM:.2f}.mp4"
            try:
                final_video = _post_zoom_ffmpeg(final_video, z_out, POST_ZOOM)
            except Exception as e:
                logger.warning("Falha no pós-zoom (%.2f): %s", POST_ZOOM, e)
        if modo == "3":
            print(f"✅ Exportado (sem postar): {final_video}")
            return
        # AJUSTE APLICADO AQUI
        _postar_video(final_video, idioma, use_vpn=use_vpn)
        return

    todos = d["final"] + d["test"] + d["cenas"]
    if not todos:
        print("Nenhum arquivo disponível.")
        return
    print("\nArquivos disponíveis:")
    for i, p in enumerate(todos, 1):
        print(f"{i}. {os.path.basename(p)}")
    op2 = input("Arquivo (número | b=voltar): ").strip().lower()
    if op2 in _BACK_TOKENS or not op2.isdigit() or not (1 <= int(op2) <= len(todos)):
        print("Cancelado.")
        return
    escolhido = todos[int(op2) - 1]
    if modo == "2":
        # AJUSTE APLICADO AQUI
        _postar_video(escolhido, idioma, use_vpn=use_vpn)
        return
    trilha = _pick_bgm_path()
    saida = os.path.join(VIDEOS_DIR, f"{os.path.splitext(os.path.basename(escolhido))[0]}_export.mp4")
    try:
        exportado = _stitch_and_bgm_ffmpeg([escolhido], saida, trilha, bgm_db=BG_MIX_DB)
        if POST_ZOOM > 1.0001:
            z_out = os.path.splitext(exportado)[0] + f"_z{POST_ZOOM:.2f}.mp4"
            exportado = _post_zoom_ffmpeg(exportado, z_out, POST_ZOOM)
        print(f"✅ Exportado (sem postar): {exportado}")
    except Exception as e:
        logger.warning("Falha ao exportar: %s", e)
        
# ------------------------ Fluxo interativo ------------------------
def _sugerir_assuntos(persona: str) -> List[str]:
    return ["Prosperidade", "Signos", "Sorte do dia", "Amor", "Proteção", "Limpeza energética"]

def _selecionar_assunto(persona: str) -> Optional[str]:
    print("\nAssunto do vídeo (escolha uma opção, '0' para digitar, 'b' p/ voltar):")
    exemplos = _sugerir_assuntos(persona)
    for i, e in enumerate(exemplos, 1):
        print(f"{i}. {e}")
    print("7. Aleatório **opção nova**")
    print("0. Outro (digitar manualmente)")
    op = input("Opção: ").strip().lower()
    if op in _BACK_TOKENS:
        return None
    if op == "0":
        tema = input("Digite o tema (curto): ").strip()
        return tema or "Prosperidade"
    if op == "7":
        return random.choice(exemplos)
    if op.isdigit() and 1 <= int(op) <= len(exemplos):
        return exemplos[int(op) - 1]
    return "Prosperidade"

def _selecionar_qtd_cenas() -> Optional[int]:
    print("\nQuantas cenas deseja gerar? (cada cena ≈ 8s)  [Enter=2 | b=voltar]")
    raw = input("Digite 1–4: ").strip().lower()
    if raw in _BACK_TOKENS:
        return None
    if raw == "":
        return 2
    try:
        n = int(raw)
        return max(1, min(4, n))
    except Exception:
        return 2

def _selecionar_variacao() -> Optional[str]:
    print("\nVariação de cenário/roupa:")
    print("1. Manter cenários e roupas padrão")
    print("2. Trocar cenário e manter roupa")
    print("3. Trocar roupa (cor/estilo) e manter cenário")
    print("4. Trocar roupa e trocar cenário")
    print("5. Aleatório")
    print("b. Voltar")
    op = input("Escolha 1,2,3,4,5 ou b: ").strip().lower()
    if op in _BACK_TOKENS:
        return None
    mapping = {"1": "keep_all", "2": "change_bg", "3": "change_wardrobe", "4": "change_both", "5": "random"}
    mode = mapping.get(op, "keep_all")
    if mode == "random":
        mode = random.choice(list(mapping.values())[:-1])
        print(f"(Aleatório) Selecionado: {mode.replace('_',' ')}")
    return mode

def _perguntar_headless_flow(default_on: bool) -> Optional[bool]:
    padrao = "Sim" if default_on else "Não"
    print("\nExecutar o Flow (Veo3) em modo headless?")
    print(f"Enter = {padrao}  |  1. Sim  |  2. Não  |  b. Voltar")
    op = input("Escolha: ").strip().lower()
    if op in _BACK_TOKENS:
        return None
    if op in {"1", "s", "sim"}:
        return True
    if op in {"2", "n", "nao", "não"}:
        return False
    return default_on

def _preview_and_confirm_prompts(slug: str, prompts: List[str]) -> Optional[bool]:
    print("\n================= PRÉVIA DE PROMPTS (todas as cenas) =================")
    for i, ptxt in enumerate(prompts, 1):
        prompt_file = os.path.join(PROMPTS_DIR, f"veo3_{slug}_c{i}.prompt.txt")
        _save_text(prompt_file, ptxt)
        print(f"\n--- CENA {i} ---\n{ptxt}\n(Salvo em: {prompt_file})")
    print("\n======================================================================")
    resp = input("Aprovar estes prompts e continuar? (s/N | b=voltar): ").strip().lower()
    if resp in _BACK_TOKENS:
        return None
    return resp == "s"

def _submenu_acao() -> Optional[str]:
    print("\nO que deseja fazer agora?")
    print("1. Gerar APENAS para testes (não postar)")
    print("2. Gerar e POSTAR")
    print("3. REUTILIZAR vídeo(s)")
    print("4. Gerar e **EXPORTAR** (sem postar)")
    print("5. Gerar FALTANTES (dos prompts salvos) e POSTAR")
    print("b. Voltar")
    op = input("Digite 1, 2, 3, 4, 5 ou b: ").strip().lower()
    if op in _BACK_TOKENS:
        return None
    return op if op in {"1", "2", "3", "4", "5"} else "1"

def _regenerar_cenas_faltantes(slug: str, idioma: str, persona: str) -> List[str]:
    prompts = _load_saved_prompts(slug)
    if not prompts:
        print("Não encontrei prompts salvos para este slug.")
        return []
    out_paths = [os.path.join(VIDEOS_DIR, f"veo3_{slug}_c{i+1}.mp4") for i in range(len(prompts))]
    logger.info("♻️ Regenerando cenas faltantes (%d no total); manterei as OK.", len(prompts))
    return _veo3_generate_batch(prompts, out_paths, idioma=idioma, persona=persona)

# VERSÃO NOVA para veo3.py
def executar_interativo(persona: str, idioma: str, use_vpn: bool) -> None:
    persona, idioma = (persona or "luisa").lower(), (idioma or "pt-br").lower()
    global VEO3_HEADLESS
    if USE_BACKEND == "flow":
        ans = _perguntar_headless_flow(VEO3_HEADLESS)
        if ans is None: return
        VEO3_HEADLESS = bool(ans)
        os.environ["VEO3_CHROME_HEADLESS"] = "1" if VEO3_HEADLESS else "0"
        logger.info("→ Flow headless: %s", "ON" if VEO3_HEADLESS else "OFF")
    
    while True:
        tema = _selecionar_assunto(persona)
        if tema is None: return
        n_cenas = _selecionar_qtd_cenas()
        if n_cenas is None: continue
        variacao = _selecionar_variacao()
        if variacao is None: continue
        escolha = _submenu_acao()
        if escolha is None: continue
        slug = re.sub(r"[^a-z0-9]+", "-", f"{persona}-{tema.lower()}").strip("-")
        if escolha == "3": _menu_reutilizar_videos(idioma, use_vpn); return # Passa use_vpn
        if escolha == "5":
            mp4s = _regenerar_cenas_faltantes(slug, idioma, persona)
            if not mp4s: return
            trilha = _pick_bgm_path()
            saida = os.path.join(VIDEOS_DIR, f"veo3_{slug}_final.mp4")
            try: final_video = _stitch_and_bgm_ffmpeg(mp4s, saida, trilha, bgm_db=BG_MIX_DB)
            except Exception as e: logger.warning("Falha ao juntar/BGM (%s). Usarei a primeira cena.", e); final_video = mp4s[0]
            if POST_ZOOM > 1.0001:
                z_out = os.path.splitext(final_video)[0] + f"_z{POST_ZOOM:.2f}.mp4"
                try: final_video = _post_zoom_ffmpeg(final_video, z_out, POST_ZOOM)
                except Exception as e: logger.warning("Falha no pós-zoom (%.2f): %s", POST_ZOOM, e)
            _postar_video(final_video, idioma, use_vpn=use_vpn, cleanup_slug=True) # Passa use_vpn
            return
        try:
            viral_context = _ask_gemini_viral_analysis(tema, persona, idioma)
            prompts = _ask_gemini_scene_prompts(persona, idioma, tema, n_cenas, variacao, viral_context)
        except Exception as e:
            logger.exception("Falha ao obter prompts do Gemini: %s", e); print("❌ Não foi possível obter prompts do Gemini. Tente novamente."); return
        if not prompts: print("❌ Nenhum prompt foi gerado pelo Gemini. Voltando ao menu."); continue
        approved = _preview_and_confirm_prompts(slug, prompts)
        if approved is None: continue
        if not approved: print("❌ Prompts reprovados. Voltando ao menu."); continue
        out_paths = [os.path.join(VIDEOS_DIR, f"veo3_{slug}_c{i+1}.mp4") for i in range(len(prompts))]
        logger.info("🎬 Gerando %d cena(s)…", len(prompts))
        mp4s = _veo3_generate_batch(prompts, out_paths, idioma=idioma, persona=persona)
        if not mp4s: print("❌ A geração de vídeo falhou. Verifique os logs."); return
        if escolha == "1":
            print("\n✅ Cenas de teste geradas:")
            for p in mp4s: print(" -", p)
            print("\nUse 'Reutilizar' ou a opção 5 para juntar/postar/exportar.")
            return
        trilha = _pick_bgm_path()
        saida = os.path.join(VIDEOS_DIR, f"veo3_{slug}_final.mp4")
        try: final_video = _stitch_and_bgm_ffmpeg(mp4s, saida, trilha, bgm_db=BG_MIX_DB)
        except Exception as e: logger.warning("Falha ao juntar/BGM (%s). Usarei a primeira cena.", e); final_video = mp4s[0]
        if POST_ZOOM > 1.0001:
            z_out = os.path.splitext(final_video)[0] + f"_z{POST_ZOOM:.2f}.mp4"
            try: final_video = _post_zoom_ffmpeg(final_video, z_out, POST_ZOOM)
            except Exception as e: logger.warning("Falha no pós-zoom (%.2f): %s", POST_ZOOM, e)
        if escolha == "4": print(f"✅ Exportado (sem postar): {final_video}"); return
        _postar_video(final_video, idioma, use_vpn=use_vpn, cleanup_slug=True) # Passa use_vpn
        return
# ------------------------ Batch (Flow/API) ------------------------
def _veo3_generate_single_api(prompt_text: str, out_path: str, idioma: str, persona: str) -> str:
    raise NotImplementedError("Backend API não implementado. Use VEO3_BACKEND=flow.")

def _veo3_generate_batch(prompts: List[str], out_paths: List[str], idioma: str, persona: str) -> List[str]:
    """Geração que respeita arquivos já prontos (resume)."""
    assert len(prompts) == len(out_paths) and prompts, "prompts/out_paths inconsistentes"

    # 1) separa cenas já OK x faltantes
    ok_paths, todo_prompts, todo_outs = [], [], []
    for pr, op in zip(prompts, out_paths):
        if _is_ok_video(op):
            logger.info("⏭️  Pulando cena já pronta (com áudio): %s", os.path.basename(op))
            ok_paths.append(op)
        else:
            todo_prompts.append(pr)
            todo_outs.append(op)

    done_paths: List[str] = ok_paths[:]

    if not todo_prompts:
        logger.info("Todas as cenas já estavam prontas.")
        return out_paths

    if USE_BACKEND == "flow":
        if not (FLOW_PROJECT_URL and os.path.isfile(FLOW_COOKIES_FILE) and _HAVE_FLOW):
            raise RuntimeError("Backend Flow mal configurado (URL, cookies ou utils/veo3_flow.py).")

        # 2) gera somente as faltantes
        if len(todo_prompts) > 1 and _HAVE_FLOW_MANY:
            logger.info("🌐 Flow backend: generate_many_via_flow (%d cenas faltantes)…", len(todo_prompts))
            try:
                new_paths = generate_many_via_flow(
                    prompts=todo_prompts,
                    out_paths=todo_outs,
                    project_url=FLOW_PROJECT_URL,
                    cookies_file=FLOW_COOKIES_FILE,
                    headless=VEO3_HEADLESS,
                    timeout_sec=int(float(os.getenv("FLOW_TIMEOUT_SEC", "600"))),
                )
                done_paths.extend(new_paths)
                return sorted(done_paths, key=lambda p: p)
            except Exception as e:
                logger.warning("generate_many_via_flow falhou (%s); farei single por cena.", e)

        logger.info("🌐 Flow backend: generate_single_via_flow por cena (faltantes)…")
        for ptxt, outp in zip(todo_prompts, todo_outs):
            got = generate_single_via_flow(
                prompt_text=ptxt,
                out_path=outp,
                project_url=FLOW_PROJECT_URL,
                cookies_file=FLOW_COOKIES_FILE,
                headless=VEO3_HEADLESS,
                timeout_sec=int(float(os.getenv("FLOW_TIMEOUT_SEC", "600"))),
            )
            # garante que o arquivo final está no caminho esperado
            if got and got != outp and os.path.isfile(got):
                try:
                    shutil.move(got, outp)
                except Exception:
                    shutil.copy2(got, outp)
            done_paths.append(outp)
        return sorted(done_paths, key=lambda p: p)

    logger.info("🧠 API backend: gerando %d cena(s)…", len(todo_prompts))
    for p, o in zip(todo_prompts, todo_outs):
        got = _veo3_generate_single_api(p, o, idioma, persona)
        if got and got != o and os.path.isfile(got):
            try:
                shutil.move(got, o)
            except Exception:
                shutil.copy2(got, o)
        done_paths.append(o)
    return sorted(done_paths, key=lambda p: p)

# ------------------------ Automático (com pré-config) ------------------------
# VERSÃO NOVA para veo3.py
def postar_em_intervalo(persona: str, idioma: str, cada_horas: float, use_vpn: bool) -> None:
    persona, idioma = (persona or "luisa").lower(), (idioma or "pt-br").lower()
    global VEO3_HEADLESS
    if USE_BACKEND == "flow":
        ans = _perguntar_headless_flow(VEO3_HEADLESS)
        if ans is None: print("Cancelado."); return
        VEO3_HEADLESS = bool(ans)
        os.environ["VEO3_CHROME_HEADLESS"] = "1" if VEO3_HEADLESS else "0"
        logger.info("→ Flow headless: %s", "ON" if VEO3_HEADLESS else "OFF")
    print("\n=== Configuração do Veo3 Automático ===")
    tema_fix = _selecionar_assunto(persona)
    if tema_fix is None: print("Cancelado."); return
    usar_tema_aleatorio = tema_fix.strip().lower() in {"random", "aleatorio", "aleatório"}
    n_cenas = _selecionar_qtd_cenas()
    if n_cenas is None: print("Cancelado."); return
    variacao_sel = _selecionar_variacao()
    if variacao_sel is None: print("Cancelado."); return
    variacao_random = (variacao_sel == "random")
    base_temas = _sugerir_assuntos(persona)
    intervalo = float(cada_horas) * 3600.0
    
    try:
        while True:
            inicio = datetime.now()
            tema = random.choice(base_temas) if usar_tema_aleatorio else tema_fix
            print(f"\n🟢 Veo3 automático — {inicio:%d/%m %H:%M} | persona={persona} | tema={tema}")
            success = False
            try:
                viral_context = _ask_gemini_viral_analysis(tema, persona, idioma)
                variacao = (random.choice(["keep_all", "change_bg", "change_wardrobe", "change_both"]) if variacao_random else variacao_sel)
                prompts = _ask_gemini_scene_prompts(persona, idioma, tema, n=n_cenas, variation_mode=variacao, viral_context=viral_context)
                if not prompts: raise RuntimeError(f"Geração de prompts retornou lista vazia para o tema '{tema}'.")
                slug = re.sub(r"[^a-z0-9]+", "-", f"{persona}-{tema.lower()}").strip("-")
                for i, ptxt in enumerate(prompts, 1): _save_text(os.path.join(PROMPTS_DIR, f"veo3_{slug}_c{i}.prompt.txt"), ptxt)
                out_paths = [os.path.join(VIDEOS_DIR, f"veo3_{slug}_c{i+1}.mp4") for i in range(len(prompts))]
                mp4s = _veo3_generate_batch(prompts, out_paths, idioma=idioma, persona=persona)
                if not mp4s: raise RuntimeError("Geração de vídeo batch não retornou arquivos.")
                trilha = _pick_bgm_path()
                saida = os.path.join(VIDEOS_DIR, f"veo3_{slug}_final.mp4")
                try: final_video = _stitch_and_bgm_ffmpeg(mp4s, saida, trilha, bgm_db=BG_MIX_DB)
                except Exception as e: logger.warning("Falha stitch/BGM (%s). Publicarei a primeira cena.", e); final_video = mp4s[0]
                if POST_ZOOM > 1.0001:
                    z_out = os.path.splitext(final_video)[0] + f"_z{POST_ZOOM:.2f}.mp4"
                    try: final_video = _post_zoom_ffmpeg(final_video, z_out, POST_ZOOM)
                    except Exception as e: logger.warning("Falha no pós-zoom auto (%.2f): %s", POST_ZOOM, e)
                success = _postar_video(final_video, idioma, use_vpn=use_vpn, cleanup_slug=True) # Passa use_vpn
            except Exception as e: logger.exception("Falha no ciclo Veo3: %s", e)
            retry_min = _auto_retry_minutes()
            if not success and retry_min > 0:
                proxima = datetime.now() + timedelta(minutes=retry_min)
                logger.warning("⚠️ Falha na execução; nova tentativa em %d min (às %s).", retry_min, proxima.strftime('%H:%M'))
            else: proxima = inicio + timedelta(seconds=intervalo)
            while True:
                rem = (proxima - datetime.now()).total_seconds()
                if rem <= 0: break
                print(f"Próxima execução em {rem/3600.0:.2f} horas...", end="\r")
                time.sleep(min(30.0, rem))
    except KeyboardInterrupt: print("\n🛑 Veo3 automático interrompido.")