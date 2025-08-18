# main.py
import os
import time
import logging
from datetime import datetime, timedelta
import random
import shutil
from glob import glob

# ====== Robustez extra (timeout/cleanup/watchdog) ======
from concurrent.futures import ProcessPoolExecutor, TimeoutError
import multiprocessing
import atexit
import faulthandler
try:
    import psutil  # opcional (recomendado)
except Exception:
    psutil = None

# >>> Carrega o .env o mais cedo possível e com override
from dotenv import load_dotenv, find_dotenv
ENV_PATH = find_dotenv(usecwd=True)
load_dotenv(ENV_PATH, override=True)

# Só depois do .env carregado, importamos os utilitários que lêem variáveis
from utils.frase import gerar_prompt_paisagem, gerar_frase_motivacional, gerar_slug
from utils.imagem import gerar_imagem_com_frase, escrever_frase_na_imagem, montar_slides_pexels
from utils.video import gerar_video
from utils.tiktok import postar_no_tiktok_e_renomear

# LOG_LEVEL do .env (INFO default)
LOG_LEVEL = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    level=LOG_LEVEL,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ====== Config de robustez via .env ======
ITERATION_TIMEOUT_MIN = float(os.getenv("ITERATION_TIMEOUT_MIN", "12.0"))   # timeout duro por execução
HANG_WATCHDOG_SECS    = int(float(os.getenv("HANG_WATCHDOG_SECS", "600")))  # dump de stack periódico

# Heartbeat (log periódico durante a espera)
HEARTBEAT_MIN  = float(os.getenv("HEARTBEAT_MIN", "10"))   # 0 para desativar
HEARTBEAT_SECS = HEARTBEAT_MIN * 60.0 if HEARTBEAT_MIN > 0 else 0.0

# Política de limpeza de navegador (segura por padrão)
# Opções: "none" | "drivers_only" | "children" | "match" | "all"
CHROME_CLEANUP_POLICY = os.getenv("CHROME_CLEANUP_POLICY", "drivers_only").strip().lower()
CHROME_KILL_MATCH     = os.getenv("CHROME_KILL_MATCH", "").strip()  # usado quando policy=match

# Watchdog para identificar enforcamentos raros
os.makedirs("cache", exist_ok=True)
_watchdog_log_path = os.path.join("cache", "hang_watchdog.log")
try:
    _watchdog_f = open(_watchdog_log_path, "w", buffering=1, encoding="utf-8")
    faulthandler.enable(_watchdog_f)
    # despeja stack a cada HANG_WATCHDOG_SECS se o processo ficar preso em syscalls
    if HANG_WATCHDOG_SECS > 0:
        faulthandler.dump_traceback_later(HANG_WATCHDOG_SECS, repeat=True, file=_watchdog_f)
        atexit.register(faulthandler.cancel_dump_traceback_later)
except Exception as _e:
    logger.debug("Watchdog desativado: %s", _e)

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
IMAGENS_SLIDES_TXT = os.path.join(IMAGENS_DIR, "slides_txt")
AUDIOS_DIR = "audios"
AUDIOS_TTS_DIR = os.path.join(AUDIOS_DIR, "tts")
CACHE_DIR = "cache"

# --------------------- limpeza ---------------------
def _safe_unlink(path: str):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
            logger.debug("removido: %s", path)
    except Exception as e:
        logger.debug("não consegui remover '%s': %s", path, e)

def _limpar_pre_post(imagem_base: str, imagem_capa: str):
    """
    Executa após gerar o vídeo e ANTES de postar:
      - apaga audios/ (raiz) e audios/tts/
      - apaga cache/*.txt (drawtext, etc.)
      - apaga imagens/slides_txt/
      - apaga imagens/* EXCETO imagem_base e imagem_capa (essas ainda serão usadas na postagem)
    """
    # 1) audios raiz e tts
    for d in (AUDIOS_TTS_DIR, AUDIOS_DIR):
        if os.path.isdir(d):
            for nome in os.listdir(d):
                p = os.path.join(d, nome)
                if os.path.isfile(p):
                    _safe_unlink(p)

    # 2) cache/*.txt
    if os.path.isdir(CACHE_DIR):
        for txt in glob(os.path.join(CACHE_DIR, "*.txt")):
            _safe_unlink(txt)

    # 3) imagens/slides_txt (diretório inteiro)
    shutil.rmtree(IMAGENS_SLIDES_TXT, ignore_errors=True)

    # 4) imagens/* exceto capa e base
    keep = {os.path.abspath(imagem_base), os.path.abspath(imagem_capa)}
    if os.path.isdir(IMAGENS_DIR):
        for nome in os.listdir(IMAGENS_DIR):
            p = os.path.join(IMAGENS_DIR, nome)
            if os.path.isdir(p):
                continue
            if os.path.abspath(p) in keep:
                continue
            _safe_unlink(p)

