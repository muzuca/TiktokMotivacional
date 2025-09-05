# main.py
# CLI principal: conteúdos Motivacional/Tarot e integração com Veo3.
# Modo "Postar agora" e "Postar automaticamente".
# Correções: evitar dupla geração de narração longa/TTS (gera 1x no video.py),
# calcular slides por estimativa de leitura (WPM) e passar long_text ao gerar_video.

import os
import sys
import time
import logging
import random
import re
from datetime import datetime, timedelta
from concurrent.futures import ProcessPoolExecutor, TimeoutError
import multiprocessing
from typing import Optional, Union

# === Ajusta CWD quando empacotado ===
if getattr(sys, 'frozen', False):
    try:
        os.chdir(os.path.dirname(sys.executable))
    except Exception:
        pass

# ==== .env o mais cedo possível ====
from dotenv import load_dotenv, find_dotenv
ENV_PATH = find_dotenv(usecwd=True)
load_dotenv(ENV_PATH, override=True)

# ==== LOG ====
LOG_LEVEL = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    level=LOG_LEVEL,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ====== Imports do pipeline clássico ======
from utils.frase import (
    gerar_prompts_de_imagem_variados,
    gerar_frase_motivacional,
    gerar_slug,
    gerar_hashtags_virais,
)

# (Opcional) longas — se existirem, usamos; se não, caímos no fallback
try:
    from utils.frase import gerar_frase_motivacional_longa
    _HAVE_LONGO_MOTIV = True
except Exception:
    _HAVE_LONGO_MOTIV = False

try:
    from utils.frase import gerar_prompt_tarot, gerar_frase_tarot_curta, gerar_frase_tarot_longa
    _HAVE_TAROT_FUNCS = True
except Exception:
    _HAVE_TAROT_FUNCS = False

from utils.imagem import escrever_frase_na_imagem, gerar_imagem_com_frase
try:
    from utils.imagem import gerar_imagem_dalle
    _HAVE_DALLE_FUNC = True
except Exception:
    _HAVE_DALLE_FUNC = False

from utils.video import gerar_video
from utils.tiktok import postar_no_tiktok_e_renomear

# (IMPORTANTE) Removido TTS prévio para não duplicar geração no video.py
# try:
#     from utils.audio import gerar_narracao_tts, _duracao_arquivo as _duracao_audio_segundos
#     _HAVE_AUDIO = True
# except Exception:
#     _HAVE_AUDIO = False
_HAVE_AUDIO = False  # garantimos que não use TTS aqui

# ==== Veo3 (menus internos) ====
try:
    from utils.veo3 import executar_interativo as veo3_executar_interativo
    from utils.veo3 import postar_em_intervalo as veo3_postar_em_intervalo
    _HAVE_VEO3 = True
except Exception:
    _HAVE_VEO3 = False

# ====== Constantes/UI ======
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

# DEFAULT_SLIDES_COUNT fica aqui por compat (fallback)
DEFAULT_SLIDES_COUNT = int(os.getenv("SLIDES_COUNT", "4"))

# Dinâmica de slides (parametrizável no .env)
SLIDE_SECONDS_PER_IMAGE = float(os.getenv("SLIDE_SECONDS_PER_IMAGE", "3.0"))
SLIDES_MIN = int(os.getenv("SLIDES_MIN", "3"))
SLIDES_MAX = int(os.getenv("SLIDES_MAX", "10"))

IMAGENS_DIR = "imagens"
VIDEOS_DIR = "videos"
AUDIOS_DIR = "audios"
MAX_RETRIES = 3

_BACK_TOKENS = {"b", "voltar", "back"}
_TT_HEADLESS_ASKED = False

# ====== Helpers ======
def _int_env(name, default):
    try:
        v = os.getenv(name)
        return int(v) if v is not None and str(v).strip() != "" else default
    except Exception:
        return default

def _menu_modo_execucao() -> str:
    while True:
        print("\nO que você deseja fazer?")
        print("1. Postar agora (uma vez) *padrão")
        print("2. Postar automaticamente a cada X horas")
        op = input("Escolha 1 ou 2: ").strip().lower()
        if op in {"", "1"}:
            return "1"
        if op == "2":
            return "2"
        print("Opção inválida.")

def _selecionar_idioma() -> Optional[str]:
    while True:
        print("\nEscolha o país para referência da língua das mensagens:")
        print("1. EUA (Inglês) *padrão")
        print("2. Brasil (pt-br)")
        print("3. Árabe (egípcio)")
        print("4. Rússia (Russo)")
        print("b. Voltar")
        op = input("Digite a opção: ").strip().lower()
        if op in _BACK_TOKENS:
            return None
        if op in {"", "1"}:
            return "en"
        if op == "2":
            return "pt-br"
        if op == "3":
            return "ar-eg"
        if op == "4":
            return "ru"
        print("Opção inválida!")

