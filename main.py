# main.py
# CLI com menus em camadas e opção de "b/voltar" em todos os passos.
# Integra com utils/veo3.py (interativo e automático) e mantém os fluxos motivacional/tarot.

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
    # Tenta importar a função para DALL-E. Não quebra se não existir.
    from utils.imagem import gerar_imagem_dalle
    _HAVE_DALLE_FUNC = True
except ImportError:
    _HAVE_DALLE_FUNC = False
# -------------------------------------------------------------
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
MAX_RETRIES = 3 # Número de tentativas para gerar cada imagem

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
    """
    Envolve postar_no_tiktok_e_renomear com tentativas extras caso falhe.
    Controlado por:
      - ONEOFF_RETRY_MINUTES (default 10)
      - ONEOFF_RETRY_MAX (default 1)  -> total de tentativas = 1 (imediata) + ONEOFF_RETRY_MAX
    """
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

        # Falhou
        if tentativa > max_retries + 1:  # 1 imediata + N retries
            logger.error("❌ Falha em todas as tentativas de postagem (%d).", tentativa - 1)
            return False

        # Preparar retry
        logger.warning("⚠️ Post falhou (tentativa %d). Tentarei de novo em %d min...", tentativa, delay_min)
        # Fecha chromedrivers/navegadores da execução anterior p/ evitar sessão zumbi
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
HUMAN_JITTER_MIN_SEC  = _int_env("HUMAN_INTERVAL_JITTER_MIN_SEC", -900)
HUMAN_JITTER_MAX_SEC  = _int_env("HUMAN_INTERVAL__JITTER_MAX_SEC",  900)
START_JITTER_MIN_SEC  = _int_env("HUMAN_START_JITTER_MIN_SEC", 0)
START_JITTER_MAX_SEC  = _int_env("HUMAN_START_JITTER_MAX_SEC", 0)

def _human_next_interval_seconds(base_hours):
    lo = HUMAN_JITTER_MIN_SEC
    hi = HUMAN_JITTER_MAX_SEC
    if lo > hi:
        lo, hi = hi, lo
    offset = random.randint(lo, hi)
    base = max(60, int(base_hours * 3600))
    nxt = base + offset
    if nxt < 60:
        nxt = 60
    return nxt

def _maybe_sleep_start_jitter():
    lo = max(0, START_JITTER_MIN_SEC)
    hi = max(0, START_JITTER_MAX_SEC)
    if hi <= 0:
        return 0
    if lo > hi:
        lo, hi = hi, lo
    sl = random.randint(lo, hi)
    if sl > 0:
        logger.info("⏳ Jitter inicial: aguardando %ds antes do primeiro ciclo...", sl)
        time.sleep(sl)
    return sl

# ========================= UI (menus com VOLTAR) =========================
_BACK_TOKENS = {"b", "voltar", "back"}

def _menu_modo_execucao():
    while True:
        print("\nO que você deseja fazer?")
        print("1. Postar agora (uma vez) *padrão")
        print("2. Postar automaticamente a cada X horas")
        op = input("Escolha 1 ou 2: ").strip().lower()
        if op in {"1", "2", ""}:
             return "2" if op == "2" else "1"
        print("Opção inválida.")

def _selecionar_idioma():
    while True:
        print("\nEscolha o país para referência da língua das mensagens:")
        print("1. EUA (Inglês) *padrão")
        print("2. Brasil (pt-br)")
        print("3. Árabe (egípcio)")
        print("b. Voltar")
        op = input("Digite a opção: ").strip().lower()
        if op in _BACK_TOKENS: return None
        if op in {"", "1"}: return "en"
        if op == "2": return "pt-br"
        if op == "3": return "ar-eg"
        print("Opção inválida!")

def _submenu_conteudo_por_idioma(idioma):
    while True:
        if idioma == "ar-eg":
            print("\nSelecione o conteúdo (Árabe):")
            print("1. Motivacional")
            print("2. Cartomante")
            if _HAVE_VEO3: print("3. Veo3 (Yasmina)")
            print("b. Voltar")
            op = input("Digite a opção: ").strip().lower()
            if op in _BACK_TOKENS: return None
            if op == "3" and _HAVE_VEO3: return ("veo3", "yasmina")
            return ("tarot", None) if op == "2" else ("motivacional", None)

        if idioma == "pt-br":
            print("\nSelecione o conteúdo (Brasil):")
            print("1. Motivacional")
            print("2. Cartomante")
            if _HAVE_VEO3: print("3. Veo3 (Luisa)")
            print("b. Voltar")
            op = input("Digite a opção: ").strip().lower()
            if op in _BACK_TOKENS: return None
            if op == "3" and _HAVE_VEO3: return ("veo3", "luisa")
            return ("tarot", None) if op == "2" else ("motivacional", None)

        print("\nSelecione o conteúdo (EUA):")
        print("1. Motivacional")
        print("2. Cartomante")
        print("b. Voltar")
        op = input("Digite a opção: ").strip().lower()
        if op in _BACK_TOKENS: return None
        return ("tarot", None) if op == "2" else ("motivacional", None)

