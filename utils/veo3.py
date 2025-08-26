# utils/veo3.py
# Veo3 (Flow via Selenium OU Gemini API) + FFmpeg
# - Gera 1..N cenas; no Flow usa generate_many_via_flow (mesma sessão, baixa e exclui cada vídeo)
# - Normaliza cada cena para 9:16 (1080x1920) via scale+pad (SEM zoom)
# - Junta cenas (concat) + BGM opcional (com Audio Ducking); aplica PÓS-ZOOM opcional no final
# - Menus com "b/voltar": testar, gerar+postar, reutilizar, **exportar (sem postar)** e auto-config do modo automático
from __future__ import annotations
import os, re, json, time, shutil, random, logging, subprocess
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

D_MIN = int(os.getenv("VEO3_DIALOGUE_MIN_WORDS", "18"))
D_MAX = int(os.getenv("VEO3_DIALOGUE_MAX_WORDS", "25"))
HASHTAGS_TOP_N = 3

NEGATIVE_PROMPT = (
    "cartoon, drawing, low quality, branding, readable text, fantasy effects, "
    "facial change, outfit inconsistency, visual distortion, watermark, logo"
)

USE_BACKEND = os.getenv("VEO3_BACKEND", "flow").strip().lower()
FLOW_PROJECT_URL  = os.getenv("FLOW_PROJECT_URL", "").strip()
FLOW_COOKIES_FILE = os.getenv("FLOW_COOKIES_FILE", "cookies_veo3.txt").strip()
VEO3_HEADLESS     = os.getenv("VEO3_CHROME_HEADLESS", "1").strip() != "0"

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

# ------------------------ Gemini (estágio 1) ------------------------
def _ask_gemini_viral_analysis(tema: str, persona: str, idioma: str) -> Dict[str, str]:
    logger.info("🧠 Estágio 1: Analisando tendências e definindo direção de cena para o tema '%s'...", tema)
    client = _get_client()
    data_hoje = datetime.now().strftime("%Y-%m-%d")
    target_lang_map = {"pt": "Brazilian Portuguese", "ar": "Arabic", "en": "English"}
    target_lang_name = target_lang_map.get(idioma.lower()[:2], "English")

    try:
        prompt_template = _load_prompt_template("stage_1_analysis")
        cmd = prompt_template.format(
            data_hoje=data_hoje, tema=tema, persona=persona, target_lang_name=target_lang_name
        )

        # LOG p/ debug
        print("\n================ COMANDO ENVIADO AO GEMINI (ANÁLISE) ================\n")
        print(cmd)
        print("\n====================================================================\n")

        cfg = types.GenerateContentConfig(response_mime_type="application/json")
        resp = client.models.generate_content(model=TEXT_MODEL, contents=cmd, config=cfg)
        analysis = json.loads(resp.text or "{}")
        required_keys = [
            "viral_angle", "keywords", "tone_of_voice", "visual_style",
            "cinematography_suggestions", "character_action", "hook_style", "cta_style"
        ]
        if all(key in analysis for key in required_keys):
            logger.info("✅ Direção de cena definida com sucesso.")
            print("\n================ MANUAL DE DIREÇÃO (Estágio 1) ================\n")
            print(json.dumps(analysis, indent=2, ensure_ascii=False))
            print("\n==============================================================\n")
            return analysis
        else:
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
        return "VARY both wardrobe (color/style) AND background between scenes, coherent with the character."
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
    target_lang_map = {"pt": "Brazilian Portuguese (pt-BR)", "ar": "Egyptian Arabic", "en": "English"}
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

def _ask_gemini_scene_prompts(
    persona: str, idioma: str, tema: str, n: int, variation_mode: str, viral_context: Dict[str, str]
) -> List[str]:
    client = _get_client()
    cmd = _build_gemini_command_full(persona, idioma, tema, n, variation_mode, viral_context)
    logger.info("🎬 Estágio 2: Gerando prompts de cena com base no manual de direção...")
    print("\n================ COMANDO ENVIADO AO GEMINI (CENAS) ================\n")
    print(cmd)
    print("\n===================================================================\n")
    cfg = types.GenerateContentConfig(response_mime_type="application/json")
    resp = client.models.generate_content(model=TEXT_MODEL, contents=cmd, config=cfg)
    return _parse_prompts_from_gemini_json(resp.text or "", n)

