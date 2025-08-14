# main.py

import os
import time
import logging
from datetime import datetime, timedelta
import random

from utils.frase import gerar_prompt_paisagem, gerar_frase_motivacional, gerar_slug
from utils.imagem import gerar_imagem_com_frase, escrever_frase_na_imagem
from utils.video import gerar_video
from utils.tiktok import postar_no_tiktok_e_renomear

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# --- estilos exibidos no menu (devem existir no utils/video.py) ---
STYLE_OPTIONS = {
    "1": ("classic", "Clássico legível (Montserrat/Inter, branco + stroke)"),
    "2": ("modern",  "Modernão (Bebas/Alta, caps, discreto)"),
    "3": ("serif",   "Elegante (Playfair/Cinzel, leve dourado)"),
    "4": ("mono",    "Monoespaçada minimalista"),
    "5": ("clean",   "Clean sem stroke (peso médio)")
}

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
    if op not in ("1", "2"):
        print("Opção inválida! Usando modo 1 (postar agora).")
        op = "1"
    return op

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
    """
    Retorna 'gemini' (padrão) ou 'elevenlabs'
    """
    print("\nEscolha o mecanismo de voz (TTS):")
    print("1. Gemini (padrão)")
    print("2. ElevenLabs")
    op = input("Escolha 1 ou 2: ").strip()
    if op == "2":
        return "elevenlabs"
    return "gemini"

def _selecionar_legendas() -> bool:
    """
    Pergunta se deseja queimar legendas no vídeo (Sim/Não).
    """
    print("\nDeseja adicionar legendas sincronizadas no vídeo?")
    print("1. Sim (padrão)")
    print("2. Não")
    op = input("Escolha 1 ou 2: ").strip()
    if op == "2":
        return False
    return True

def _selecionar_estilo_video() -> str:
    """
    Escolhe o estilo visual do vídeo (afeta tipografia/cor das legendas).
    Retorna a chave de estilo usada em utils/video.py.
    """
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

def _map_video_style_to_image_template(style_key: str) -> str:
    """
    Converte o estilo escolhido para o template de tipografia da IMAGEM.
    Templates disponíveis: 'classic_serif' | 'modern_block' | 'minimal_center'
    """
    s = (style_key or "").lower()
    if s in ("classic", "clean", "1", "5"):
        # Montserrat/Inter com blocos fortes (legível)
        return "modern_block"
    if s in ("serif", "3"):
        # Playfair/Cinzel (elegante)
        return "classic_serif"
    if s in ("mono", "4"):
        # por enquanto usa minimal_center (Bebas + Montserrat) como base
        return "minimal_center"
    # modern (Bebas caps)
    return "minimal_center"

def rotina(idioma: str, tts_engine: str, legendas: bool, video_style: str):
    os.makedirs("imagens", exist_ok=True)
    os.makedirs("videos", exist_ok=True)
    os.makedirs("audios", exist_ok=True)

    logger.info("Gerando conteúdos (%s)...", idioma)
    prompt_imagem = gerar_prompt_paisagem(idioma)
    frase = gerar_frase_motivacional(idioma)

    slug_imagem = gerar_slug(prompt_imagem)
    slug_frase = gerar_slug(frase)
    imagem_base = f"imagens/{slug_imagem}.jpg"
    imagem_final = f"imagens/{slug_frase}.jpg"
    video_final = f"videos/{slug_frase}.mp4"

    gerar_imagem_com_frase(prompt=prompt_imagem, arquivo_saida=imagem_base)

    # >>> aplica o mesmo "estilo" visual da legenda também na tipografia da imagem
    template_img = _map_video_style_to_image_template(video_style)
    logger.info("🖌️ Template da imagem selecionado: %s (derivado de '%s')", template_img, video_style)
    escrever_frase_na_imagem(imagem_base, frase, imagem_final, template=template_img, idioma=idioma)

    # Passa novas opções para o gerador de vídeo
    gerar_video(
        imagem_final,
        video_final,
        preset="hd",
        idioma=idioma,
        tts_engine=tts_engine,
        legendas=legendas,
        video_style=video_style,
    )

    postar_no_tiktok_e_renomear(
        descricao_personalizada=frase,
        imagem_base=imagem_base,
        imagem_final=imagem_final,
        video_final=video_final,
        idioma=idioma
    )
    logger.info("✅ Execução concluída.")

def postar_em_intervalo(cada_horas: float, idioma: str, tts_engine: str, legendas: bool, video_style: str):
    logger.info("⏱️ Modo automático: a cada %.2f horas (Ctrl+C para parar).", cada_horas)
    try:
        while True:
            inicio = datetime.now()
            logger.info("🟢 Nova execução (%s).", inicio.strftime("%d/%m %H:%M:%S"))
            try:
                rotina(idioma, tts_engine, legendas, video_style)
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
    idioma = _selecionar_idioma()
    modo = _menu_modo_execucao()
    tts_engine = _selecionar_tts_engine()
    legendas = _selecionar_legendas()
    video_style = _selecionar_estilo_video()

    if modo == "1":
        rotina(idioma, tts_engine, legendas, video_style)
    else:
        intervalo = _ler_intervalo_horas()
        postar_em_intervalo(intervalo, idioma, tts_engine, legendas, video_style)