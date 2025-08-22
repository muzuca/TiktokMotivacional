# utils/veo3.py
# Veo3 (Flow via Selenium OU Gemini API) + FFmpeg
# - Gera 1..N cenas; no Flow usa generate_many_via_flow (mesma sessão, baixa e exclui cada vídeo)
# - Normaliza cada cena para 9:16 (1080x1920) via scale+pad (SEM zoom)
# - Junta cenas (concat) + BGM opcional; aplica PÓS-ZOOM opcional no final
# - Menu: testar, gerar+postar, reutilizar, e **gerar+exportar (sem postar)**
from __future__ import annotations
import os, re, json, time, shutil, random, logging, subprocess
from typing import List, Dict
from datetime import datetime, timedelta

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

# Saída final (vertical)
OUT_W, OUT_H = 1080, 1920
FPS_OUT      = 30
AUDIO_SR     = 44100

# Bitrates / H264
BR_V = "5000k"
BR_A = "192k"
H264_LEVEL = "4.0"

# Pós-zoom (aplicado DEPOIS do vídeo já 1080x1920). 1.0 = desliga
POST_ZOOM = float(os.getenv("VEO3_POST_ZOOM", "1.0"))  # ex.: 1.30 = +30%

# BGM
BG_MIX_DB = float(os.getenv("BG_MIX_DB", "-20.0"))

# Modelos
VEO3_MODEL  = os.getenv("VEO3_MODEL", "veo-3.0-generate-preview").strip()
TEXT_MODEL  = os.getenv("VEO3_TEXT_MODEL", "gemini-2.0-flash-001").strip()

# Regras de diálogo
D_MIN = int(os.getenv("VEO3_DIALOGUE_MIN_WORDS", "18"))
D_MAX = int(os.getenv("VEO3_DIALOGUE_MAX_WORDS", "25"))

NEGATIVE_PROMPT = (
    "cartoon, drawing, low quality, branding, readable text, fantasy effects, "
    "facial change, outfit inconsistency, visual distortion, watermark, logo"
)

# Backend: flow (site) ou api
USE_BACKEND = os.getenv("VEO3_BACKEND", "flow").strip().lower()  # "api" ou "flow"
FLOW_PROJECT_URL  = os.getenv("FLOW_PROJECT_URL", "").strip()
FLOW_COOKIES_FILE = os.getenv("FLOW_COOKIES_FILE", "cookies_veo3.txt").strip()
VEO3_HEADLESS     = os.getenv("VEO3_CHROME_HEADLESS", "1").strip() != "0"

# Flow helpers
_HAVE_FLOW = False
_HAVE_FLOW_MANY = False
try:
    if USE_BACKEND == "flow":
        from utils.veo3_flow import generate_single_via_flow
        _HAVE_FLOW = True
        try:
            from utils.veo3_flow import generate_many_via_flow  # preferido (mesma sessão)
            _HAVE_FLOW_MANY = True
        except Exception:
            _HAVE_FLOW_MANY = False
except Exception as e:
    _HAVE_FLOW = False
    _HAVE_FLOW_MANY = False
    logger.warning("Flow backend indisponível: %s", e)

# ========= FFmpeg helpers =========
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