# ------------------------ FFmpeg helpers ------------------------
def _filter_for_input_nozoom(i: int) -> str:
    return (f"[{i}:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
            f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2,fps={FPS_OUT},format=yuv420p,setsar=1/1[v{i}];"
            f"[{i}:a]aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={AUDIO_SR}[a{i}];")

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
        parts.append(f"[{bgm_idx}:a]aformat=sample_fmts=s16:channel_layouts=stereo:sample_rates={AUDIO_SR},"
                     f"aloop=loop=-1:size=2e9,aresample={AUDIO_SR}[bg_looped];")
        parts.append(f"[bg_looped][acat]sidechaincompress=threshold=0.06:ratio=8:attack=100:release=500:detection=rms[bg_ducked];")
        vol = 10 ** (bgm_db / 20.0)
        parts.append(f"[acat][bg_ducked]amix=inputs=2:duration=first:dropout_transition=0:weights='1 {vol}'[aout]")
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
    filter_v = (f"[0:v]scale=iw*{zoom}:ih*{zoom},crop={OUT_W}:{OUT_H}:(iw-{OUT_W})/2:(ih-{OUT_H})/2,"
                f"fps={FPS_OUT},format=yuv420p,setsar=1/1[v]")
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

# ------------------------ TikTok posting ------------------------
def _normalize_hashtags(hashtags, k: int = HASHTAGS_TOP_N) -> List[str]:
    out, seen = [], set()
    def _push(h: str):
        h = (h or "").strip()
        if not h:
            return
        if not h.startswith("#"):
            h = "#" + re.sub(r"^\W+", "", h)
        if h not in seen:
            seen.add(h)
            out.append(h)
    if isinstance(hashtags, str):
        tokens = re.findall(r"#\w+", hashtags) or re.split(r"\s+", hashtags.strip())
        for t in tokens:
            _push(t)
    else:
        try:
            for t in hashtags:
                _push(str(t))
        except Exception:
            pass
    return out[:k]

def _postar_video(final_video: str, idioma: str):
    """Gera descrição (no IDIOMA certo) e envia para o TikTok."""
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

    print("✅ Postado com sucesso." if ok else "⚠️ Upload não confirmado. Verifique os logs.")

# ------------------------ Utilidades de arquivo ------------------------
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
        _postar_video(final_video, idioma)
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
        _postar_video(escolhido, idioma)
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
    print("0. Outro (digitar manualmente)")
    op = input("Opção: ").strip().lower()
    if op in _BACK_TOKENS:
        return None
    if op == "0":
        tema = input("Digite o tema (curto): ").strip()
        return tema or "Prosperidade"
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

