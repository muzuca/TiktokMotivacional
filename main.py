# main.py
# CLI com menus em camadas e opção de "b/voltar" em todos os passos.
# Integra com utils/veo3.py (interativo e automático) e mantém os fluxos motivacional/tarot.
# Atualizado para: quando há MOVIMENTO, a frase principal é desenhada no VÍDEO (FFmpeg);
# quando NÃO há movimento, a frase é queimada nas IMAGENS (Pillow) antes do vídeo.

import os
import sys
import time
import logging
from datetime import datetime, timedelta
import random
from concurrent.futures import ProcessPoolExecutor, TimeoutError
import multiprocessing
import atexit
import faulthandler

try:
    import psutil  # opcional
except Exception:
    psutil = None

# === Ajusta CWD ao lado do .exe (quando empacotado) ===
if getattr(sys, 'frozen', False):
    try:
        os.chdir(os.path.dirname(sys.executable))
    except Exception:
        pass

# .env o mais cedo possível
from dotenv import load_dotenv, find_dotenv
ENV_PATH = find_dotenv(usecwd=True)
load_dotenv(ENV_PATH, override=True)

# Utils que dependem do .env
from utils.frase import (
    gerar_prompts_de_imagem_variados,
    gerar_frase_motivacional,
    gerar_slug,
    gerar_hashtags_virais,
)
from utils.imagem import escrever_frase_na_imagem, gerar_imagem_com_frase
try:
    # Tenta importar a função para DALL-E/ChatGPT (automação por navegador).
    from utils.imagem import gerar_imagem_dalle
    _HAVE_DALLE_FUNC = True
except ImportError:
    _HAVE_DALLE_FUNC = False

from utils.video import gerar_video
from utils.tiktok import postar_no_tiktok_e_renomear

# (Tarot – se existir)
try:
    from utils.frase import gerar_prompt_tarot, gerar_frase_tarot_curta
    _HAVE_TAROT_FUNCS = True
except Exception:
    _HAVE_TAROT_FUNCS = False

# (Veo3 – menus internos com suporte a “voltar”)
try:
    from utils.veo3 import executar_interativo as veo3_executar_interativo
    from utils.veo3 import postar_em_intervalo as veo3_postar_em_intervalo
    _HAVE_VEO3 = True
except Exception:
    _HAVE_VEO3 = False

# ====== LOG ======
LOG_LEVEL = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    level=LOG_LEVEL,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ====== Helpers .env ======
def _int_env(name, default):
    try:
        v = os.getenv(name)
        return int(v) if v is not None and str(v).strip() != "" else default
    except Exception:
        return default

def _float_env(name, default):
    try:
        v = os.getenv(name)
        return float(v) if v is not None and str(v).strip() != "" else default
    except Exception:
        return default

ITERATION_TIMEOUT_MIN = float(os.getenv("ITERATION_TIMEOUT_MIN", "12.0"))
HANG_WATCHDOG_SECS    = int(float(os.getenv("HANG_WATCHDOG_SECS", "600")))
HEARTBEAT_MIN         = float(os.getenv("HEARTBEAT_MIN", "10"))
HEARTBEAT_SECS        = HEARTBEAT_MIN * 60.0 if HEARTBEAT_MIN > 0 else 0.0

CHROME_CLEANUP_POLICY = os.getenv("CHROME_CLEANUP_POLICY", "drivers_only").strip().lower()
CHROME_KILL_MATCH     = os.getenv("CHROME_KILL_MATCH", "").strip()
RUN_INLINE_WHEN_FROZEN = os.getenv("RUN_INLINE_WHEN_FROZEN", "1").strip() != "0"

os.makedirs("cache", exist_ok=True)
_watchdog_log_path = os.path.join("cache", "hang_watchdog.log")
try:
    _watchdog_f = open(_watchdog_log_path, "w", buffering=1, encoding="utf-8")
    faulthandler.enable(_watchdog_f)
    if HANG_WATCHDOG_SECS > 0:
        faulthandler.dump_traceback_later(HANG_WATCHDOG_SECS, repeat=True, file=_watchdog_f)
        atexit.register(faulthandler.cancel_dump_traceback_later)
