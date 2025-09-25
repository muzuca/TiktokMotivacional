# main.py
import os
import sys
import time
import logging
import random
import shutil
from datetime import datetime, timedelta
from concurrent.futures import ProcessPoolExecutor, TimeoutError
import multiprocessing
from typing import Optional, Union

if getattr(sys, 'frozen', False):
    try:
        os.chdir(os.path.dirname(sys.executable))
    except Exception:
        pass

from dotenv import load_dotenv, find_dotenv
ENV_PATH = find_dotenv(usecwd=True)
load_dotenv(ENV_PATH, override=True)

_ENV_PATH = ENV_PATH
try:
    _ENV_MTIME = os.path.getmtime(_ENV_PATH) if _ENV_PATH else None
except OSError:
    _ENV_MTIME = None

LOG_LEVEL = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(level=LOG_LEVEL, format='[%(asctime)s] %(levelname)s: %(message)s', datefmt='%H:%M:%S')
logging.getLogger("seleniumwire").setLevel(logging.WARNING)
logging.getLogger("hpack").setLevel(logging.WARNING)
logging.getLogger("webdriver_manager").setLevel(logging.WARNING) # Adicionado para limpar logs
logger = logging.getLogger(__name__)

# ====== Imports do pipeline ======
from utils.frase import gerar_prompts_de_imagem_variados, gerar_frase_motivacional, gerar_slug, gerar_hashtags_virais
try:
    from utils.frase import gerar_frase_motivacional_longa
    _HAVE_LONGO_MOTIV = True
except Exception: _HAVE_LONGO_MOTIV = False
try:
    from utils.frase import gerar_prompt_tarot, gerar_frase_tarot_curta, gerar_frase_tarot_longa
    _HAVE_TAROT_FUNCS = True
except Exception: _HAVE_TAROT_FUNCS = False
from utils.imagem import escrever_frase_na_imagem, gerar_imagem_com_frase
try:
    from utils.imagem import gerar_imagem_dalle
    _HAVE_DALLE_FUNC = True
except Exception: _HAVE_DALLE_FUNC = False
from utils.video import gerar_video
from utils.tiktok import postar_no_tiktok_e_renomear
try:
    from utils.veo3 import executar_interativo as veo3_executar_interativo, postar_em_intervalo as veo3_postar_em_intervalo
    _HAVE_VEO3 = True
except Exception: _HAVE_VEO3 = False
try:
    from utils.vpn_manager import setup_vpn, is_vpn_setup_complete
    _HAVE_VPN = True
except ImportError:
    _HAVE_VPN = False

# ====== Constantes/UI ======
STYLE_OPTIONS = {
    "1": ("classic", "Clássico legível (Montserrat/Inter, branco + stroke)"), "2": ("modern", "Moderno (Bebas/Alta, caps, discreto)"),
    "3": ("serif", "Elegante (Playfair/Cinzel, leve dourado)"), "4": ("mono", "Monoespaçada minimalista"), "5": ("clean", "Clean sem stroke (peso médio)"),
}
MOTION_OPTIONS = {
    "1": ("none", "Sem movimento"), "2": ("kenburns_in", "Zoom in (Ken Burns)"), "3": ("kenburns_out", "Zoom out (Ken Burns)"),
    "4": ("pan_lr", "Pan left–right"), "5": ("pan_ud", "Pan up–down"), "0": ("random", "Aleatório (entre movimentos)"),
}