def _save_text(path: str, text: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

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
    print("b. Voltar")
    op = input("Digite 1, 2, 3, 4 ou b: ").strip().lower()
    if op in _BACK_TOKENS:
        return None
    return op if op in {"1", "2", "3", "4"} else "1"

def executar_interativo(persona: str, idioma: str) -> None:
    persona, idioma = (persona or "luisa").lower(), (idioma or "pt-br").lower()

    while True:
        tema = _selecionar_assunto(persona)
        if tema is None:
            return  # voltar ao menu anterior (main)

        n_cenas = _selecionar_qtd_cenas()
        if n_cenas is None:
            continue  # volta para escolher o assunto novamente

        variacao = _selecionar_variacao()
        if variacao is None:
            continue  # volta para escolher o assunto novamente

        escolha = _submenu_acao()
        if escolha is None:
            continue  # volta e recomeça o pequeno fluxo interno

        if escolha == "3":
            _menu_reutilizar_videos(idioma)
            return

        slug = re.sub(r"[^a-z0-9]+", "-", f"{persona}-{tema.lower()}").strip("-")

        try:
            viral_context = _ask_gemini_viral_analysis(tema, persona, idioma)
            prompts = _ask_gemini_scene_prompts(persona, idioma, tema, n_cenas, variacao, viral_context)
        except Exception as e:
            logger.exception("Falha ao obter prompts do Gemini: %s", e)
            print("❌ Não foi possível obter prompts do Gemini. Tente novamente.")
            return

        if not prompts:
            print("❌ Nenhum prompt foi gerado pelo Gemini. Voltando ao menu.")
            continue

        approved = _preview_and_confirm_prompts(slug, prompts)
        if approved is None:
            continue  # usuário escolheu voltar
        if not approved:
            print("❌ Prompts reprovados. Voltando ao menu.")
            continue

        out_paths = [os.path.join(VIDEOS_DIR, f"veo3_{slug}_c{i+1}.mp4") for i in range(len(prompts))]
        logger.info("🎬 Gerando %d cena(s)…", len(prompts))
        mp4s = _veo3_generate_batch(prompts, out_paths, idioma=idioma, persona=persona)
        if not mp4s:
            print("❌ A geração de vídeo falhou. Verifique os logs.")
            return

        if escolha == "1":
            print("\n✅ Cenas de teste geradas:")
            for p in mp4s:
                print(" -", p)
            print("\nUse 'Reutilizar' para juntar/postar/exportar.")
            return

        trilha = _pick_bgm_path()
        saida  = os.path.join(VIDEOS_DIR, f"veo3_{slug}_final.mp4")
        try:
            final_video = _stitch_and_bgm_ffmpeg(mp4s, saida, trilha, bgm_db=BG_MIX_DB)
        except Exception as e:
            logger.warning("Falha ao juntar/BGM (%s). Usarei a primeira cena.", e)
            final_video = mp4s[0]

        if POST_ZOOM > 1.0001:
            z_out = os.path.splitext(final_video)[0] + f"_z{POST_ZOOM:.2f}.mp4"
            try:
                final_video = _post_zoom_ffmpeg(final_video, z_out, POST_ZOOM)
            except Exception as e:
                logger.warning("Falha no pós-zoom (%.2f): %s", POST_ZOOM, e)

        if escolha == "4":
            print(f"✅ Exportado (sem postar): {final_video}")
            return

        _postar_video(final_video, idioma)
        return

# ------------------------ Batch (Flow/API) ------------------------
def _veo3_generate_single_api(prompt_text: str, out_path: str, idioma: str, persona: str) -> str:
    # Placeholder: mantido por compatibilidade; o projeto atual usa o backend Flow.
    # Se você quiser ativar API pura, implemente aqui.
    raise NotImplementedError("Backend API não implementado. Use VEO3_BACKEND=flow.")

def _veo3_generate_batch(prompts: List[str], out_paths: List[str], idioma: str, persona: str) -> List[str]:
    assert len(prompts) == len(out_paths) and prompts, "prompts/out_paths inconsistentes"
    if USE_BACKEND == "flow":
        if not (FLOW_PROJECT_URL and os.path.isfile(FLOW_COOKIES_FILE) and _HAVE_FLOW):
            raise RuntimeError("Backend Flow mal configurado (URL, cookies ou utils/veo3_flow.py).")
        if _HAVE_FLOW_MANY:
            logger.info("🌐 Flow backend: generate_many_via_flow (%d cenas)…", len(prompts))
            try:
                return generate_many_via_flow(
                    prompts=prompts,
                    out_paths=out_paths,
                    project_url=FLOW_PROJECT_URL,
                    cookies_file=FLOW_COOKIES_FILE,
                    headless=VEO3_HEADLESS,
                    timeout_sec=int(float(os.getenv("FLOW_TIMEOUT_SEC", "600"))),
                )
            except Exception as e:
                logger.warning("generate_many_via_flow falhou (%s); farei single por cena.", e)

        logger.info("🌐 Flow backend: generate_single_via_flow por cena…")
        done = []
        for ptxt, outp in zip(prompts, out_paths):
            done.append(generate_single_via_flow(
                prompt_text=ptxt,
                out_path=outp,
                project_url=FLOW_PROJECT_URL,
                cookies_file=FLOW_COOKIES_FILE,
                headless=VEO3_HEADLESS,
                timeout_sec=int(float(os.getenv("FLOW_TIMEOUT_SEC", "600"))),
            ))
        return done

    logger.info("🧠 API backend: gerando %d cena(s)…", len(prompts))
    return [_veo3_generate_single_api(p, o, idioma, persona) for p, o in zip(prompts, out_paths)]

# ------------------------ Automático (com pré-config) ------------------------
def postar_em_intervalo(persona: str, idioma: str, cada_horas: float) -> None:
    """Loop automático com pré-configuração (tema/nº cenas/variação).
       Opções 'aleatório a cada ciclo' estão disponíveis."""
    persona, idioma = (persona or "luisa").lower(), (idioma or "pt-br").lower()

    # Pré-config interativa
    print("\n=== Configuração do Veo3 Automático ===")
    tema_fix = _selecionar_assunto(persona)
    if tema_fix is None:
        print("Cancelado.")
        return
    usar_tema_aleatorio = False
    if tema_fix.strip().lower() in {"random", "aleatorio", "aleatório"}:
        usar_tema_aleatorio = True

    n_cenas = _selecionar_qtd_cenas()
    if n_cenas is None:
        print("Cancelado.")
        return

    variacao_sel = _selecionar_variacao()
    if variacao_sel is None:
        print("Cancelado.")
        return
    variacao_random = (variacao_sel == "random")

    base_temas = _sugerir_assuntos(persona)
    intervalo = float(cada_horas) * 3600.0

    try:
        while True:
            inicio = datetime.now()
            tema = random.choice(base_temas) if usar_tema_aleatorio else tema_fix
            print(f"\n🟢 Veo3 automático — {inicio:%d/%m %H:%M} | persona={persona} | tema={tema}")

            try:
                viral_context = _ask_gemini_viral_analysis(tema, persona, idioma)
                variacao = (random.choice(["keep_all", "change_bg", "change_wardrobe", "change_both"])
                            if variacao_random else variacao_sel)
                prompts = _ask_gemini_scene_prompts(
                    persona, idioma, tema, n=n_cenas, variation_mode=variacao, viral_context=viral_context
                )
                if not prompts:
                    raise RuntimeError(f"Geração de prompts retornou lista vazia para o tema '{tema}'.")
                slug = re.sub(r"[^a-z0-9]+", "-", f"{persona}-{tema.lower()}").strip("-")
                for i, ptxt in enumerate(prompts, 1):
                    _save_text(os.path.join(PROMPTS_DIR, f"veo3_{slug}_c{i}.prompt.txt"), ptxt)

                out_paths = [os.path.join(VIDEOS_DIR, f"veo3_{slug}_c{i+1}.mp4") for i in range(len(prompts))]
                mp4s = _veo3_generate_batch(prompts, out_paths, idioma=idioma, persona=persona)
                if not mp4s:
                    raise RuntimeError("Geração de vídeo batch não retornou arquivos.")

                trilha = _pick_bgm_path()
                saida = os.path.join(VIDEOS_DIR, f"veo3_{slug}_final.mp4")
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
                        logger.warning("Falha no pós-zoom auto (%.2f): %s", POST_ZOOM, e)

                _postar_video(final_video, idioma)

            except Exception as e:
                logger.exception("Falha no ciclo Veo3: %s", e)

            proxima = inicio + timedelta(seconds=intervalo)
            while True:
                rem = (proxima - datetime.now()).total_seconds()
                if rem <= 0:
                    break
                print(f"Próxima execução em {rem/3600.0:.2f} horas...", end="\r")
                time.sleep(min(30.0, rem))
    except KeyboardInterrupt:
        print("\n🛑 Veo3 automático interrompido.")