except Exception as _e:
    logger.debug("Watchdog desativado: %s", _e)

# ====== Opções visuais ======
STYLE_OPTIONS = {
    "1": ("classic", "Clássico legível (Montserrat/Inter, branco + stroke)"),
    "2": ("modern",  "Modernão (Bebas/Alta, caps, discreto)"),
    "3": ("serif",   "Elegante (Playfair/Cinzel, leve dourado)"),
    "4": ("mono",    "Monoespaçada minimalista"),
    "5": ("clean",   "Clean sem stroke (peso médio)"),
}
MOTION_OPTIONS = {
    "1": ("none",          "Sem movimento"),
    "2": ("kenburns_in",   "Zoom in (Ken Burns)"),
    "3": ("kenburns_out",  "Zoom out (Ken Burns)"),
    "4": ("pan_lr",        "Pan left→right"),
    "5": ("pan_ud",        "Pan up→down"),
    "0": ("random",        "Aleatório (entre movimentos)"),
}
DEFAULT_SLIDES_COUNT = int(os.getenv("SLIDES_COUNT", "4"))
IMAGENS_DIR = "imagens"
VIDEOS_DIR = "videos"
AUDIOS_DIR = "audios"
MAX_RETRIES = 3  # tentativas para gerar cada imagem

# --------------------- limpeza ---------------------
def _safe_unlink(path):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except Exception as e:
        logger.debug("não consegui remover '%s': %s", path, e)

# --------------------- robustez browsers ---------------------
def _cleanup_browsers(policy=None):
    if not psutil:
        return
    policy = (policy or CHROME_CLEANUP_POLICY).strip().lower()
    if policy not in {"none", "drivers_only", "children", "match", "all"}:
        policy = "drivers_only"
    killed = 0
    for p in psutil.process_iter(attrs=["pid", "name", "cmdline", "ppid"]):
        try:
            name = (p.info.get("name") or "").lower()
            if "chromedriver" in name:
                if policy in {"drivers_only", "children", "match", "all"}:
                    p.kill(); killed += 1
                continue
            if "chrome" == name or name.startswith("chrome"):
                if policy in {"none", "drivers_only"}:
                    continue
                if policy == "all":
                    p.kill(); killed += 1; continue
                if policy == "children":
                    try:
                        parents = p.parents()
                    except Exception:
                        parents = []
                    if any("chromedriver" in (pp.name() or "").lower() for pp in parents):
                        p.kill(); killed += 1
                    continue
                if policy == "match" and CHROME_KILL_MATCH:
                    cmd = " ".join(p.info.get("cmdline") or [])
                    if CHROME_KILL_MATCH.lower() in cmd.lower():
                        p.kill(); killed += 1
                continue
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue
    if killed:
        logger.info("🧹 Cleanup browsers (%s): %d processo(s) finalizado(s).", policy, killed)

# --------------------- retry de postagem ---------------------
def _postar_com_retry(video_final: str, idioma: str, desc_tiktok: str) -> bool:
    delay_min   = _int_env("ONEOFF_RETRY_MINUTES", 10)
    max_retries = _int_env("ONEOFF_RETRY_MAX", 1)

    tentativa = 0
    while True:
        tentativa += 1
        ok = False
        try:
            ok = postar_no_tiktok_e_renomear(
                descricao_personalizada=desc_tiktok,
                video_final=video_final,
                idioma=idioma
            )
        except Exception as e:
            logger.exception("❌ Exceção ao postar no TikTok (tentativa %d): %s", tentativa, e)
            ok = False

        if ok:
            logger.info("✅ Post confirmado no TikTok (tentativa %d).", tentativa)
            return True
        
        if tentativa > max_retries:
            logger.error("❌ Falha em todas as tentativas de postagem (%d).", tentativa)
            return False

        logger.warning("⚠️ Post falhou (tentativa %d). Tentarei de novo em %d min...", tentativa, delay_min)
        try:
            _cleanup_browsers(policy="children")
        except Exception:
            pass
        time.sleep(max(1, int(delay_min) * 60))