def _submenu_conteudo_por_idioma(idioma: str):
    if idioma == "ar-eg":
        print("\nSelecione o conteúdo (Árabe):")
        print("1. Motivacional")
        print("2. Cartomante")
        if _HAVE_VEO3:
            print("3. Veo3 (Yasmina)")
        print("b. Voltar")
        op = input("Digite a opção: ").strip().lower()
        if op in _BACK_TOKENS: return None
        if op == "3" and _HAVE_VEO3: return ("veo3", "yasmina")
        return ("tarot", None) if op == "2" else ("motivacional", None)

    if idioma == "pt-br":
        print("\nSelecione o conteúdo (Brasil):")
        print("1. Motivacional")
        print("2. Cartomante")
        if _HAVE_VEO3:
            print("3. Veo3 (Luisa)")
        print("b. Voltar")
        op = input("Digite a opção: ").strip().lower()
        if op in _BACK_TOKENS: return None
        if op == "3" and _HAVE_VEO3: return ("veo3", "luisa")
        return ("tarot", None) if op == "2" else ("motivacional", None)

    if idioma in ("ru", "ru-ru"):
        print("\nSelecione o conteúdo (Rússia):")
        print("1. Motivacional")
        print("2. Cartomante")
        if _HAVE_VEO3:
            print("3. Veo3 (Alina)")
        print("b. Voltar")
        op = input("Digite a opção: ").strip().lower()
        if op in _BACK_TOKENS: return None
        if op == "3" and _HAVE_VEO3: return ("veo3", "alina")
        return ("tarot", None) if op == "2" else ("motivacional", None)

    print("\nSelecione o conteúdo (EUA):")
    print("1. Motivacional")
    print("2. Cartomante")
    print("b. Voltar")
    op = input("Digite a opção: ").strip().lower()
    if op in _BACK_TOKENS: return None
    return ("tarot", None) if op == "2" else ("motivacional", None)

def _ler_intervalo_horas() -> Optional[float]:
    while True:
        raw = input("De quantas em quantas horas? (ex.: 3 ou 1.5)  [b=voltar]: ").strip().replace(",", ".").lower()
        if raw in _BACK_TOKENS:
            return None
        try:
            horas = float(raw)
            if horas <= 0: raise ValueError
            return horas
        except Exception:
            print("Valor inválido.")

def _selecionar_tts_engine() -> Optional[str]:
    while True:
        print("\nEscolha o mecanismo de voz (TTS):")
        print("1. Gemini (padrão)")
        print("2. ElevenLabs")
        print("b. Voltar")
        op = input("Escolha: ").strip().lower()
        if op in _BACK_TOKENS: return None
        return "elevenlabs" if op == "2" else "gemini"

def _selecionar_legendas() -> Optional[bool]:
    while True:
        print("\nDeseja adicionar legendas sincronizadas no vídeo?")
        print("1. Sim *padrão")
        print("2. Não")
        print("b. Voltar")
        op = input("Escolha: ").strip().lower()
        if op in _BACK_TOKENS: return None
        return False if op == "2" else True

def _selecionar_estilo_video() -> Optional[str]:
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

def _selecionar_motion(env_default: str) -> Optional[str]:
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

def _perguntar_headless(label: str, default_on: bool) -> Optional[bool]:
    padrao = 'Sim' if default_on else 'Não'
    print(f"\nExecutar {label} em modo headless?")
    print(f"Enter = {padrao}  |  1. Sim  |  2. Não  |  b. Voltar")
    op = input("Escolha: ").strip().lower()
    if op in _BACK_TOKENS:
        return None
    if op in {'1', 'sim', 's'}:
        return True
    if op in {'2', 'nao', 'não', 'n'}:
        return False
    return default_on