def _limpar_pos_post(imagem_base: str, imagem_capa: str):
    """Executa após postar: remove também imagem_base e imagem_capa."""
    _safe_unlink(imagem_base)
    _safe_unlink(imagem_capa)

# --------------------- helpers robustez ---------------------
def _cleanup_browsers(policy: str = None):
    """
    Limpeza segura de processos do ecossistema Selenium.
    policy:
      - "none"          -> não mata nada
      - "drivers_only"  -> mata apenas chromedriver
      - "children"      -> mata chromes cujo ANCESTRAL é um chromedriver
      - "match"         -> mata chromes cujo cmdline contém CHROME_KILL_MATCH
      - "all"           -> mata TUDO (chrome + chromedriver) — perigoso
    """
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
                    p.kill()
                    killed += 1
                continue

            if "chrome" == name or name == "chrome.exe" or name.startswith("chrome"):
                if policy == "none" or policy == "drivers_only":
                    continue
                if policy == "all":
                    p.kill(); killed += 1; continue

                # policy == children -> verifica ancestrais
                if policy == "children":
                    try:
                        parents = p.parents()
                    except Exception:
                        parents = []
                    if any("chromedriver" in (pp.name() or "").lower() for pp in parents):
                        p.kill(); killed += 1
                    continue

                # policy == match -> substring no cmdline
                if policy == "match" and CHROME_KILL_MATCH:
                    cmd = " ".join(p.info.get("cmdline") or [])
                    if CHROME_KILL_MATCH.lower() in cmd.lower():
                        p.kill(); killed += 1
                continue
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            # melhor ser resiliente do que travar
            continue

    if killed:
        logger.info("🧹 Cleanup browsers (%s): %d processo(s) finalizado(s).", policy, killed)
    else:
        logger.debug("🧹 Cleanup browsers (%s): nada a fazer.", policy)

def _run_rotina_once(args_tuple) -> bool:
    """Executa 1 ciclo completo em *subprocesso* (para timeout duro)."""
    try:
        # pré-limpeza defensiva de zumbis (segura)
        _cleanup_browsers()
        idioma, tts_engine, legendas, video_style, motion, slides_count = args_tuple
        rotina(idioma, tts_engine, legendas, video_style, motion, slides_count)
        return True
    except Exception as e:
        logging.exception("Falha na execução única: %s", e)
        return False
    finally:
        # pós-limpeza defensiva (segura)
        _cleanup_browsers()

def _executar_com_timeout(args_tuple) -> bool:
    """Roda _run_rotina_once em processo isolado com timeout."""
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

# --------------------- UI ---------------------
def _selecionar_idioma() -> str:
    print("\nEscolha o país para referência da língua das mensagens:")
    print("1. EUA (Inglês) *padrão")
    print("2. Brasil (pt-br)")
    op = input("Digite o número da opção (1 ou 2): ").strip()
    if op not in ("1", "2"):
        print("Opção inválida! Usando EUA (Inglês) como padrão.")
        op = "1"
    return "en" if op == "1" else "pt-br"

def _menu_modo_execucao() -> str:
    print("\nO que você deseja fazer?")
    print("1. Postar agora (uma vez)")
    print("2. Postar automaticamente a cada X horas")
    op = input("Escolha 1 ou 2: ").strip()
    return op if op in ("1","2") else "1"

def _ler_intervalo_horas() -> float:
    while True:
        raw = input("De quantas em quantas horas? (ex.: 3 ou 1.5): ").strip().replace(",", ".")
        try:
            horas = float(raw)
            if horas <= 0:
                raise ValueError
            return horas
        except Exception:
            print("Valor inválido. Digite um número > 0 (ex.: 2, 2.5, 0.5).")

def _selecionar_tts_engine() -> str:
    print("\nEscolha o mecanismo de voz (TTS):")
    print("1. Gemini (padrão)")
    print("2. ElevenLabs")
    op = input("Escolha 1 ou 2: ").strip()
    return "elevenlabs" if op == "2" else "gemini"

def _selecionar_legendas() -> bool:
    print("\nDeseja adicionar legendas sincronizadas no vídeo?")
    print("1. Sim (padrão)")
    print("2. Não")
    op = input("Escolha 1 ou 2: ").strip()
    return False if op == "2" else True