def _run_rotina_once(args_tuple):
    try:
        _cleanup_browsers()
        modo_conteudo, idioma, tts_engine, legendas, video_style, motion, slides_count, image_engine = args_tuple
        rotina(modo_conteudo, idioma, tts_engine, legendas, video_style, motion, slides_count, image_engine)
        return True
    except Exception as e:
        logging.exception("Falha na execução única: %s", e)
        return False
    finally:
        _cleanup_browsers()

def _executar_com_timeout(args_tuple):
    if getattr(sys, 'frozen', False) and RUN_INLINE_WHEN_FROZEN:
        start = time.time()
        try:
            ok = _run_rotina_once(args_tuple)
            return bool(ok)
        finally:
            dur = time.time() - start
            logging.info("⏲️ Duração do ciclo (inline/frozen): %.1fs", dur)

    ctx = multiprocessing.get_context("spawn")
    start = time.time()
    _cleanup_browsers()
    with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as ex:
        fut = ex.submit(_run_rotina_once, args_tuple)
        try:
            ok = fut.result(timeout=int(ITERATION_TIMEOUT_MIN * 60))
            return bool(ok)
        except TimeoutError:
            logging.error("⏱️ Iteração excedeu %.1f min — abortando e limpando (policy=%s).",
                          ITERATION_TIMEOUT_MIN, CHROME_CLEANUP_POLICY)
            _cleanup_browsers()
            return False
        finally:
            dur = time.time() - start
            logging.info("⏲️ Duração do ciclo: %.1fs", dur)

# ========================= JITTER “HUMANO” =========================
def _human_next_interval_seconds(base_hours: float) -> int:
    base = max(0.25, float(base_hours)) * 3600.0
    # ±10% + jitter 0–6 min
    swing = base * random.uniform(-0.10, 0.10)
    jitter = random.randint(0, 360)
    return max(300, int(base + swing + jitter))

def _maybe_sleep_start_jitter():
    # pequeno atraso aleatório 0–15s para parecer mais humano
    delay = random.uniform(0.0, 15.0)
    if delay > 0.2:
        time.sleep(delay)

# ========================= UI (menus com VOLTAR) =========================
_BACK_TOKENS = {"b", "voltar", "back"}

def _menu_modo_execucao():
    print("\nO que você deseja fazer?")
    print("1. Postar agora (uma vez) *padrão")
    print("2. Postar automaticamente a cada X horas")
    op = input("Escolha 1 ou 2: ").strip().lower()
    if op in _BACK_TOKENS: return None
    return "2" if op == "2" else "1"

def _selecionar_idioma():
    print("\nEscolha o país para referência da língua das mensagens:")
    print("1. EUA (Inglês) *padrão")
    print("2. Brasil (pt-br)")
    print("3. Árabe (egípcio)")
    print("b. Voltar")
    op = input("Digite a opção: ").strip().lower()
    if op in _BACK_TOKENS: return None
    return {"2":"pt-br", "3":"ar-eg"}.get(op, "en")

def _submenu_conteudo_por_idioma(idioma):
    if idioma.startswith("pt"):
        print("\nSelecione o conteúdo (Brasil):")
        print("1. Motivacional")
        print("2. Cartomante")
        print("3. Veo3 (Luisa)")
        print("b. Voltar")
        op = input("Digite a opção: ").strip().lower()
        if op in _BACK_TOKENS: return None
        if op == "2": return ("tarot", "luisa")
        if op == "3": return ("veo3", "luisa")
        return ("motivacional", "default")
    else:
        label = "EUA" if idioma.startswith("en") else "EG"
        print(f"\nSelecione o conteúdo ({label}):")
        print("1. Motivacional")
        print("2. Cartomante")
        print("b. Voltar")
        op = input("Digite a opção: ").strip().lower()
        if op in _BACK_TOKENS: return None
        if op == "2": return ("tarot", "default")
        return ("motivacional", "default")

def _ler_intervalo_horas():
    raw = input("\nA cada quantas horas deseja postar? (ex.: 4.5) | b=voltar\n> ").strip().lower()
    if raw in _BACK_TOKENS: return None
    try:
        v = float(raw.replace(",", "."))
        return max(0.25, v)
    except Exception:
        return 4.0