def _ler_intervalo_horas():
    while True:
        raw = input("De quantas em quantas horas? (ex.: 3 ou 1.5)  [b=voltar]: ").strip().replace(",", ".").lower()
        if raw in _BACK_TOKENS: return None
        try:
            horas = float(raw)
            if horas <= 0: raise ValueError
            return horas
        except Exception:
            print("Valor inválido.")

def _selecionar_tts_engine():
    while True:
        print("\nEscolha o mecanismo de voz (TTS):")
        print("1. Gemini (padrão)")
        print("2. ElevenLabs")
        print("b. Voltar")
        op = input("Escolha: ").strip().lower()
        if op in _BACK_TOKENS: return None
        return "elevenlabs" if op == "2" else "gemini"

def _selecionar_legendas():
    while True:
        print("\nDeseja adicionar legendas sincronizadas no vídeo?")
        print("1. Sim *padrão")
        print("2. Não")
        print("b. Voltar")
        op = input("Escolha: ").strip().lower()
        if op in _BACK_TOKENS: return None
        return False if op == "2" else True

def _selecionar_estilo_video():
    while True:
        print("\nEscolha o estilo do vídeo (legendas/typography):")
        print("0. Aleatório *padrão")
        for k, (_, desc) in STYLE_OPTIONS.items():
            print(f"{k}. {desc}")
        print("b. Voltar")
        op = input("Opção: ").strip().lower()
        if op in _BACK_TOKENS: return None
        if op in STYLE_OPTIONS:
            return STYLE_OPTIONS[op][0]
        if op in {"", "0"}:
            escolha = random.choice(list(STYLE_OPTIONS.values()))[0]
            print(f"Estilo sorteado: {escolha}")
            return escolha
        print("Opção inválida!")

def _selecionar_motion(env_default):
    while True:
        print("\nMovimento do vídeo:")
        print(f"(Enter para manter do .env: '{env_default}')")
        for k, (key, desc) in MOTION_OPTIONS.items():
            print(f"{k}. {desc}")
        print("b. Voltar")
        op = input("Opção: ").strip().lower()
        if op in _BACK_TOKENS: return None
        if op == "":
            print(f"Movimento: {env_default} (do .env)")
            return env_default
        if op == "0":
            pool = [v[0] for v in MOTION_OPTIONS.values() if v[0] not in ("none", "random")]
            escolha = random.choice(pool); print(f"Movimento sorteado: {escolha}")
            return escolha
        if op in MOTION_OPTIONS: return MOTION_OPTIONS[op][0]
        print("Opção inválida!")

def _selecionar_qtd_fotos(padrao):
    while True:
        print("\nQuantidade de fotos no vídeo (inclui a capa).")
        print(f"Enter = usar .env (SLIDES_COUNT={padrao}) | b = voltar")
        raw = input("Digite 1..10: ").strip().lower()
        if raw in _BACK_TOKENS: return None
        if raw == "":
            return max(1, min(10, padrao))
        try:
            n = int(raw)
            if 1 <= n <= 10:
                return n
        except Exception:
            pass
        print("Valor inválido!")

