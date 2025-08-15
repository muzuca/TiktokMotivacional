# main.py
import os
import time
import logging
from datetime import datetime, timedelta
import random
from dotenv import load_dotenv

from utils.frase import gerar_prompt_paisagem, gerar_frase_motivacional, gerar_slug
from utils.imagem import gerar_imagem_com_frase, escrever_frase_na_imagem, montar_slides_pexels
from utils.video import gerar_video
from utils.tiktok import postar_no_tiktok_e_renomear

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

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
    s = (style_key or "").lower()
    if s in ("classic", "clean", "1", "5"):
        return "modern_block"
    if s in ("serif", "3"):
        return "classic_serif"
    if s in ("mono", "4"):
        return "minimal_center"
    return "minimal_center"

def rotina(idioma: str, tts_engine: str, legendas: bool, video_style: str, motion: str, slides_count: int):
    os.makedirs("imagens", exist_ok=True)
    os.makedirs("videos", exist_ok=True)
    os.makedirs("audios", exist_ok=True)

    logger.info("Gerando conteúdos (%s)...", idioma)
    prompt_imagem = gerar_prompt_paisagem(idioma)
    frase = gerar_frase_motivacional(idioma)

    slug_img = gerar_slug(prompt_imagem)
    slug_frase = gerar_slug(frase)
    imagem_base = f"imagens/{slug_img}.jpg"
    imagem_capa = f"imagens/{slug_frase}.jpg"
    video_final = f"videos/{slug_frase}.mp4"

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

    slides_txt_dir = os.path.join("imagens", "slides_txt")
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

    # 4) TikTok
    postar_no_tiktok_e_renomear(
        descricao_personalizada=frase,
        imagem_base=imagem_base,
        imagem_final=imagem_capa,
        video_final=video_final,
        idioma=idioma
    )
    logger.info("✅ Execução concluída.")

def postar_em_intervalo(cada_horas: float, idioma: str, tts_engine: str, legendas: bool, video_style: str, motion: str, slides_count: int):
    logger.info("⏱️ Modo automático: a cada %.2f horas (Ctrl+C para parar).", cada_horas)
    try:
        while True:
            inicio = datetime.now()
            logger.info("🟢 Nova execução (%s).", inicio.strftime("%d/%m %H:%M:%S"))
            try:
                rotina(idioma, tts_engine, legendas, video_style, motion, slides_count)
            except Exception as e:
                logger.exception("❌ Erro durante a execução: %s", e)

            proxima = inicio + timedelta(hours=cada_horas)
            restante = (proxima - datetime.now()).total_seconds()
            if restante <= 0:
                continue
            logger.info("⏳ Próxima execução em ~%.0f min.", restante / 60)
            while restante > 0:
                dur = min(60, restante)
                time.sleep(dur)
                restante -= dur
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

    if modo == "1":
        rotina(idioma, tts_engine, legendas, video_style, motion, slides_count)
    else:
        intervalo = _ler_intervalo_horas()
        postar_em_intervalo(intervalo, idioma, tts_engine, legendas, video_style, motion, slides_count)