def _selecionar_tts_engine():
    print("\nEscolha o mecanismo de voz (TTS):")
    print("1. Gemini (padrão)")
    print("2. ElevenLabs")
    print("b. Voltar")
    op = input("Escolha: ").strip().lower()
    if op in _BACK_TOKENS: return None
    return "elevenlabs" if op == "2" else "gemini"

def _selecionar_legendas():
    print("\nDeseja adicionar legendas sincronizadas no vídeo?")
    print("1. Sim *padrão")
    print("2. Não")
    print("b. Voltar")
    op = input("Escolha: ").strip().lower()
    if op in _BACK_TOKENS: return None
    return (op != "2")

def _selecionar_estilo_video():
    print("\nEscolha o estilo do vídeo (legendas/typography):")
    print("0. Aleatório *padrão")
    for k,(key,label) in STYLE_OPTIONS.items():
        print(f"{k}. {label}")
    print("b. Voltar")
    op = input("Opção: ").strip().lower()
    if op in _BACK_TOKENS: return None
    if op == "0" or op == "":
        choice = random.choice(list(STYLE_OPTIONS.values()))[0]
        print(f"Estilo sorteado: {choice}")
        return choice
    return STYLE_OPTIONS.get(op, ("modern",""))[0]

def _selecionar_motion(env_default):
    env_default = (env_default or "none").lower()
    label_default = f"'{env_default}'"
    print("\nMovimento do vídeo:")
    print(f"(Enter para manter do .env: {label_default})")
    print("1. Sem movimento")
    print("2. Zoom in (Ken Burns)")
    print("3. Zoom out (Ken Burns)")
    print("4. Pan left→right")
    print("5. Pan up→down")
    print("0. Aleatório (entre movimentos)")
    print("b. Voltar")
    op = input("Opção: ").strip().lower()
    if op in _BACK_TOKENS: return None
    if op == "":
        return env_default
    opt = MOTION_OPTIONS.get(op)
    if not opt:
        return env_default
    if opt[0] == "random":
        choice = random.choice([v[0] for k,v in MOTION_OPTIONS.items() if v[0] != "random"])
        return choice
    return opt[0]

def _selecionar_qtd_fotos(padrao):
    print("\nQuantidade de fotos no vídeo (inclui a capa).")
    print(f"Enter = usar .env (SLIDES_COUNT={padrao}) | b = voltar")
    raw = input("Digite 1..10: ").strip().lower()
    if raw in _BACK_TOKENS: return None
    if raw == "": return int(padrao)
    try:
        v = int(raw); return max(1, min(10, v))
    except Exception:
        return int(padrao)

def _selecionar_gerador_imagens(padrao):
    padrao = (padrao or "pexels").lower()
    print("\nSelecione qual o mecanismo de geração de imagens:")
    print("1. Pexels (Fotos Reais)")
    print("2. DALL-E / ChatGPT (Geradas por IA)")
    print(f"Enter = usar .env (IMAGE_MODE={padrao}) | b = voltar")
    raw = input("Digite 1 ou 2: ").strip().lower()
    if raw in _BACK_TOKENS: return None
    if raw == "":
        return "chatgpt" if padrao in ("chatgpt","dalle") else "pexels"
    return "chatgpt" if raw == "2" else "pexels"

def _map_video_style_to_image_template(style_key):
    s = (style_key or "").lower()
    if s in ("classic", "1"): return "classic_serif"
    if s in ("clean", "5"):   return "modern_block"
    if s in ("serif", "3"):   return "classic_serif"
    if s in ("mono", "4"):    return "minimal_center"
    if s in ("modern", "2"):  return "modern_block"
    return "minimal_center"