def _selecionar_gerador_imagens(padrao):
    while True:
        print("\nSelecione qual o mecanismo de geração de imagens:")
        print("1. Pexels (Fotos Reais)")
        print("2. DALL-E / ChatGPT (Geradas por IA)")
        print(f"Enter = usar .env (IMAGE_MODE={padrao}) | b = voltar")
        
        raw = input("Digite 1 ou 2: ").strip().lower()

        if raw in _BACK_TOKENS: return None
        if raw == "":
            print(f" → Usando padrão: {padrao}")
            return padrao
        if raw == "1":
            print(" → Selecionado: Pexels")
            return "pexels"
        if raw == "2":
            # --- CORRIGIDO: Usa a variável renomeada ---
            if not _HAVE_DALLE_FUNC:
                print("   ❌ AVISO: A função 'gerar_imagem_dalle' não foi encontrada em 'utils/imagem.py'.")
                print("   ❌ Esta opção não funcionará até que a função seja implementada.")
            print(" → Selecionado: DALL-E / ChatGPT")
            return "dalle"
        print("Valor inválido!")

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

    if modo_conteudo == "tarot" and _HAVE_TAROT_FUNCS:
        tema_imagem = gerar_prompt_tarot(idioma)
        frase = gerar_frase_tarot_curta(idioma)
    else:
        tema_imagem = gerar_frase_motivacional(idioma)
        frase = tema_imagem

    try:
        hashtags_list = gerar_hashtags_virais(frase, idioma=idioma, n=3)
        desc_tiktok = (frase + " " + " ".join(hashtags_list)).strip()
    except Exception as e:
        logger.warning("Falha ao gerar hashtags (seguirei sem): %s", e)
        desc_tiktok = frase

    slug_frase = gerar_slug(frase)
    video_final = os.path.join(VIDEOS_DIR, f"{slug_frase}.mp4")

    logger.info(f"Gerando {slides_count} prompts de imagem com o tema: '{tema_imagem}'")
    prompts_de_imagem = gerar_prompts_de_imagem_variados(tema_imagem, slides_count, idioma)

    generated_image_paths = []
    if image_engine == 'dalle':
        if not _HAVE_DALLE_FUNC:
            raise RuntimeError("A função 'gerar_imagem_dalle' é necessária para este modo, mas não foi encontrada.")
        
        for i, img_prompt in enumerate(prompts_de_imagem):
            imagem_path = os.path.join(IMAGENS_DIR, f"{slug_frase}_slide_{i+1:02d}.png")
            success = False
            for attempt in range(MAX_RETRIES):
                logger.info(f"Gerando imagem DALL-E {i+1}/{slides_count} (Tentativa {attempt+1}/{MAX_RETRIES})...")
                logger.info(f"  Prompt: {img_prompt}")
                try:
                    gerar_imagem_dalle(prompt=img_prompt, arquivo_saida=imagem_path, idioma=idioma)
                    if os.path.exists(imagem_path) and os.path.getsize(imagem_path) > 1024:
                        generated_image_paths.append(imagem_path)
                        success = True
                        logger.info(f"✅ Imagem {i+1} gerada com sucesso: {imagem_path}")
                        break
                except Exception as e:
                    logger.error(f"❌ Falha na tentativa {attempt+1} de gerar a imagem {i+1}: {e}")
                    if attempt < MAX_RETRIES - 1: time.sleep(5)
            if not success:
                logger.error(f"❌❌ Falha em gerar a imagem {i+1} após {MAX_RETRIES} tentativas. Pulando este slide.")

    else: # Padrão é Pexels
        for i, img_prompt in enumerate(prompts_de_imagem):
            imagem_path = os.path.join(IMAGENS_DIR, f"{slug_frase}_slide_{i+1:02d}.png")
            success = False
            for attempt in range(MAX_RETRIES):
                logger.info(f"Gerando imagem Pexels {i+1}/{slides_count} (Tentativa {attempt+1}/{MAX_RETRIES})...")
                logger.info(f"  Prompt: {img_prompt}")
                try:
                    gerar_imagem_com_frase(prompt=img_prompt, arquivo_saida=imagem_path, idioma=idioma)
                    if os.path.exists(imagem_path) and os.path.getsize(imagem_path) > 1024:
                        generated_image_paths.append(imagem_path)
                        success = True
                        logger.info(f"✅ Imagem {i+1} gerada com sucesso: {imagem_path}")
                        break
                except Exception as e:
                    logger.error(f"❌ Falha na tentativa {attempt+1} de gerar a imagem {i+1}: {e}")
                    if attempt < MAX_RETRIES - 1: time.sleep(5)
            if not success:
                logger.error(f"❌❌ Falha em gerar a imagem {i+1} após {MAX_RETRIES} tentativas. Pulando este slide.")
    
    if not generated_image_paths:
        raise RuntimeError("Nenhuma imagem pôde ser gerada. Abortando a criação do vídeo.")

    # --- ÚNICA ALTERAÇÃO SOLICITADA ---
    # Escreve a frase principal em TODAS as imagens geradas, não apenas na primeira.
    slides_para_video = []
    logger.info(f"✍️  Escrevendo a frase '{frase[:30]}...' em todas as {len(generated_image_paths)} imagens.")
    template_img = _map_video_style_to_image_template(video_style)

    for i, img_path in enumerate(generated_image_paths):
        # Cria um novo nome de arquivo para a imagem com texto
        nome_base, extensao = os.path.splitext(os.path.basename(img_path))
        saida_com_texto_path = os.path.join(IMAGENS_DIR, f"{nome_base}_com_texto.png")
        
        escrever_frase_na_imagem(
            imagem_path=img_path,
            frase=frase,
            saida_path=saida_com_texto_path,
            template=template_img,
            idioma=idioma
        )
        slides_para_video.append(saida_com_texto_path)
    # --- FIM DA ALTERAÇÃO ---

    logger.info(f"🖼️ Slides com texto prontos para o vídeo ({len(slides_para_video)}).")

    try:
        gerar_video(
            imagem_path=slides_para_video[0], # Usa a primeira imagem com texto como referência
            saida_path=video_final,
            preset="fullhd",
            idioma=idioma,
            tts_engine=tts_engine,
            legendas=legendas,
            video_style=video_style,
            motion=motion,
            slides_paths=slides_para_video,
            content_mode=modo_conteudo
        )
    except TypeError:
        gerar_video(
            imagem_path=slides_para_video[0],
            saida_path=video_final,
            preset="fullhd",
            idioma=idioma,
            tts_engine=tts_engine,
            legendas=legendas,
            video_style=video_style,
            motion=motion,
            slides_paths=slides_para_video
        )

    # ---------- Postagem com retry ----------
    ok_post = _postar_com_retry(video_final=video_final, idioma=idioma, desc_tiktok=desc_tiktok)
    if ok_post:
        logger.info("✅ Processo concluído com sucesso!")
    else:
        logger.error("❌ Processo finalizado sem confirmar postagem (após retries). Veja logs!")