def _int_env(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        return int(v) if v is not None and str(v).strip() != "" else default
    except Exception: return default

DEFAULT_SLIDES_COUNT, SLIDES_MIN, SLIDES_MAX = _int_env("SLIDES_COUNT", 4), _int_env("SLIDES_MIN", 3), _int_env("SLIDES_MAX", 10)
IMAGENS_DIR, VIDEOS_DIR, AUDIOS_DIR = "imagens", "videos", "audios"
MAX_RETRIES, _BACK_TOKENS = 3, {"b", "voltar", "back"}
_WPM = {"en": 155.0, "pt-br": 150.0, "pt": 150.0, "ar-eg": 130.0, "ar": 130.0, "ru": 140.0, "id": 155.0}

def _estimativa_duracao_segundos(texto: str, idioma: str) -> float:
    if not texto: return 0.0
    return max(5.0, (len(texto.split()) / _WPM.get(idioma.lower(), 150.0)) * 60.0)

def _reload_env_if_changed(force: bool = False) -> bool:
    global _ENV_MTIME
    if not _ENV_PATH: return False
    try: mtime = os.path.getmtime(_ENV_PATH)
    except OSError: mtime = None
    changed = force or ((_ENV_MTIME is None and mtime is not None) or (mtime is not None and _ENV_MTIME is not None and mtime > _ENV_MTIME))
    if changed:
        load_dotenv(_ENV_PATH, override=True)
        _ENV_MTIME = mtime
        logger.info(".env recarregado.")
    return changed

def _menu_modo_execucao() -> str:
    while True:
        print("\nO que você deseja fazer?\n1. Postar agora (uma vez) *padrão\n2. Postar automaticamente a cada X horas")
        op = input("Escolha 1 ou 2: ").strip().lower()
        if op in {"", "1"}: return "1"
        if op == "2": return "2"

def _selecionar_modo_output() -> dict:
    while True:
        print("\nSelecione o modo de saída do vídeo:")
        print("1. Gerar e postar normalmente [padrão]")
        print("2. Só gerar o arquivo e salvar numa pasta personalizada")
        op = input("Escolha 1 ou 2: ").strip().lower()
        if op in {"", "1"}:
            return {"modo": "postar", "custom_pasta": None}
        elif op == "2":
            while True:
                custom_pasta = input("Cole o caminho COMPLETO da pasta para salvar o arquivo (ex: C:\\Users\\vinic\\OneDrive\\Documentos\\MuMuSharedFolder\\Camera):\n").strip()
                if not custom_pasta: print("Por favor, preencha o caminho da pasta. Tente novamente!")
                else: return {"modo": "salvar", "custom_pasta": custom_pasta}

def _selecionar_idioma() -> Optional[str]:
    while True:
        print("\nEscolha o país para referência do idioma das mensagens:\n1. EUA (Inglês) *padrão\n2. Brasil (pt-br)\n3. Árabe (egípcio)\n4. Rússia (Russo)\n5. Indonésia (Indonésio)\nb. Voltar")
        op = input("Digite a opção: ").strip().lower()
        if op in _BACK_TOKENS: return None
        if op in {"", "1"}: return "en"
        if op == "2": return "pt-br"
        if op == "3": return "ar-eg"
        if op == "4": return "ru"
        if op == "5": return "id"

def _submenu_conteudo_por_idioma(idioma: str):
    is_ar, is_pt, is_ru, is_id = idioma == "ar-eg", idioma == "pt-br", idioma == "ru", idioma == "id"
    options = {"1": ("motivacional", None), "2": ("tarot", None)}
    if is_ar: print("\nSelecione o conteúdo (Árabe):"); options["3"] = ("veo3", "yasmina") if _HAVE_VEO3 else None
    elif is_pt: print("\nSelecione o conteúdo (Brasil):"); options["3"] = ("veo3", "luisa") if _HAVE_VEO3 else None
    elif is_ru: print("\nSelecione o conteúdo (Rússia):"); options["3"] = ("veo3", "alina") if _HAVE_VEO3 else None
    elif is_id: print("\nSelecione o conteúdo (Indonésia):"); options["3"] = ("veo3", "ayu") if _HAVE_VEO3 else None
    else: print("\nSelecione o conteúdo (EUA):")
    print("1. Motivacional\n2. Cartomante")
    if "3" in options and options["3"]: print(f"3. {options['3'][0].capitalize()} ({options['3'][1].capitalize()})")
    print("b. Voltar")
    op = input("Digite a opção: ").strip().lower()
    if op in _BACK_TOKENS: return None
    return options.get(op, options["1"])

def _ler_intervalo_horas() -> Optional[float]:
    while True:
        raw = input("De quantas em quantas horas? (ex.: 3 ou 1.5)  [b=voltar]: ").strip().replace(",", ".").lower()
        if raw in _BACK_TOKENS: return None
        try: return float(raw)
        except Exception: print("Valor inválido.")

def _selecionar_tts_engine() -> Optional[str]:
    while True:
        print("\nEscolha o mecanismo de voz (TTS):\n1. Gemini (padrão)\n2. ElevenLabs\nb. Voltar")
        op = input("Escolha: ").strip().lower()
        if op in _BACK_TOKENS: return None
        return "elevenlabs" if op == "2" else "gemini"

def _selecionar_legendas() -> Optional[bool]:
    while True:
        print("\nDeseja adicionar legendas sincronizadas no vídeo?\n1. Sim *padrão\n2. Não\nb. Voltar")
        op = input("Escolha: ").strip().lower()
        if op in _BACK_TOKENS: return None
        return op != "2"

def _selecionar_estilo_video() -> Optional[str]:
    while True:
        print("\nEscolha o estilo do vídeo (legendas/typography):\n0. Aleatório *padrão")
        for k, (_, desc) in STYLE_OPTIONS.items(): print(f"{k}. {desc}")
        print("b. Voltar")
        op = input("Opção: ").strip().lower()
        if op in _BACK_TOKENS: return None
        if op in STYLE_OPTIONS: return STYLE_OPTIONS[op][0]
        if op in {"", "0"}:
            escolha = random.choice(list(STYLE_OPTIONS.values()))[0]; print(f"Estilo sorteado: {escolha}")
            return escolha

def _selecionar_motion(env_default: str) -> Optional[str]:
    while True:
        print(f"\nMovimento do vídeo:\n(Enter para manter do .env: '{env_default}')")
        for k, (key, desc) in MOTION_OPTIONS.items(): print(f"{k}. {desc}")
        print("b. Voltar")
        op = input("Opção: ").strip().lower()
        if op in _BACK_TOKENS: return None
        if op == "": return env_default
        if op == "0":
            escolha = random.choice([v[0] for v in MOTION_OPTIONS.values() if v[0] not in ("none", "random")])
            print(f"Movimento sorteado: {escolha}"); return escolha
        if op in MOTION_OPTIONS: return MOTION_OPTIONS[op][0]

def _perguntar_headless(label: str, default_on: bool) -> Optional[bool]:
    padrao = 'Sim' if default_on else 'Não'
    print(f"\nExecutar {label} em modo headless?\nEnter = {padrao}  |  1. Sim  |  2. Não  |  b. Voltar")
    op = input("Escolha: ").strip().lower()
    if op in _BACK_TOKENS: return None
    if op in {'1', 'sim', 's'}: return True
    if op in {'2', 'nao', 'não', 'n'}: return False
    return default_on

def _selecionar_gerador_imagens(padrao: str) -> Optional[str]:
    while True:
        print(f"\nSelecione qual o mecanismo de geração de imagens:\n1. Pexels (Fotos Reais)\n2. DALL-E / ChatGPT (Geradas por IA)\nEnter = usar .env (IMAGE_MODE={padrao}) | b = voltar")
        raw = input("Digite 1 ou 2: ").strip().lower()
        if raw in _BACK_TOKENS: return None
        if raw == "": print(f" ⇒ Usando padrão: {padrao}"); return padrao
        if raw == "1": print(" ⇒ Selecionado: Pexels"); return "pexels"
        if raw == "2":
            if not _HAVE_DALLE_FUNC: print("   AVISO: 'gerar_imagem_dalle' não está implementado.")
            print(" ⇒ Selecionado: DALL-E / ChatGPT")
            ans = _perguntar_headless('o ChatGPT/DALL-E (imagens)', os.getenv('CHATGPT_HEADLESS', '0').strip() != '0')
            if ans is None: return None
            os.environ['CHATGPT_HEADLESS'] = '1' if ans else '0'; print(f" ⇒ ChatGPT/DALL-E headless: {'ON' if ans else 'OFF'}")
            return "dalle"

def _map_video_style_to_image_template(style_key: str) -> str:
    s = (style_key or "").lower()
    if s in ("classic", "1", "serif", "3"): return "classic_serif"
    if s in ("clean", "5", "modern", "2"): return "modern_block"
    if s in ("mono", "4"): return "minimal_center"
    return "minimal_center"

def rotina(modo_conteudo: str, idioma: str, tts_engine: str, legendas: bool, video_style: str, motion: str, image_engine: str, use_vpn: bool, headless_tiktok: bool, apenas_salvar: bool = False) -> Optional[str]:
    """
    Executa a rotina principal de geração de vídeo.
    Se 'apenas_salvar' for True, gera o vídeo e retorna o caminho do arquivo sem postar.
    """
    os.makedirs(IMAGENS_DIR, exist_ok=True); os.makedirs(VIDEOS_DIR, exist_ok=True); os.makedirs(AUDIOS_DIR, exist_ok=True)
    logger.info("Gerando conteúdos (%s | modo=%s | imagens=%s)...", idioma, modo_conteudo, image_engine)
    
    if modo_conteudo == "tarot" and _HAVE_TAROT_FUNCS:
        tema_imagem, frase = gerar_prompt_tarot(idioma), gerar_frase_tarot_curta(idioma)
    else:
        tema_imagem = frase = gerar_frase_motivacional(idioma)
    
    try:
        hashtags_list = gerar_hashtags_virais(frase, idioma=idioma, n=3)
        desc_tiktok = (frase + " " + " ".join(hashtags_list)).strip()
    except Exception as e:
        logger.warning("Falha ao gerar hashtags: %s", e); desc_tiktok = frase
    
    long_text = None
    if _HAVE_TAROT_FUNCS and modo_conteudo == "tarot":
        try: long_text = gerar_frase_tarot_longa(idioma)
        except Exception: pass
    if not long_text and _HAVE_LONGO_MOTIV:
        try: long_text = gerar_frase_motivacional_longa(idioma)
        except Exception: pass
    
    dur_est = _estimativa_duracao_segundos(long_text, idioma) if long_text else 30.0
    slides_auto = int(round(dur_est / float(os.getenv("SLIDES_SECONDS_PER", "3.0")))) if float(os.getenv("SLIDES_SECONDS_PER", "3.0")) > 0 else DEFAULT_SLIDES_COUNT
    slides_count = max(SLIDES_MIN, min(SLIDES_MAX, slides_auto))
    slug_frase = gerar_slug(frase)
    video_final = os.path.join(VIDEOS_DIR, f"{slug_frase}.mp4")
    
    logger.info(f"Gerando {slides_count} prompts de imagem com o tema: '{tema_imagem}'")
    prompts_de_imagem = gerar_prompts_de_imagem_variados(tema_imagem, slides_count, idioma)
    
    generated_image_paths = []
    if image_engine == 'dalle': pass
    else:
        for i, img_prompt in enumerate(prompts_de_imagem):
            imagem_path = os.path.join(IMAGENS_DIR, f"{slug_frase}_slide_{i+1:02d}.png")
            try:
                gerar_imagem_com_frase(prompt=img_prompt, arquivo_saida=imagem_path, idioma=idioma)
                if os.path.exists(imagem_path) and os.path.getsize(imagem_path) > 1024:
                    generated_image_paths.append(imagem_path)
            except Exception as e: logger.error(f"Falha ao gerar a imagem {i+1}: {e}")

    if not generated_image_paths: raise RuntimeError("Nenhuma imagem foi gerada.")
    
    slides_para_video = []
    template_img = _map_video_style_to_image_template(video_style)
    for img_path in generated_image_paths:
        nome_base = os.path.splitext(os.path.basename(img_path))[0]
        out_path = os.path.join(IMAGENS_DIR, f"{nome_base}_com_texto.png")
        escrever_frase_na_imagem(imagem_path=img_path, frase=frase, saida_path=out_path, template=template_img, idioma=idioma)
        slides_para_video.append(out_path)
        
    gerar_video(imagem_path=slides_para_video[0], saida_path=video_final, preset="fullhd", idioma=idioma, tts_engine=tts_engine, legendas=legendas, video_style=video_style, motion=motion, slides_paths=slides_para_video, content_mode=modo_conteudo, long_text=long_text)
    
    if apenas_salvar:
        logger.info(f"✓ Vídeo gerado com sucesso em '{video_final}'. Pulando etapa de postagem.")
        return video_final
    else:
        ok = postar_no_tiktok_e_renomear(descricao_personalizada=desc_tiktok, video_final=video_final, idioma=idioma, use_vpn=use_vpn, headless=headless_tiktok)
        if ok:
            logger.info("✓ Processo concluído com sucesso!")
        else:
            logger.error("✗ Falha na postagem. Verifique os logs!")
        return None
    
ITERATION_TIMEOUT_MIN = float(os.getenv("ITERATION_TIMEOUT_MIN", "12.0"))
def _run_rotina_once(args_tuple):
    try: rotina(*args_tuple); return True
    except Exception as e: logging.exception("Falha na execução única: %s", e); return False
def _executar_com_timeout(args_tuple) -> Union[bool, None]:
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as ex:
        fut = ex.submit(_run_rotina_once, args_tuple)
        try: return bool(fut.result(timeout=int(ITERATION_TIMEOUT_MIN * 60)))
        except TimeoutError: logging.error("✗ Iteração excedeu %.1f min — abortando.", ITERATION_TIMEOUT_MIN); return None
def postar_em_intervalo(cada_horas: float, **kwargs):
    logger.info(f"⌛ Modo automático base: {cada_horas:.2f} h (Ctrl+C para parar).")
    try:
        while True:
            _reload_env_if_changed()
            inicio = datetime.now()
            logger.info("⏳ Nova execução (%s).", inicio.strftime('%d/%m %H:%M:%S'))
            _executar_com_timeout(tuple(kwargs.values()))
            proxima = inicio + timedelta(hours=cada_horas)
            rem = max(0.0, (proxima - datetime.now()).total_seconds())
            logger.info("Próxima execução em %.2f horas...", rem / 3600.0)
            time.sleep(rem)
    except KeyboardInterrupt: logger.info("⏹️ Encerrado pelo usuário.")

def _limpar_pasta(destino: str):
    try:
        if not os.path.exists(destino):
            os.makedirs(destino)
        for f in os.listdir(destino):
            caminho = os.path.join(destino, f)
            if os.path.isfile(caminho):
                os.remove(caminho)
    except Exception as e:
        logger.warning(f"Falha ao limpar pasta '{destino}': {e}")

def _run_rotina_salvar_once(custom_pasta: str, **kwargs):
    kwargs["apenas_salvar"] = True
    caminho_gerado = rotina(**kwargs)
    if caminho_gerado and os.path.exists(caminho_gerado):
        try:
            nome_arquivo = os.path.basename(caminho_gerado)
            _limpar_pasta(custom_pasta)
            destino_final = os.path.join(custom_pasta, nome_arquivo)
            shutil.copy2(caminho_gerado, destino_final)
            logger.info(f"✓ Vídeo salvo com sucesso em '{destino_final}' (pasta de destino limpa antes).")
            return True
        except Exception as e:
            logger.error(f"✗ Falha ao salvar o vídeo na pasta personalizada: {e}")
            return False
    else:
        logger.error("✗ Falha na geração do vídeo, arquivo não encontrado para salvar.")
        return False

def _executar_salvamento_com_timeout(args_tuple):
    try:
        custom_pasta, kwargs_dict = args_tuple
        return _run_rotina_salvar_once(custom_pasta, **kwargs_dict)
    except Exception as e:
        logging.exception("Falha na execução única de salvamento: %s", e)
        return False

def salvar_em_intervalo(cada_horas: float, custom_pasta: str, **kwargs):
    logger.info(f"⌛ Modo automático de SALVAMENTO: a cada {cada_horas:.2f} h em '{custom_pasta}' (Ctrl+C para parar).")
    try:
        while True:
            _reload_env_if_changed()
            inicio = datetime.now()
            logger.info("⏳ Nova geração de vídeo (%s).", inicio.strftime('%d/%m %H:%M:%S'))
            args_para_execucao = (custom_pasta, kwargs)
            ctx = multiprocessing.get_context("spawn")
            with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as ex:
                fut = ex.submit(_executar_salvamento_com_timeout, args_para_execucao)
                try:
                    fut.result(timeout=int(ITERATION_TIMEOUT_MIN * 60))
                except TimeoutError:
                    logging.error("✗ Iteração de salvamento excedeu %.1f min — abortando.", ITERATION_TIMEOUT_MIN)
            proxima = inicio + timedelta(hours=cada_horas)
            rem = max(0.0, (proxima - datetime.now()).total_seconds())
            logger.info("Próxima geração de vídeo em %.2f horas...", rem / 3600.0)
            time.sleep(rem)
    except KeyboardInterrupt:
        logger.info("⏹️ Encerrado pelo usuário.")

def _menu_principal():
    _reload_env_if_changed(force=False)
    while True:
        modo = _menu_modo_execucao()
        _reload_env_if_changed(force=False)
        idioma = _selecionar_idioma()
        if idioma is None: continue
        op_output = _selecionar_modo_output()
        use_vpn = False
        ans_tt = False
        horas = 0
        if op_output["modo"] == "postar":
            idioma_selecionado = idioma.lower().strip()
            provider = os.getenv("VPN_PROVIDER", "none").lower()
            vpn_langs_str = ""
            if provider == 'urban': vpn_langs_str = os.getenv('URBANVPN_LANGS', '')
            elif provider == 'zoog': vpn_langs_str = os.getenv('ZOOGVPN_LANGS', '')
            vpn_langs = [lang.strip().lower() for lang in vpn_langs_str.split(',') if lang.strip()]
            use_vpn = (_HAVE_VPN and provider in ('urban', 'zoog') and idioma_selecionado in vpn_langs)
            if use_vpn:
                os.environ['HEADLESS_UPLOAD'] = '0'
                logger.info(f"ℹ️ VPN ({provider.upper()}) necessária. O modo Headless para o TikTok foi desativado automaticamente.")
            else:
                default_tt_headless = os.getenv('HEADLESS_UPLOAD', '0').strip() != '0'
                ans_tt_resp = _perguntar_headless('o TikTok (upload)', default_tt_headless)
                if ans_tt_resp is None: continue
                ans_tt = ans_tt_resp
                os.environ['HEADLESS_UPLOAD'] = '1' if ans_tt else '0'
        
        if modo == "2":
             horas = _ler_intervalo_horas()
             if horas is None: continue
        
        conteudo = _submenu_conteudo_por_idioma(idioma)
        if conteudo is None: continue
        
        if conteudo[0] == "veo3":
            if modo == "2":
                if horas is not None: veo3_postar_em_intervalo(persona=conteudo[1], idioma=idioma, cada_horas=horas, use_vpn=use_vpn)
            else:
                veo3_executar_interativo(persona=conteudo[1], idioma=idioma, use_vpn=use_vpn)
            continue
        
        tts_engine = _selecionar_tts_engine()
        if tts_engine is None: continue
        legendas = _selecionar_legendas()
        if legendas is None: continue
        video_style = _selecionar_estilo_video()
        if video_style is None: continue
        motion = _selecionar_motion(env_default=os.getenv("MOTION", "kenburns_in"))
        if motion is None: continue
        image_engine = _selecionar_gerador_imagens(os.getenv("IMAGE_MODE", "pexels"))
        if image_engine is None: continue
        headless_tiktok = not use_vpn and ans_tt if not use_vpn else False
        kwargs = {
            "modo_conteudo": conteudo[0], "idioma": idioma, "tts_engine": tts_engine, "legendas": legendas,
            "video_style": video_style, "motion": motion, "image_engine": image_engine, "use_vpn": use_vpn,
            "headless_tiktok": headless_tiktok
        }
        if op_output["modo"] == "salvar":
            if modo == "2":
                salvar_em_intervalo(cada_horas=horas, custom_pasta=op_output["custom_pasta"], **kwargs)
            else:
                _run_rotina_salvar_once(custom_pasta=op_output["custom_pasta"], **kwargs)
        else:
            if modo == "2":
                postar_em_intervalo(cada_horas=horas, **kwargs)
            else:
                rotina(**kwargs)
                              
if __name__ == "__main__":
    try:
        multiprocessing.freeze_support()
    except Exception:
        pass
    _reload_env_if_changed(force=True)
    if _HAVE_VPN:
        provider = os.getenv("VPN_PROVIDER", "none").lower()
        if provider in ('urban', 'zoog'):
            profile_name = None
            if provider == 'urban': profile_name = os.getenv("URBANVPN_PROFILE_NAME")
            elif provider == 'zoog': profile_name = os.getenv("ZOOGVPN_PROFILE_NAME")
            if profile_name:
                if not is_vpn_setup_complete(profile_name):
                    logger.warning("="*50); logger.warning(f"‼️ ATENÇÃO: Configuração da {provider.upper()} VPN necessária (primeira execução)."); logger.warning("="*50)
                    setup_vpn()
                    logger.info(f"Setup da {provider.upper()} VPN concluído. Por favor, reinicie o script para continuar.")
                    sys.exit(0)
            else:
                logger.error(f"VPN_PROVIDER='{provider}' mas o PROFILE_NAME correspondente não está definido no .env!")
    
    _menu_principal()