def _selecionar_gerador_imagens(padrao: str) -> Optional[str]:
    while True:
        print("\nSelecione qual o mecanismo de geração de imagens:")
        print("1. Pexels (Fotos Reais)")
        print("2. DALL-E / ChatGPT (Geradas por IA)")
        print(f"Enter = usar .env (IMAGE_MODE={padrao}) | b = voltar")
        raw = input("Digite 1 ou 2: ").strip().lower()
        if raw in _BACK_TOKENS:
            return None
        if raw == "":
            print(f" → Usando padrão: {padrao}")
            return padrao
        if raw == "1":
            print(" → Selecionado: Pexels")
            return "pexels"
        if raw == "2":
            if not _HAVE_DALLE_FUNC:
                print("   ❌ AVISO: 'gerar_imagem_dalle' não está implementado. Esta opção não funcionará.")
            print(" → Selecionado: DALL-E / ChatGPT")
            default_chatgpt_headless = os.getenv('CHATGPT_HEADLESS', '0').strip() != '0'
            ans = _perguntar_headless('o ChatGPT/DALL·E (imagens)', default_chatgpt_headless)
            if ans is None:
                return None
            os.environ['CHATGPT_HEADLESS'] = '1' if ans else '0'
            print(f" → ChatGPT/DALL·E headless: {'ON' if ans else 'OFF'}")
            return "dalle"
        print("Valor inválido!")

def _map_video_style_to_image_template(style_key: str) -> str:
    s = (style_key or "").lower()
    if s in ("classic", "1"): return "classic_serif"
    if s in ("clean", "5"):   return "modern_block"
    if s in ("serif", "3"):   return "classic_serif"
    if s in ("mono", "4"):    return "minimal_center"
    if s in ("modern", "2"):  return "modern_block"
    return "minimal_center"

# === Estimativa de duração (WPM por idioma) ===
_WPM = {
    "en": 155.0,
    "pt-br": 150.0,
    "pt": 150.0,
    "ar-eg": 130.0,
    "ar": 130.0,
    "ru": 140.0,
}
def _estimativa_duracao_segundos(texto: str, idioma: str) -> float:
    if not texto:
        return 0.0
    # conta por tokens separados por espaço (funciona ok p/ a maioria dos alfabetos)
    words = len(texto.split())
    wpm = _WPM.get(idioma.lower(), 150.0)
    secs = (words / wpm) * 60.0
    return max(5.0, secs)  # mínimo de segurança