def postar_em_intervalo(cada_horas, modo_conteudo, idioma, tts_engine, legendas, video_style, motion, slides_count, image_engine):
    logger.info(f"⏱️ Modo automático base: {cada_horas:.2f} h (Ctrl+C para parar).")
    _maybe_sleep_start_jitter()

    try:
        while True:
            inicio = datetime.now()
            logger.info(f"🟢 Nova execução ({inicio.strftime('%d/%m %H:%M:%S')}).")

            args_tuple = (modo_conteudo, idioma, tts_engine, legendas, video_style, motion, slides_count, image_engine)
            ok = _executar_com_timeout(args_tuple)

            intervalo_next = _human_next_interval_seconds(cada_horas)
            proxima = inicio + timedelta(seconds=intervalo_next)
            rem_now = max(0.0, (proxima - datetime.now()).total_seconds())
            logger.info(f"✅ Execução {'ok' if ok else 'com falha'}. ⏳ Próxima em ~{rem_now / 60:.0f} min (~{int(rem_now)}s). Alvo: {proxima.strftime('%d/%m %Y %H:%M:%S')}")

            last_hb_ts = time.time()
            while True:
                now = datetime.now()
                rem = (proxima - now).total_seconds()
                if rem <= 0: break
                step = min(30.0, rem)
                time.sleep(max(0.1, step))
                if HEARTBEAT_SECS > 0.0 and (time.time() - last_hb_ts) >= HEARTBEAT_SECS:
                    logger.info(f"⏳ Em execução. Faltam ~{max(0.0, rem) / 60:.0f} min (~{int(max(0.0, rem))}s). Alvo: {proxima.strftime('%d/%m %Y %H:%M:%S')}.")
                    last_hb_ts = time.time()
    except KeyboardInterrupt:
        logger.info("🛑 Automático interrompido.")

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
        if modo_exec is None: continue 
        idioma = _selecionar_idioma()
        if idioma is None: continue
        tipo_persona = _submenu_conteudo_por_idioma(idioma)
        if tipo_persona is None: continue
        tipo, persona = tipo_persona

        if tipo == "veo3":
            if not _HAVE_VEO3:
                print("\n❌ O módulo utils/veo3.py não foi encontrado.")
                sys.exit(1)
            if modo_exec == "2":
                intervalo_horas = _ler_intervalo_horas()
                if intervalo_horas is None: continue
                veo3_postar_em_intervalo(persona=persona, idioma=idioma, cada_horas=intervalo_horas)
            else:
                veo3_executar_interativo(persona=persona, idioma=idioma)
            continue

        if modo_exec == "2":
            intervalo_horas = _ler_intervalo_horas()
            if intervalo_horas is None: continue
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
        else:
            postar_em_intervalo(intervalo_horas, *args_tuple)