def _selecionar_estilo_video() -> str:
    print("\nEscolha o estilo do vídeo (legendas/typography):")
    print("0. Aleatório")
    for k, (_, desc) in STYLE_OPTIONS.items():
        print(f"{k}. {desc}")
    op = input("Digite o número da opção: ").strip()
    if op == "0":
        escolha = random.choice(list(STYLE_OPTIONS.values()))[0]
        print(f"Estilo sorteado: {escolha}")
        return escolha
    if op in STYLE_OPTIONS:
        return STYLE_OPTIONS[op][0]
    print("Opção inválida! Usando estilo clássico.")
    return "classic"

def _selecionar_motion(env_default: str) -> str:
    print("\nMovimento do vídeo:")
    print("(Enter para manter do .env: '%s')" % env_default)
    for k, (_, desc) in MOTION_OPTIONS.items():
        print(f"{k}. {desc}")
    op = input("Digite o número da opção: ").strip()
    if op == "":
        print(f"Movimento: {env_default} (do .env)")
        return env_default
    if op == "0":
        pool = [v[0] for v in MOTION_OPTIONS.values() if v[0] not in ("none", "random")]
        escolha = random.choice(pool)
        print(f"Movimento sorteado: {escolha}")
        return escolha
    if op in MOTION_OPTIONS:
        return MOTION_OPTIONS[op][0]
    print(f"Opção inválida! Mantendo: {env_default}")
    return env_default

def _selecionar_qtd_fotos(padrao: int) -> int:
    print("\nQuantidade de fotos no vídeo (inclui a capa).")
    print(f"Pressione Enter para usar o padrão do .env (SLIDES_COUNT={padrao}).")
    raw = input("Digite um número entre 1 e 10: ").strip()
    if raw == "":
        return max(1, min(10, padrao))
    try:
        n = int(raw)
        if 1 <= n <= 10:
            return n
        raise ValueError
    except Exception:
        print(f"Valor inválido! Usando {padrao}.")
        return max(1, min(10, padrao))

def _map_video_style_to_image_template(style_key: str) -> str:
    """
    Mapeia o estilo escolhido no menu para o template de imagem.
    Agora 'classic' realmente usa 'classic_serif'.
    """
    s = (style_key or "").lower()
    if s in ("classic", "1"):
        return "classic_serif"
    if s in ("clean", "5"):
        return "modern_block"
    if s in ("serif", "3"):
        return "classic_serif"
    if s in ("mono", "4"):
        return "minimal_center"
    if s in ("modern", "2"):
        return "modern_block"
    return "minimal_center"

# --------------------- pipeline ---------------------
def rotina(idioma: str, tts_engine: str, legendas: bool, video_style: str, motion: str, slides_count: int):
    os.makedirs(IMAGENS_DIR, exist_ok=True)
    os.makedirs("videos", exist_ok=True)
    os.makedirs(AUDIOS_DIR, exist_ok=True)

    logger.info("Gerando conteúdos (%s)...", idioma)
    prompt_imagem = gerar_prompt_paisagem(idioma)
    frase = gerar_frase_motivacional(idioma)

    slug_img = gerar_slug(prompt_imagem)
    slug_frase = gerar_slug(frase)
    imagem_base = os.path.join(IMAGENS_DIR, f"{slug_img}.jpg")
    imagem_capa = os.path.join(IMAGENS_DIR, f"{slug_frase}.jpg")
    video_final = os.path.join("videos", f"{slug_frase}.mp4")

    # 1) capa
    gerar_imagem_com_frase(prompt=prompt_imagem, arquivo_saida=imagem_base)
    template_img = _map_video_style_to_image_template(video_style)
    logger.info("🖌️ Template da imagem selecionado: %s (derivado de '%s')", template_img, video_style)
    escrever_frase_na_imagem(imagem_base, frase, imagem_capa, template=template_img, idioma=idioma)

    # 2) slides do Pexels e replicar o MESMO texto em todas
    try:
        slides_raw = montar_slides_pexels(prompt_imagem, count=max(1, slides_count), primeira_imagem=imagem_capa)
    except Exception as e:
        logger.warning("Falha ao montar slides: %s. Usando apenas a capa.", e)
        slides_raw = [imagem_capa]

    slides_txt_dir = IMAGENS_SLIDES_TXT
    os.makedirs(slides_txt_dir, exist_ok=True)

    slides_com_texto = []
    for i, img_path in enumerate(slides_raw):
        if i == 0 and os.path.abspath(img_path) == os.path.abspath(imagem_capa):
            slides_com_texto.append(img_path)
            continue
        out_path = os.path.join(slides_txt_dir, f"{slug_frase}_slide_{i+1:02d}.jpg")
        try:
            escrever_frase_na_imagem(img_path, frase, out_path, template=template_img, idioma=idioma)
            slides_com_texto.append(out_path)
        except Exception as e:
            logger.warning("Falha ao aplicar frase no slide %d (%s): %s", i+1, img_path, e)

    if not slides_com_texto:
        slides_com_texto = [imagem_capa]

    logger.info("🖼️ Slides com texto (%d): %s", len(slides_com_texto), " | ".join(slides_com_texto))

    # 3) vídeo
    gerar_video(
        imagem_capa,
        video_final,
        preset="hd",
        idioma=idioma,
        tts_engine=tts_engine,
        legendas=legendas,
        video_style=video_style,
        motion=motion,
        slides_paths=slides_com_texto,
        transition=None  # usa TRANSITION do .env
    )

    # >>> Limpeza PRÉ-POST (mantém capa e base para a função de postagem)
    try:
        _limpar_pre_post(imagem_base=imagem_base, imagem_capa=imagem_capa)
        logger.info("🧹 Limpeza pré-post concluída.")
    except Exception as e:
        logger.warning("Falha na limpeza pré-post: %s", e)

    # 4) TikTok
    try:
        postar_no_tiktok_e_renomear(
            descricao_personalizada=frase,
            imagem_base=imagem_base,
            imagem_final=imagem_capa,
            video_final=video_final,
            idioma=idioma
        )
    except Exception as e:
        logger.exception("❌ Falha ao postar no TikTok: %s", e)

    # >>> Limpeza PÓS-POST (remove capa e base também)
    try:
        _limpar_pos_post(imagem_base=imagem_base, imagem_capa=imagem_capa)
        logger.info("🧹 Limpeza pós-post concluída.")
    except Exception as e:
        logger.warning("Falha na limpeza pós-post: %s", e)

    logger.info("✅ Execução concluída.")