def rotina(modo_conteudo, idioma, tts_engine, legendas, video_style, motion, slides_count, image_engine):
    os.makedirs(IMAGENS_DIR, exist_ok=True)
    os.makedirs(VIDEOS_DIR, exist_ok=True)
    os.makedirs(AUDIOS_DIR, exist_ok=True)

    logger.info("Gerando conteúdos (%s | modo=%s | imagens=%s)...", idioma, modo_conteudo, image_engine)

    # Tema + frase curta (para título e descrição)
    if modo_conteudo == "tarot" and _HAVE_TAROT_FUNCS:
        tema_imagem = gerar_prompt_tarot(idioma)
        frase = gerar_frase_tarot_curta(idioma)
    else:
        tema_imagem = gerar_frase_motivacional(idioma)
        frase = tema_imagem

    # Hashtags e descrição
    try:
        hashtags_list = gerar_hashtags_virais(frase, idioma=idioma, n=3)
        desc_tiktok = (frase + " " + " ".join(hashtags_list)).strip()
    except Exception as e:
        logger.warning("Falha ao gerar hashtags (seguirei sem): %s", e)
        desc_tiktok = frase

    slug_frase = gerar_slug(frase)
    video_final = os.path.join(VIDEOS_DIR, f"{slug_frase}.mp4")

    logger.info("Gerando %d prompts de imagem com o tema: '%s'", slides_count, tema_imagem)
    prompts_de_imagem = gerar_prompts_de_imagem_variados(tema_imagem, slides_count, idioma)

    generated_image_paths = []
    # 'chatgpt' ou 'dalle' vão usar a mesma função (automação via navegador)
    usar_dalle = image_engine in ('chatgpt', 'dalle')
    gerador_func = gerar_imagem_dalle if usar_dalle and _HAVE_DALLE_FUNC else gerar_imagem_com_frase
    
    for i, img_prompt in enumerate(prompts_de_imagem):
        imagem_path = os.path.join(IMAGENS_DIR, f"{slug_frase}_slide_{i+1:02d}.png")
        success = False
        for attempt in range(MAX_RETRIES):
            logger.info("Gerando imagem %s %d/%d (Tentativa %d/%d)...", image_engine.upper(), i+1, slides_count, attempt+1, MAX_RETRIES)
            logger.info("  Prompt: %s", img_prompt)
            try:
                gerador_func(prompt=img_prompt, arquivo_saida=imagem_path, idioma=idioma)
                if os.path.exists(imagem_path) and os.path.getsize(imagem_path) > 1024:
                    generated_image_paths.append(imagem_path)
                    success = True
                    logger.info("✅ Imagem %d gerada com sucesso: %s", i+1, imagem_path)
                    break
            except Exception as e:
                logger.error("❌ Falha na tentativa %d de gerar a imagem %d: %s", attempt+1, i+1, e)
                if attempt < MAX_RETRIES - 1:
                    time.sleep(5)
        if not success:
            logger.error("❌❌ Falha em gerar a imagem %d após %d tentativas. Pulando este slide.", i+1, MAX_RETRIES)
    
    if not generated_image_paths:
        raise RuntimeError("Nenhuma imagem pôde ser gerada. Abortando a criação do vídeo.")

    # ===== Decisão: queimar texto na imagem OU no vídeo =====
    motion_key = (motion or "none").lower()
    if motion_key in ("none", "1"):
        logger.info("✍️  Modo SEM MOVIMENTO: Renderizando frase nas imagens via Python...")
        slides_para_ffmpeg = []
        template_img = _map_video_style_to_image_template(video_style)
        for i, img_path in enumerate(generated_image_paths):
            saida_com_texto_path = os.path.join(IMAGENS_DIR, f"{slug_frase}_slide_{i+1:02d}_com_texto.png")
            escrever_frase_na_imagem(
                imagem_path=img_path,
                frase=frase,
                saida_path=saida_com_texto_path,
                template=template_img,
                idioma=idioma
            )
            slides_para_ffmpeg.append(saida_com_texto_path)
        frase_para_ffmpeg = ""  # no vídeo, não desenha a frase
    else:
        logger.info("✍️  Modo COM MOVIMENTO: Frase será desenhada estática no VÍDEO (FFmpeg)...")
        slides_para_ffmpeg = generated_image_paths
        frase_para_ffmpeg = frase

    logger.info("🖼️ Slides prontos para o vídeo (%d).", len(slides_para_ffmpeg))

    # Geração do vídeo (video.py já implementa a lógica de desenhar ou não a frase principal)
    try:
        gerar_video(
            imagem_path=slides_para_ffmpeg[0],
            saida_path=video_final,
            preset="fullhd",
            idioma=idioma,
            tts_engine=tts_engine,
            legendas=legendas,
            video_style=video_style,
            motion=motion,
            slides_paths=slides_para_ffmpeg,
            content_mode=modo_conteudo,
            frase_principal=frase_para_ffmpeg
        )
    except TypeError:
        # compat antigo
        gerar_video(
            imagem_path=slides_para_ffmpeg[0],
            saida_path=video_final,
            preset="fullhd",
            idioma=idioma,
            tts_engine=tts_engine,
            legendas=legendas,
            video_style=video_style,
            motion=motion,
            slides_paths=slides_para_ffmpeg,
            frase_principal=frase_para_ffmpeg
        )

    ok_post = _postar_com_retry(video_final=video_final, idioma=idioma, desc_tiktok=desc_tiktok)
    if ok_post:
        logger.info("✅ Processo concluído com sucesso!")
    else:
        logger.error("❌ Processo finalizado sem confirmar postagem (após retries). Veja logs!")