def _extract_frame_ffmpeg(video_path: str, out_jpg: str, t: float = 1.0) -> str:
    try:
        subprocess.run([
            _ffmpeg_or_die(), "-y",
            "-ss", f"{max(0.0, t):.3f}", "-i", video_path,
            "-frames:v", "1", "-q:v", "2", out_jpg
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return out_jpg if os.path.isfile(out_jpg) else ""
    except Exception:
        return ""

def _pick_bgm_path() -> str:
    # 1) util externo (se disponível)
    if callable(_pick_bgm_util):
        try:
            p = _pick_bgm_util()
            if p and os.path.isfile(p):
                return p
        except Exception as e:
            logger.debug("Falha ao escolher trilha via utils.audio: %s", e)
    # 2) varredura simples em ./audios
    base = "audios"
    if os.path.isdir(base):
        exts = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")
        cands = [os.path.join(base, f) for f in os.listdir(base) if f.lower().endswith(exts)]
        if cands:
            return random.choice(cands)
    return ""

# ========= Exemplo de prompt completo (few-shot p/ orientar o Gemini) =========
EXAMPLE_PROMPT_LUISA = (
    "Subject: A hyper-realistic cinematic selfie video of a Brazilian fortune teller named Luísa walking through an open city square with colorful trees and a central fountain during golden hour, delivering a bold intuitive hook.\n\n"
    "Character: Brazilian woman around 28 years old with light brown skin and golden undertones. Face: oval shape with soft cheekbones, straight nose, full lips with soft pink lipstick, long natural lashes, subtle brown eyeliner, thick softly arched brows, light-brown eyes with tiny amber flecks, a faint beauty mark below the right cheekbone. Hair: long wavy chestnut hair parted to the side, a few strands tucked behind the left ear. Outfit: sleeveless deep sapphire-blue dress, pink-quartz pendant necklace, matching earrings. Nails: almond-shaped, soft blush pink. Presence: warm, confident, magnetic.\n\n"
    "Background: A real open public square with mosaic paving, a stone fountain, benches, kiosks, and colorful trees (jacaranda and bougainvillea). People move in soft focus behind her; no readable signs or logos.\n\n"
    "Lighting: Golden-hour sunlight with warm flares and natural highlights on hair, skin, jewelry, and water.\n\n"
    "Action: Luísa records in selfie mode while walking slowly past the fountain, bringing the phone slightly closer as she begins speaking.\n\n"
    "Style: Hyper-realistic cinematic selfie, 16:9, gentle handheld movement.\n\n"
    "Dialogue: spoken in Brazilian Portuguese\n“Se este vídeo te encontrou, escuta; a cidade respira diferente; sua intuição está chamando; hoje você decide algo simples; amanhã sua vida muda de direção.”\n\n"
    "Background sounds: Fountain water, soft city chatter, footsteps on stone, distant birds.\n"
    "Music: None\n"
    "Model: veo-3\n"
    "Length: 8 seconds\n"
    "Resolution: 1080p (16:9)\n"
    "Framerate: 24fps\n"
    "Negative prompt: No branding, no readable text, no fantasy effects, no facial change, no outfit inconsistency, no visual distortion."
)

# ========= Gemini client =========
def _get_client() -> "genai.Client":
    if not _HAVE_GENAI:
        raise RuntimeError("Instale: pip install -U google-genai")
    return genai.Client()

# ========= PROMPTS COMPLETOS (sem pós-processar o texto do Gemini) =========
def _variation_policy_text(mode: str) -> str:
    if mode == "keep_all":
        return ("Keep the baseline wardrobe AND baseline background consistent across all scenes. "
                "Minor micro-variations allowed (camera angle / people in soft focus), but keep core look unchanged.")
    if mode == "change_bg":
        return ("Keep baseline wardrobe; VARY the background between scenes (coherent locations).")
    if mode == "change_wardrobe":
        return ("Keep baseline background; VARY wardrobe color/style between scenes, still coherent with character.")
    if mode == "change_both":
        return ("VARY both wardrobe (color/style) AND background between scenes, coherent with the character.")
    return "Keep the baseline wardrobe AND baseline background consistent across all scenes."

def _scene_roles_text(n: int) -> str:
    if n <= 1:
        return "1) SINGLE — short self-contained message with soft closure."
    if n == 2:
        return "1) HOOK — attention grab.\n2) MESSAGE — clear main theme."
    if n == 3:
        return "1) HOOK — attention grab.\n2) MESSAGE — clear main theme.\n3) CTA — clear single call to action."
    # 4 ou mais
    middle = "\n".join([f"{i}) MESSAGE — main theme." for i in range(2, n)])
    return f"1) HOOK — attention grab.\n{middle}\n{n}) CTA — clear single call to action."

def _scene_roles_text(n: int) -> str:
    if n <= 1:
        # cena única deve fechar o assunto e ter micro-CTA de engajamento (sem links/serviços)
        return "1) SINGLE — self-contained message with a soft engagement micro-CTA (like/comment/save), no links or services."
    if n == 2:
        return "1) HOOK — attention grab.\n2) MESSAGE — clear main theme (no CTA)."
    if n == 3:
        return "1) HOOK — attention grab.\n2) MESSAGE — clear main theme.\n3) CTA — single engagement call."
    # 4 ou mais
    middle = "\n".join([f"{i}) MESSAGE — main theme." for i in range(2, n)])
    return f"1) HOOK — attention grab.\n{middle}\n{n}) CTA — single engagement call."

def _variation_policy_text(mode: str) -> str:
    if mode == "keep_all":
        return ("Keep the baseline wardrobe AND baseline background consistent across all scenes. "
                "Minor micro-variations allowed (camera angle / people in soft focus), but keep core look unchanged.")
    if mode == "change_bg":
        return ("Keep baseline wardrobe; VARY the background between scenes (coherent locations).")
    if mode == "change_wardrobe":
        return ("Keep baseline background; VARY wardrobe color/style between scenes, still coherent with character.")
    if mode == "change_both":
        return ("VARY both wardrobe (color/style) AND background between scenes, coherent with the character.")
    return "Keep the baseline wardrobe AND baseline background consistent across all scenes."

def _build_gemini_command_full(persona: str, idioma: str, tema: str, n: int, variation_mode: str) -> str:
    persona = (persona or "luisa").lower()
    idioma  = (idioma or "pt-br").lower()
    role_persona = (
        "Luísa, Brazilian fortune teller (pt-BR). About 28; light brown skin with golden undertones; "
        "oval face; soft cheekbones; straight nose; full lips with soft pink lipstick; long natural lashes; "
        "subtle brown eyeliner; thick softly arched brows; light-brown eyes with tiny amber flecks; "
        "faint beauty mark below right cheekbone; long wavy chestnut hair, side part; a few strands tucked behind the left ear; "
        "warm, confident, magnetic presence. Baseline wardrobe: sleeveless deep sapphire-blue dress, pink-quartz pendant necklace, "
        "matching earrings; nails: almond-shaped, soft blush pink."
    )
    target_lang = "Brazilian Portuguese (pt-BR)" if idioma.startswith("pt") else ("Egyptian Arabic" if idioma.startswith("ar") else "English")
    variation_text = _variation_policy_text(variation_mode)
    roles_text     = _scene_roles_text(n)

    # limites vindos do .env
    D_MIN = int(os.getenv("VEO3_DIALOGUE_MIN_WORDS", "18"))
    D_MAX = int(os.getenv("VEO3_DIALOGUE_MAX_WORDS", "25"))

    strict_rules_extra = (
        f"- Spoken duration per Dialogue must be ≤ 8 seconds. Aim ~2.3–2.7 words/second.\n"
        f"- Keep Dialogue strictly within {D_MIN}–{D_MAX} words; never exceed the max.\n"
        f"- End Dialogue as a complete clause with a full stop; avoid ending on function words (e.g., de/da/do/com/para...).\n"
        f"- NO ellipses '...' and NO em dashes '—'; use commas/semicolons; include surrounding quotes.\n"
    )

    cta_policy = (
        "CTA policy (engagement-only):\n"
        "- Only include a CTA when the Scene roles specify it (i.e., last scene in 3+ scenes; micro-CTA in single-scene).\n"
        "- CTA must request platform engagement ONLY (choose 1–2 max): like (curtir), comment (comentar o que fez sentido), save (salvar), share (compartilhar), follow (seguir).\n"
        "- Provide an explicit comment cue, e.g., “curte este vídeo e comenta o que fez sentido para você”.\n"
        "- FORBIDDEN in Dialogue: appointments, consultations, DMs, private messages, external links, “link na bio”, websites, pricing, services, WhatsApp/Telegram/email.\n"
        "- Tone: warm, brief, non-pushy, in pt-BR."
    )

    cmd = f"""
You are a Veo3 prompt writer for short vertical videos (~8s per scene).
Character dossier (baseline): {role_persona}
Target language for Dialogue: {target_lang}
Theme: {tema or 'Prosperidade'}

Return multiple independent scenes. Each scene MUST be a fully stand-alone Veo3 prompt in ONE ```vbnet``` block with the headings in this exact order:
Subject:
Character:
Background:
Lighting:
Action:
Style:
Dialogue: spoken in Brazilian Portuguese
"<{D_MIN}–{D_MAX} words, no em dash, no ellipses, natural and pronounceable>"
Background sounds:
Music:
Model:
Length:
Resolution:
Framerate:
Negative prompt:

STRICT RULES:
- Respond JSON only (see schema). Each scene independent—no references to previous scenes.
- Dialogue ONLY in target language; all other fields strictly in English.
- Style must be vertical 9:16. Resolution must be 1080x1920 (9:16). Length 8 seconds. Model veo-3.
- Negative prompt present and realistic.
{strict_rules_extra}
Scene roles (in order):
{roles_text}

Variation policy:
{variation_text}

{cta_policy}

Use the following example ONLY to learn the structure/level of detail (do not copy text):
{EXAMPLE_PROMPT_LUISA}

JSON SCHEMA (respond strictly as JSON, no extra text):
{{
  "scenes": [
    {{"title": "Scene 1 — ...", "prompt": "```vbnet\\nSubject: ...\\n...\\n```"}},
    ...
  ]
}}
Generate exactly {n} scenes.
""".strip()
    return cmd

def _parse_prompts_from_gemini_json(raw_text: str, n: int) -> List[str]:
    try:
        data = json.loads((raw_text or "").strip())
    except Exception as e:
        raise RuntimeError(f"Resposta do Gemini não é JSON válido: {e}")
    scenes = data.get("scenes") or []
    prompts = []
    for s in scenes:
        block = str(s.get("prompt","")).strip()
        if not block:
            continue
        # normaliza casos raros de ```vbnet``` duplicados
        block = re.sub(r"```vbnet\s*```vbnet", "```vbnet", block, flags=re.IGNORECASE)
        prompts.append(block)
    if len(prompts) != n:
        raise RuntimeError(f"Gemini retornou {len(prompts)}/{n} prompts.")
    return prompts

def _ask_gemini_prompts_full(persona: str, idioma: str, tema: str, n: int, variation_mode: str) -> List[str]:
    if not _HAVE_GENAI:
        raise RuntimeError("Gemini SDK indisponível. Instale: pip install -U google-genai")
    client = _get_client()
    cmd = _build_gemini_command_full(persona, idioma, tema, n, variation_mode)

    # imprime o comando enviado
    print("\n================ COMANDO ENVIADO AO GEMINI ================\n")
    print(cmd)
    print("\n===========================================================\n")

    logger.info("📝 Gemini (prompts completos) — tema=%s | n=%d | persona=%s | idioma=%s | var=%s",
                tema, n, persona, idioma, variation_mode)
    cfg = types.GenerateContentConfig(response_mime_type="application/json")
    resp = client.models.generate_content(model=TEXT_MODEL, contents=cmd, config=cfg)
    raw = (resp.text or "").strip()
    return _parse_prompts_from_gemini_json(raw, n)

# ========= Normalização (sem zoom), concat e BGM =========
def _filter_for_input_nozoom(i: int) -> str:
    # scale + pad (SEM zoom), preserva AR e preenche 9:16 com barras quando precisa
    return (
        f"[{i}:v]"
        f"scale={OUT_W}:-1:force_original_aspect_ratio=decrease,"
        f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={FPS_OUT},format=yuv420p,setsar=1/1[v{i}];"
        f"[{i}:a]aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={AUDIO_SR},"
        f"aresample={AUDIO_SR}[a{i}];"
    )

def _stitch_and_bgm_ffmpeg(mp4s: List[str], out_path: str, bgm_path: str = "", bgm_db: float = BG_MIX_DB) -> str:
    if not mp4s:
        raise ValueError("Lista de vídeos vazia.")
    ffmpeg = _ffmpeg_or_die()
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-stats"]
    for p in mp4s:
        cmd += ["-i", p]
    bgm_idx = -1
    if bgm_path and os.path.isfile(bgm_path):
        cmd += ["-i", bgm_path]
        bgm_idx = len(mp4s)

    parts = []
    for i in range(len(mp4s)):
        parts.append(_filter_for_input_nozoom(i))
    va = "".join([f"[v{i}][a{i}]" for i in range(len(mp4s))])
    parts.append(f"{va}concat=n={len(mp4s)}:v=1:a=1[vcat][acat];")
    if bgm_idx >= 0:
        vol = 10 ** (bgm_db / 20.0)
        parts.append(
            f"[{bgm_idx}:a]"
            f"aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={AUDIO_SR},"
            f"volume={vol},aresample={AUDIO_SR}[bg];"
            f"[acat][bg]amix=inputs=2:duration=first:dropout_transition=0[aout]"
        )
        vmap, amap = "[vcat]", "[aout]"
    else:
        vmap, amap = "[vcat]", "[acat]"

    filter_complex = "".join(parts)
    final = cmd + [
        "-filter_complex", filter_complex,
        "-map", vmap, "-map", amap,
        "-r", str(FPS_OUT), "-vsync", "cfr",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-preset", "superfast",
        "-profile:v", "high", "-level", H264_LEVEL,
        "-b:v", BR_V, "-maxrate", BR_V, "-bufsize", "6M",
        "-c:a", "aac", "-b:a", BR_A, "-ar", str(AUDIO_SR), "-ac", "2",
        "-movflags", "+faststart+use_metadata_tags", "-map_metadata", "-1",
        "-x264-params", "keyint=60:min-keyint=60:scenecut=0",
        "-threads", str(max(1, os.cpu_count()//2)),
        out_path
    ]
    logger.info("🎬 FFmpeg (stitch):\n%s", "\n".join(final))
    subprocess.run(final, check=True)
    return out_path

# ========= PÓS-ZOOM (segundo passe) =========
def _post_zoom_ffmpeg(src_path: str, dst_path: str, zoom: float) -> str:
    zoom = float(zoom or 1.0)
    if zoom <= 1.0001:
        # cópia (reencode leve para manter flags)
        subprocess.run([
            _ffmpeg_or_die(), "-y", "-loglevel", "error", "-stats",
            "-i", src_path,
            "-c:v", "libx264", "-preset", "superfast",
            "-profile:v", "high", "-level", H264_LEVEL,
            "-pix_fmt", "yuv420p", "-r", str(FPS_OUT),
            "-c:a", "aac", "-b:a", BR_A, "-ar", str(AUDIO_SR), "-ac", "2",
            "-movflags", "+faststart+use_metadata_tags", "-map_metadata", "-1",
            "-x264-params", "keyint=60:min-keyint=60:scenecut=0",
            "-threads", str(max(1, os.cpu_count()//2)),
            dst_path
        ], check=True)
        return dst_path

    filter_v = (
        f"[0:v]"
        f"scale=iw*{zoom}:ih*{zoom},"
        f"crop={OUT_W}:{OUT_H}:(iw-{OUT_W})/2:(ih-{OUT_H})/2,"
        f"fps={FPS_OUT},format=yuv420p,setsar=1/1[v]"
    )
    cmd = [
        _ffmpeg_or_die(), "-y", "-loglevel", "error", "-stats",
        "-i", src_path,
        "-filter_complex", filter_v,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "superfast",
        "-profile:v", "high", "-level", H264_LEVEL,
        "-b:v", BR_V, "-maxrate", BR_V, "-bufsize", "6M",
        "-c:a", "aac", "-b:a", BR_A, "-ar", str(AUDIO_SR), "-ac", "2",
        "-movflags", "+faststart+use_metadata_tags", "-map_metadata", "-1",
        "-x264-params", "keyint=60:min-keyint=60:scenecut=0",
        "-threads", str(max(1, os.cpu_count()//2)),
        dst_path
    ]
    logger.info("🔍 FFmpeg (post-zoom %.2fx):\n%s", zoom, "\n".join(cmd))
    subprocess.run(cmd, check=True)
    return dst_path

# ========= Postagem =========
HASHTAGS_TOP_N = 3
def _normalize_hashtags(hashtags, k: int = HASHTAGS_TOP_N) -> List[str]:
    out, seen = [], set()
    def _push(h: str):
        h = (h or "").strip()
        if not h: return
        if not h.startswith("#"):
            h = "#" + re.sub(r"^\W+", "", h)
        if h not in seen:
            seen.add(h); out.append(h)
    if isinstance(hashtags, str):
        found = re.findall(r"#\w+", hashtags)
        tokens = found if found else re.split(r"\s+", hashtags.strip())
        for t in tokens: _push(t)
    else:
        try:
            for t in hashtags: _push(str(t))
        except Exception:
            pass
    return out[:k]

def _postar_video(final_video: str, idioma: str):
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
            idioma=idioma
        )
    except Exception as e:
        logger.exception("❌ Falha ao postar no TikTok: %s", e)
    print("✅ Postado com sucesso (Veo3)." if ok else "⚠️ Upload não confirmado (Veo3). Verifique os logs.")

# ========= Reutilização (inclui EXPORT sem postar) =========
_VID_RE = re.compile(r"^veo3_(?:test_)?(?P<slug>.+?)(?:_c(?P<idx>\d+)|_final.*)?\.mp4$", re.IGNORECASE)

def _listar_slugs() -> Dict[str, Dict[str, List[str]]]:
    items: Dict[str, Dict[str, List[str]]] = {}
    try:
        for name in os.listdir(VIDEOS_DIR):
            if not name.lower().endswith(".mp4"):
                continue
            m = _VID_RE.match(name)
            if not m:
                continue
            slug = m.group("slug"); idx = m.group("idx")
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
    for slug, d in items.items():
        for k in ("cenas", "final", "test"):
            d[k].sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return items

def _menu_reutilizar_videos(idioma: str) -> None:
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
    op = input("Número (Enter p/ cancelar): ").strip()
    if not op.isdigit() or not (1 <= int(op) <= len(ordered)):
        print("Cancelado.")
        return
    slug, d = ordered[int(op) - 1]

    if d["cenas"]:
        print("\nComo deseja proceder?")
        print("1. Juntar todas as cenas (cN) e POSTAR")
        print("2. POSTAR um arquivo específico")
        print("3. Juntar todas as cenas (cN) e **EXPORTAR** (sem postar)")
        modo = input("Escolha 1, 2 ou 3: ").strip()
    else:
        print("\nComo deseja proceder?")
        print("1. POSTAR um arquivo específico")
        print("2. **EXPORTAR** um arquivo específico (sem postar)")
        modo = input("Escolha 1 ou 2: ").strip()
        modo = {"1": "2", "2": "4"}.get(modo, "2")

    if modo == "1" or modo == "3":
        if not d["cenas"]:
            print("Não há cenas para juntar.")
            return
        mp4s = sorted(d["cenas"], key=lambda p: int(_VID_RE.match(os.path.basename(p)).group("idx")))
        trilha = _pick_bgm_path()
        sufixo = "final" if modo == "1" else "final_export"
        saida  = os.path.join(VIDEOS_DIR, f"veo3_{slug}_{sufixo}.mp4")
        try:
            final_video = _stitch_and_bgm_ffmpeg(mp4s, saida, trilha, bgm_db=BG_MIX_DB)
        except Exception as e:
            logger.warning("Falha no stitch/BGM: %s", e)
            final_video = mp4s[0]
            if modo == "3":
                saida = os.path.join(VIDEOS_DIR, f"veo3_{slug}_{sufixo}_fallback.mp4")
                try:
                    final_video = _stitch_and_bgm_ffmpeg([final_video], saida, trilha, bgm_db=BG_MIX_DB)
                except Exception as e2:
                    logger.warning("Falha export fallback: %s", e2)
                    final_video = mp4s[0]

        # Pós-zoom (opcional)
        if POST_ZOOM > 1.0001:
            z_out = os.path.splitext(final_video)[0] + f"_z{POST_ZOOM:.2f}.mp4"
            try:
                final_video = _post_zoom_ffmpeg(final_video, z_out, POST_ZOOM)
            except Exception as e:
                logger.warning("Falha no pós-zoom (%.2f): %s (seguirei sem)", POST_ZOOM, e)

        if modo == "3":
            print(f"✅ Exportado (sem postar): {final_video}")
            return
        _postar_video(final_video, idioma)
        return

    todos = d["final"] + d["test"] + d["cenas"]
    if not todos:
        print("Nenhum arquivo disponível.")
        return
    print("\nArquivos disponíveis:")
    for i, p in enumerate(todos, 1):
        print(f"{i}. {os.path.basename(p)}")
    op2 = input("Arquivo (número): ").strip()
    if not op2.isdigit() or not (1 <= int(op2) <= len(todos)):
        print("Cancelado.")
        return
    escolhido = todos[int(op2) - 1]

    if modo == "2":
        _postar_video(escolhido, idioma)
        return

    # modo == "4": exportar específico (sem postar)
    trilha = _pick_bgm_path()
    saida  = os.path.join(VIDEOS_DIR, f"{os.path.splitext(os.path.basename(escolhido))[0]}_export.mp4")
    try:
        exportado = _stitch_and_bgm_ffmpeg([escolhido], saida, trilha, bgm_db=BG_MIX_DB)
        if POST_ZOOM > 1.0001:
            z_out = os.path.splitext(exportado)[0] + f"_z{POST_ZOOM:.2f}.mp4"
            exportado = _post_zoom_ffmpeg(exportado, z_out, POST_ZOOM)
        print(f"✅ Exportado (sem postar): {exportado}")
    except Exception as e:
        logger.warning("Falha ao exportar: %s", e)

# ========= Geração de vídeo (Flow/API) =========
def _person_generation_for(idioma: str, persona: str) -> str:
    idioma  = (idioma or "").lower()
    persona = (persona or "").lower()
    if idioma.startswith("ar") or persona == "yasmina":
        return os.getenv("VEO3_PERSON_GENERATION_MENA", "allow_adult")
    return os.getenv("VEO3_PERSON_GENERATION_DEFAULT", "allow_all")

def _veo3_generate_single_api(prompt_text: str, out_path: str, idioma: str, persona: str) -> str:
    if not _HAVE_GENAI:
        # fallback dev
        subprocess.run([
            _ffmpeg_or_die(), "-y",
            "-f", "lavfi", "-i", "color=c=black:s=1280x720:d=8",
            "-r", "24", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            out_path
        ], check=True)
        return out_path

    client = _get_client()
    person_gen = _person_generation_for(idioma, persona)
    logger.info("📋 Prompt VEO3 (API):\n%s", prompt_text)
    try:
        op = client.models.generate_videos(
            model=VEO3_MODEL,
            prompt=prompt_text,
            config=types.GenerateVideosConfig(
                aspect_ratio="16:9",
                negative_prompt=NEGATIVE_PROMPT,
                person_generation=person_gen,
            ),
        )
    except Exception as e:
        logger.debug("generate_videos sem config (fallback): %s", e)
        op = client.models.generate_videos(model=VEO3_MODEL, prompt=prompt_text)

    while not op.done:
        print("Aguardando Veo3…")
        time.sleep(8)
        op = client.operations.get(op)

    video = op.response.generated_videos[0]
    client.files.download(file=video.video)
    video.video.save(out_path)
    return out_path

def _veo3_generate_batch(prompts: List[str], out_paths: List[str], idioma: str, persona: str) -> List[str]:
    """
    Gera uma lista de cenas (prompts -> out_paths).
    - Flow: tenta generate_many_via_flow (mesma sessão, baixa e apaga entre cenas).
            Fallback: generate_single_via_flow por cena.
    - API : loop individual.
    """
    assert len(prompts) == len(out_paths) and prompts, "prompts/out_paths inconsistentes"

    if USE_BACKEND == "flow":
        if not FLOW_PROJECT_URL:
            raise RuntimeError("Defina FLOW_PROJECT_URL no .env para usar o backend 'flow'.")
        if not os.path.isfile(FLOW_COOKIES_FILE):
            raise RuntimeError(f"Arquivo de cookies do Flow não encontrado: {FLOW_COOKIES_FILE}")
        if not _HAVE_FLOW:
            raise RuntimeError("utils.veo3_flow não disponível.")

        if _HAVE_FLOW_MANY:
            logger.info("🌐 Flow backend: generate_many_via_flow (%d cenas)…", len(prompts))
            try:
                return generate_many_via_flow(
                    prompts=prompts,
                    out_paths=out_paths,
                    project_url=FLOW_PROJECT_URL,
                    cookies_file=FLOW_COOKIES_FILE,
                    headless=VEO3_HEADLESS,
                    timeout_sec=int(float(os.getenv('FLOW_TIMEOUT_SEC','600'))),
                )
            except Exception as e:
                logger.warning("generate_many_via_flow falhou (%s); farei single por cena.", e)

        # fallback: single por cena
        logger.info("🌐 Flow backend: generate_single_via_flow por cena…")
        done = []
        for ptxt, outp in zip(prompts, out_paths):
            path = generate_single_via_flow(
                prompt_text=ptxt,
                out_path=outp,
                project_url=FLOW_PROJECT_URL,
                cookies_file=FLOW_COOKIES_FILE,
                headless=VEO3_HEADLESS,
                timeout_sec=int(float(os.getenv('FLOW_TIMEOUT_SEC','600'))),
            )
            done.append(path)
        return done

    # API
    logger.info("🧠 API backend: gerando %d cena(s)…", len(prompts))
    done = []
    for ptxt, outp in zip(prompts, out_paths):
        done.append(_veo3_generate_single_api(ptxt, outp, idioma=idioma, persona=persona))
    return done

# ========= Fluxo interativo =========
def _sugerir_assuntos(persona: str) -> List[str]:
    return ["Prosperidade", "Signos", "Sorte do dia", "Amor", "Proteção", "Limpeza energética"]

def _selecionar_assunto(persona: str) -> str:
    print("\nAssunto do vídeo (escolha uma opção ou '0' para digitar):")
    exemplos = _sugerir_assuntos(persona)
    for i, e in enumerate(exemplos, 1):
        print(f"{i}. {e}")
    print("0. Outro (digitar manualmente)")
    op = input("Opção: ").strip()
    if op == "0":
        tema = input("Digite o tema (curto): ").strip()
        return tema if tema else "Prosperidade"
    if op.isdigit() and 1 <= int(op) <= len(exemplos):
        return exemplos[int(op) - 1]
    return "Prosperidade"

def _selecionar_qtd_cenas() -> int:
    print("\nQuantas cenas deseja gerar? (cada cena ≈ 8s)")
    print("Sugerido: 2")
    raw = input("Digite 1–4 (Enter=2): ").strip()
    if raw == "":
        return 2
    try:
        n = int(raw)
        return 1 if n < 1 else (4 if n > 4 else n)
    except Exception:
        return 2

def _selecionar_variacao() -> str:
    print("\nVariação de cenário/roupa:")
    print("1. Manter cenários e roupas padrão")
    print("2. Trocar cenário e manter roupa")
    print("3. Trocar roupa (cor/estilo) e manter cenário")
    print("4. Trocar roupa e trocar cenário")
    print("5. Aleatório")
    op = input("Escolha 1, 2, 3, 4 ou 5: ").strip()
    mapping = {"1":"keep_all","2":"change_bg","3":"change_wardrobe","4":"change_both","5":"random"}
    mode = mapping.get(op, "keep_all")
    if mode == "random":
        mode = random.choice(["keep_all","change_bg","change_wardrobe","change_both"])
        print(f"(Aleatório) Selecionado: {mode.replace('_',' ')}")
    return mode

def _save_text(path: str, text: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

def _preview_and_confirm_prompts(slug: str, prompts: List[str]) -> bool:
    print("\n================= PRÉVIA DE PROMPTS (todas as cenas) =================")
    for i, ptxt in enumerate(prompts, 1):
        prompt_file = os.path.join(PROMPTS_DIR, f"veo3_{slug}_c{i}.prompt.txt")
        _save_text(prompt_file, ptxt)
        print(f"\n--- CENA {i} ---\n{ptxt}\n(Salvo em: {prompt_file})")
    print("\n======================================================================")
    return input("Aprovar estes prompts e continuar? (s/N): ").strip().lower() == "s"

def _submenu_acao() -> str:
    print("\nO que deseja fazer agora?")
    print("1. Gerar APENAS para testes (não postar)")   # gera as cenas e para
    print("2. Gerar e POSTAR")                          # gera, junta, (pós-zoom), posta
    print("3. REUTILIZAR vídeo(s) existente(s)")        # menu de reutilização
    print("4. Gerar e **EXPORTAR** (sem postar)")       # gera, junta, (pós-zoom), NÃO posta
    op = input("Digite 1, 2, 3 ou 4: ").strip()
    return op if op in {"1","2","3","4"} else "1"

def executar_interativo(persona: str, idioma: str) -> None:
    persona = (persona or "luisa").lower()
    idioma  = (idioma  or "pt-br").lower()
    while True:
        tema     = _selecionar_assunto(persona)
        n_cenas  = _selecionar_qtd_cenas()
        variacao = _selecionar_variacao()
        escolha  = _submenu_acao()

        if escolha == "3":
            _menu_reutilizar_videos(idioma)
            return

        slug = re.sub(r"[^a-z0-9]+", "-", f"{persona}-{tema.lower()}").strip("-")

        # === PROMPTS COMPLETOS PELO GEMINI (sem pós-processar) ===
        try:
            prompts = _ask_gemini_prompts_full(persona, idioma, tema, n_cenas, variacao)
        except Exception as e:
            logger.exception("Falha ao obter prompts completos do Gemini: %s", e)
            print("❌ Não foi possível obter prompts completos do Gemini. Tente novamente.")
            return

        if not _preview_and_confirm_prompts(slug, prompts):
            print("❌ Prompts reprovados. Voltando ao menu do assunto para gerar outro conjunto.")
            continue

        # === GERA TODAS AS CENAS (Flow many / API loop) ===
        out_paths = [os.path.join(VIDEOS_DIR, f"veo3_{slug}_c{i+1}.mp4") for i in range(len(prompts))]
        logger.info("🎬 Gerando %d cena(s)…", len(prompts))
        mp4s = _veo3_generate_batch(prompts, out_paths, idioma=idioma, persona=persona)

        if escolha == "1":
            print("\n✅ Cenas de teste geradas:")
            for p in mp4s:
                print(" -", p)
            print("Use 'Reutilizar vídeo(s) existente(s)' ou rode novamente para juntar/postar/exportar.")
            return

        trilha = _pick_bgm_path()
        saida  = os.path.join(VIDEOS_DIR, f"veo3_{slug}_final.mp4")
        try:
            final_video = _stitch_and_bgm_ffmpeg(mp4s, saida, trilha, bgm_db=BG_MIX_DB)
        except Exception as e:
            logger.warning("Falha ao juntar/BGM (%s). Usarei a primeira cena.", e)
            final_video = mp4s[0]

        # Pós-zoom (opcional)
        if POST_ZOOM > 1.0001:
            z_out = os.path.splitext(final_video)[0] + f"_z{POST_ZOOM:.2f}.mp4"
            try:
                final_video = _post_zoom_ffmpeg(final_video, z_out, POST_ZOOM)
            except Exception as e:
                logger.warning("Falha no pós-zoom (%.2f): %s (seguirei sem)", POST_ZOOM, e)

        if escolha == "4":
            print(f"✅ Exportado (sem postar): {final_video}")
            return

        _postar_video(final_video, idioma)
        return

# ========= Automático =========
def postar_em_intervalo(persona: str, idioma: str, cada_horas: float) -> None:
    persona = (persona or "luisa").lower()
    idioma  = (idioma  or "pt-br").lower()
    base_temas = _sugerir_assuntos(persona)
    intervalo  = float(cada_horas) * 3600.0
    try:
        while True:
            inicio = datetime.now()
            tema   = random.choice(base_temas)
            print(f"\n🟢 Veo3 automático — {inicio:%d/%m %H:%M} | persona={persona} | tema={tema}")
            try:
                # duas cenas por padrão no automático
                prompts = _ask_gemini_prompts_full(persona, idioma, tema, n=2, variation_mode="keep_all")
                slug    = re.sub(r"[^a-z0-9]+", "-", f"{persona}-{tema.lower()}").strip("-")
                for i, ptxt in enumerate(prompts, 1):
                    _save_text(os.path.join(PROMPTS_DIR, f"veo3_{slug}_c{i}.prompt.txt"), ptxt)

                mp4s = _veo3_generate_batch(
                    prompts=prompts,
                    out_paths=[os.path.join(VIDEOS_DIR, f"veo3_{slug}_c{i+1}.mp4") for i in range(len(prompts))],
                    idioma=idioma,
                    persona=persona
                )
                trilha = _pick_bgm_path()
                saida  = os.path.join(VIDEOS_DIR, f"veo3_{slug}_final.mp4")
                try:
                    final_video = _stitch_and_bgm_ffmpeg(mp4s, saida, trilha, bgm_db=BG_MIX_DB)
                except Exception as e:
                    logger.warning("Falha stitch/BGM (%s). Publicarei a primeira cena.", e)
                    final_video = mp4s[0]
                if POST_ZOOM > 1.0001:
                    z_out = os.path.splitext(final_video)[0] + f"_z{POST_ZOOM:.2f}.mp4"
                    try:
                        final_video = _post_zoom_ffmpeg(final_video, z_out, POST_ZOOM)
                    except Exception as e:
                        logger.warning("Falha no pós-zoom auto (%.2f): %s (seguirei sem)", POST_ZOOM, e)
                _postar_video(final_video, idioma)
            except Exception as e:
                logger.exception("Falha no ciclo Veo3: %s", e)
            proxima = inicio + timedelta(seconds=intervalo)
            while True:
                now = datetime.now()
                rem = (proxima - now).total_seconds()
                if rem <= 0:
                    break
                time.sleep(min(30.0, rem))
    except KeyboardInterrupt:
        print("🛑 Veo3 automático interrompido.")