def postar_em_intervalo(cada_horas: float, idioma: str, tts_engine: str, legendas: bool, video_style: str, motion: str, slides_count: int):
    logger.info("⏱️ Modo automático: a cada %.2f horas (Ctrl+C para parar).", cada_horas)
    intervalo = float(cada_horas) * 3600.0
    try:
        while True:
            inicio = datetime.now()
            logger.info("🟢 Nova execução (%s).", inicio.strftime("%d/%m %H:%M:%S"))

            # executa em subprocesso com timeout duro + limpeza segura
            ok = _executar_com_timeout((idioma, tts_engine, legendas, video_style, motion, slides_count))

            # agenda a próxima pelo horário-alvo (mantém cadência)
            proxima = inicio + timedelta(seconds=intervalo)
            restante = (proxima - datetime.now()).total_seconds()
            if restante < 0:
                restante = 60.0  # mínimo 1 min entre ciclos

            logger.info("✅ Execução %s. ⏳ Próxima em ~%.0f min.",
                        "ok" if ok else "com falha", restante / 60)

            # dormir em blocos curtos p/ permitir Ctrl+C fácil + heartbeat
            slept = 0.0
            hb_elapsed = 0.0
            while slept < restante:
                step = min(30.0, restante - slept)
                time.sleep(step)
                slept += step
                hb_elapsed += step

                # Heartbeat: imprime a cada HEARTBEAT_SECS se configurado (>0)
                if HEARTBEAT_SECS > 0.0 and hb_elapsed >= HEARTBEAT_SECS:
                    rem = max(0.0, (proxima - datetime.now()).total_seconds())
                    logger.info("⏳ Em execução. Faltam ~%.0f min para o próximo ciclo (alvo: %s).",
                                rem / 60, proxima.strftime("%d/%m %H:%M:%S"))
                    hb_elapsed = 0.0
    except KeyboardInterrupt:
        logger.info("🛑 Automático interrompido.")

if __name__ == "__main__":
    env_motion = os.getenv("MOTION", "none").strip().lower()
    valid_motions = {v[0] for v in MOTION_OPTIONS.values()}
    if env_motion not in valid_motions:
        env_motion = "none"

    idioma = _selecionar_idioma()
    modo = _menu_modo_execucao()
    tts_engine = _selecionar_tts_engine()
    legendas = _selecionar_legendas()
    video_style = _selecionar_estilo_video()
    motion = _selecionar_motion(env_motion)
    slides_count = _selecionar_qtd_fotos(DEFAULT_SLIDES_COUNT)

    args_tuple = (idioma, tts_engine, legendas, video_style, motion, slides_count)

    if modo == "1":
        # também dá para usar timeout no modo único; se preferir direto, troque por: rotina(*args_tuple)
        _executar_com_timeout(args_tuple)
    else:
        intervalo = _ler_intervalo_horas()
        postar_em_intervalo(intervalo, *args_tuple)