def postar_em_intervalo(cada_horas, modo_conteudo, idioma, tts_engine, legendas, video_style, motion, slides_count, image_engine):
    base_secs = _human_next_interval_seconds(float(cada_horas))
    logger.info("⏳ Postagem automática a cada ~%.2f h (com jitter humano).", base_secs/3600.0)
    try:
        while True:
            _maybe_sleep_start_jitter()
            ok = _executar_com_timeout((modo_conteudo, idioma, tts_engine, legendas, video_style, motion, slides_count, image_engine))
            nxt = _human_next_interval_seconds(float(cada_horas))
            if not ok:
                logger.warning("⚠️ Execução falhou. Aguardando próximo ciclo...")
            for _ in range(int(nxt/5)):
                time.sleep(5)
    except KeyboardInterrupt:
        print("\n🛑 Automático interrompido.")

# ========================= ENTRADA =========================
if __name__ == "__main__":
    try:
        multiprocessing.freeze_support()
    except Exception:
        pass

    env_motion = os.getenv("MOTION", "none").strip().lower()
    valid_motions = {v[0] for v in MOTION_OPTIONS.values()}
    if env_motion not in valid_motions:
        env_motion = "none"

    while True:
        modo_exec = _menu_modo_execucao()
        if modo_exec is None: 
            continue 

        idioma = _selecionar_idioma()
        if idioma is None: 
            continue

        tipo_persona = _submenu_conteudo_por_idioma(idioma)
        if tipo_persona is None: 
            continue
        tipo, persona = tipo_persona

        # VEO3 (interativo/auto)
        if tipo == "veo3":
            if not _HAVE_VEO3:
                print("\n❌ O módulo utils/veo3.py não foi encontrado.")
                sys.exit(1)
            if modo_exec == "2":
                intervalo_horas = _ler_intervalo_horas()
                if intervalo_horas is None: 
                    continue
                veo3_postar_em_intervalo(persona=persona, idioma=idioma, cada_horas=intervalo_horas)
            else:
                veo3_executar_interativo(persona=persona, idioma=idioma)
            if modo_exec == "1": 
                break
            else: 
                continue

        if modo_exec == "2":
            intervalo_horas = _ler_intervalo_horas()
            if intervalo_horas is None: 
                continue
        else:
            intervalo_horas = None

        tts_engine = _selecionar_tts_engine()
        if tts_engine is None: continue
        legendas = _selecionar_legendas()
        if legendas is None: continue
        video_style = _selecionar_estilo_video()
        if video_style is None: continue
        motion = _selecionar_motion(env_motion)
        if motion is None: continue
        slides_count = _selecionar_qtd_fotos(DEFAULT_SLIDES_COUNT)
        if slides_count is None: continue
        
        image_engine = _selecionar_gerador_imagens(os.getenv("IMAGE_MODE", "pexels").lower())
        if image_engine is None: continue
        
        args_tuple = (tipo, idioma, tts_engine, legendas, video_style, motion, slides_count, image_engine)

        if modo_exec == "1":
            _executar_com_timeout(args_tuple)
            break 
        else:
            postar_em_intervalo(intervalo_horas, *args_tuple)
            break