# ====== Rotina “uma vez” ======
def rotina(modo_conteudo: str, idioma: str, tts_engine: str, legendas: bool,
           video_style: str, motion: str, slides_count: int, image_engine: str,
           ask_tiktok_headless: bool = True):
    """
    Pipeline clássico (motivacional/cartomante).
    Agora NÃO gera TTS antes para medir duração (evita duplicar chamadas).
    Em vez disso, usa estimativa por WPM para definir o número de slides.
    Passa a narração longa para o gerar_video() (quando suportado).
    """
    os.makedirs(IMAGENS_DIR, exist_ok=True)
    os.makedirs(VIDEOS_DIR, exist_ok=True)
    os.makedirs(AUDIOS_DIR, exist_ok=True)

    # >>> pergunta de headless do TikTok no início (apenas 1x por execução)
    global _TT_HEADLESS_ASKED
    if ask_tiktok_headless and not _TT_HEADLESS_ASKED:
        default_tt_headless = os.getenv('TIKTOK_HEADLESS', '1').strip() != '0'
        ans_tt = _perguntar_headless('o TikTok (upload)', default_tt_headless)
        if ans_tt is None:
            logger.info('Operação cancelada antes de iniciar a geração.')
            return
        os.environ['TIKTOK_HEADLESS'] = '1' if ans_tt else '0'
        logger.info(' → TikTok headless: %s', 'ON' if ans_tt else 'OFF')
        _TT_HEADLESS_ASKED = True
    # <<<

    logger.info("Gerando conteúdos (%s | modo=%s | imagens=%s)...", idioma, modo_conteudo, image_engine)

    # 1) Frase/tema curto e hashtags
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

    # 2) Narração longa (gera APENAS uma vez aqui; TTS fica no video.py)
    long_text = None
    if _HAVE_TAROT_FUNCS and modo_conteudo == "tarot":
        try:
            long_text = gerar_frase_tarot_longa(idioma)
        except Exception:
            long_text = None
    if long_text is None and _HAVE_LONGO_MOTIV:
        try:
            long_text = gerar_frase_motivacional_longa(idioma)
        except Exception:
            long_text = None
    if not long_text:
        base = (frase or "Your time to grow is now.")
        # fallback ~100 palavras
        long_text = (" ".join([base] * 20)).strip()

    logger.info("📝 Narração gerada internamente (frase longa).")

    # 3) Slides por estimativa (NÃO chama TTS aqui)
    dur_est = _estimativa_duracao_segundos(long_text, idioma=idioma)
    if SLIDE_SECONDS_PER_IMAGE > 0:
        slides_auto = int(round(dur_est / SLIDE_SECONDS_PER_IMAGE))
    else:
        slides_auto = DEFAULT_SLIDES_COUNT
    slides_auto = max(SLIDES_MIN, min(SLIDES_MAX, slides_auto))
    logger.info("🧮 Slides (estimado): duração≈%.2fs | %.1fs/slide ⇒ %d slides (min=%d, max=%d)",
                dur_est, SLIDE_SECONDS_PER_IMAGE, slides_auto, SLIDES_MIN, SLIDES_MAX)

    slides_count = slides_auto

    # 4) Imagens
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
                        generated_image_paths.append(imagem_path); success = True
                        logger.info(f"✅ Imagem {i+1} gerada: {imagem_path}")
                        break
                except Exception as e:
                    logger.error(f"Falha na tentativa {attempt+1} (imagem {i+1}): {e}")
                    if attempt < MAX_RETRIES - 1: time.sleep(5)
            if not success:
                logger.error(f"❌ Não consegui gerar a imagem {i+1} após {MAX_RETRIES} tentativas.")
    else:
        for i, img_prompt in enumerate(prompts_de_imagem):
            imagem_path = os.path.join(IMAGENS_DIR, f"{slug_frase}_slide_{i+1:02d}.png")
            success = False
            for attempt in range(MAX_RETRIES):
                logger.info(f"Gerando imagem Pexels {i+1}/{slides_count} (Tentativa {attempt+1}/{MAX_RETRIES})...")
                logger.info(f"  Prompt: {img_prompt}")
                try:
                    gerar_imagem_com_frase(prompt=img_prompt, arquivo_saida=imagem_path, idioma=idioma)
                    if os.path.exists(imagem_path) and os.path.getsize(imagem_path) > 1024:
                        generated_image_paths.append(imagem_path); success = True
                        logger.info(f"✅ Imagem {i+1} gerada: {imagem_path}")
                        break
                except Exception as e:
                    logger.error(f"Falha na tentativa {attempt+1} (imagem {i+1}): {e}")
                    if attempt < MAX_RETRIES - 1: time.sleep(5)
            if not success:
                logger.error(f"❌ Não consegui gerar a imagem {i+1} após {MAX_RETRIES} tentativas.")

    if not generated_image_paths:
        raise RuntimeError("Nenhuma imagem foi gerada. Abortando o vídeo.")

    # 5) Escrever frase nas imagens
    slides_para_video = []
    template_img = _map_video_style_to_image_template(video_style)
    logger.info(f"✍️  Escrevendo a frase '{frase[:30]}...' em {len(generated_image_paths)} imagens.")
    for img_path in generated_image_paths:
        nome_base = os.path.splitext(os.path.basename(img_path))[0]
        out_path = os.path.join(IMAGENS_DIR, f"{nome_base}_com_texto.png")
        escrever_frase_na_imagem(
            imagem_path=img_path,
            frase=frase,
            saida_path=out_path,
            template=template_img,
            idioma=idioma
        )
        slides_para_video.append(out_path)

    # 6) Gerar vídeo (passa long_text; fallback se assinatura não aceitar)
    logger.info("🖼️ Slides prontos (%d). Gerando vídeo…", len(slides_para_video))
    try:
        gerar_video(
            imagem_path=slides_para_video[0],
            saida_path=video_final,
            preset="fullhd",
            idioma=idioma,
            tts_engine=tts_engine,
            legendas=legendas,
            video_style=video_style,
            motion=motion,
            slides_paths=slides_para_video,
            content_mode=modo_conteudo,
            long_text=long_text  # ← evita nova geração dentro do video.py
        )
    except TypeError:
        # compatibilidade com versões antigas de utils/video.py que não aceitam long_text
        logger.warning("video.py não aceita parâmetro 'long_text'. Ele irá gerar a narração internamente.")
        gerar_video(
            imagem_path=slides_para_video[0],
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

    # 7) Postar
    ok = postar_no_tiktok_e_renomear(
        descricao_personalizada=desc_tiktok,
        video_final=video_final,
        idioma=idioma
    )
    if ok:
        logger.info("✅ Processo concluído com sucesso!")
    else:
        logger.error("❌ Falha na postagem. Verifique os logs!")

# ====== Execução com timeout (para modo automático) ======
ITERATION_TIMEOUT_MIN = float(os.getenv("ITERATION_TIMEOUT_MIN", "12.0"))

def _run_rotina_once(args_tuple):
    try:
        rotina(*args_tuple)
        return True
    except Exception as e:
        logging.exception("Falha na execução única: %s", e)
        return False

def _executar_com_timeout(args_tuple) -> Union[bool, None]:
    """
    Executa a rotina em um processo isolado com timeout.
    Retornos:
      - True  -> finalizou sem exceção
      - False -> houve exceção na rotina
      - None  -> TIMEOUT (estourou o tempo configurado)
    """
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as ex:
        fut = ex.submit(_run_rotina_once, args_tuple)
        try:
            return bool(fut.result(timeout=int(ITERATION_TIMEOUT_MIN * 60)))
        except TimeoutError:
            logging.error("⏱️ Iteração excedeu %.1f min — abortando.", ITERATION_TIMEOUT_MIN)
            return None

# ====== Modo automático (pipeline clássico) ======
def postar_em_intervalo(cada_horas: float, modo_conteudo: str, idioma: str, tts_engine: str,
                        legendas: bool, video_style: str, motion: str, slides_count: int,
                        image_engine: str):
    logger.info(f"⏱️ Modo automático base: {cada_horas:.2f} h (Ctrl+C para parar).")

    # Perguntar headless do TikTok UMA vez antes do loop
    default_tt_headless = os.getenv('TIKTOK_HEADLESS', '1').strip() != '0'
    ans_tt = _perguntar_headless('o TikTok (upload)', default_tt_headless)
    if ans_tt is None:
        logger.info('Cancelado.')
        return
    os.environ['TIKTOK_HEADLESS'] = '1' if ans_tt else '0'
    logger.info(' → TikTok headless: %s', 'ON' if ans_tt else 'OFF')

    try:
        while True:
            inicio = datetime.now()
            logger.info("🟢 Nova execução (%s).", inicio.strftime('%d/%m %H:%M:%S'))

            args_tuple = (
                modo_conteudo, idioma, tts_engine, legendas,
                video_style, motion, 0, image_engine, False
            )
            ok = _executar_com_timeout(args_tuple)

            proxima = inicio + timedelta(hours=cada_horas)
            rem = max(0.0, (proxima - datetime.now()).total_seconds())
            rem_horas = rem / 3600.0

            if ok is True:
                logger.info("✅ Execução OK.")
            elif ok is False:
                logger.warning("❌ Execução falhou.")
            else:
                logger.warning("⏱️ Execução excedeu %.1f min (timeout).", ITERATION_TIMEOUT_MIN)

            logger.info("Próxima execução em %.2f horas...", rem_horas)
            time.sleep(rem)
    except KeyboardInterrupt:
        logger.info("🟥 Encerrado pelo usuário.")

# ====== Menus de alto nível ======
def _menu_principal():
    while True:
        modo = _menu_modo_execucao()

        idioma = _selecionar_idioma()
        if idioma is None:
            continue

        conteudo = _submenu_conteudo_por_idioma(idioma)
        if conteudo is None:
            continue

        # Veo3 tem fluxo próprio
        if conteudo[0] == "veo3":
            if modo == "2":
                horas = _ler_intervalo_horas()
                if horas is None:
                    continue
                veo3_postar_em_intervalo(persona=conteudo[1], idioma=idioma, cada_horas=horas)
            else:
                veo3_executar_interativo(persona=conteudo[1], idioma=idioma)
            continue

        if modo == "2":
            horas = _ler_intervalo_horas()
            if horas is None:
                continue

        tts_engine = _selecionar_tts_engine()
        if tts_engine is None:
            continue

        legendas = _selecionar_legendas()
        if legendas is None:
            continue

        video_style = _selecionar_estilo_video()
        if video_style is None:
            continue

        motion = _selecionar_motion(env_default=os.getenv("MOTION", "kenburns_in"))
        if motion is None:
            continue

        image_engine = _selecionar_gerador_imagens(os.getenv("IMAGE_MODE", "pexels"))
        if image_engine is None:
            continue

        if modo == "2":
            postar_em_intervalo(
                cada_horas=horas,
                modo_conteudo=conteudo[0],
                idioma=idioma,
                tts_engine=tts_engine,
                legendas=legendas,
                video_style=video_style,
                motion=motion,
                slides_count=0,
                image_engine=image_engine
            )
        else:
            rotina(
                modo_conteudo=conteudo[0],
                idioma=idioma,
                tts_engine=tts_engine,
                legendas=legendas,
                video_style=video_style,
                motion=motion,
                slides_count=0,
                image_engine=image_engine,
                ask_tiktok_headless=True
            )

if __name__ == "__main__":
    try:
        multiprocessing.freeze_support()
    except Exception:
        pass
    _menu_principal()
