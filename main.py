# main.py

import os
import time
import logging
from datetime import datetime, timedelta

from utils.frase import gerar_prompt_paisagem, gerar_frase_motivacional, gerar_frase_motivacional_longa, gerar_slug
from utils.imagem import gerar_imagem_com_frase, escrever_frase_na_imagem
from utils.video import gerar_video
from utils.tiktok import postar_no_tiktok_e_renomear

# -----------------------------------------------------------------------------
# Logging com timestamps
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Auxiliares
# -----------------------------------------------------------------------------
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
    """
    Retorna:
      '1' -> postar uma vez agora
      '2' -> postar automaticamente a cada X horas
    """
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
            print("Valor inválido. Digite um número maior que zero (ex.: 2, 2.5, 0.5).")

# -----------------------------------------------------------------------------
# Núcleo da rotina de criação e postagem
# -----------------------------------------------------------------------------
def rotina(idioma: str):
    """
    1) Gera prompt + frase
    2) Baixa/gera imagem base
    3) Escreve a frase na imagem
    4) Gera vídeo (preset='hd' rápido/compatível)
    5) Posta no TikTok conforme idioma (cookies + proxy EN / direto BR)
    """
    # Garante pastas
    os.makedirs("imagens", exist_ok=True)
    os.makedirs("videos", exist_ok=True)
    os.makedirs("audios", exist_ok=True)

    # Gerar prompt e frase
    logger.info("Gerando conteúdos (%s)...", idioma)
    prompt_imagem = gerar_prompt_paisagem(idioma)
    frase = gerar_frase_motivacional(idioma)
    #frase_longa = gerar_frase_motivacional_longa(idioma)

    # Slugs e caminhos
    slug_imagem = gerar_slug(prompt_imagem)
    slug_frase = gerar_slug(frase)
    imagem_base = f"imagens/{slug_imagem}.jpg"
    imagem_final = f"imagens/{slug_frase}.jpg"
    video_final = f"videos/{slug_frase}.mp4"

    # Gerar imagem + sobrepor frase
    gerar_imagem_com_frase(prompt=prompt_imagem, arquivo_saida=imagem_base)
    escrever_frase_na_imagem(imagem_base, frase, imagem_final)

    # Criar vídeo (preset rápido/compatível) — você pode trocar para preset="ultra" se quiser mais qualidade
    gerar_video(imagem_final, video_final, preset="hd", idioma=idioma)

    # Postar no TikTok, passando o idioma para decidir cookies/hashtags/proxy
    postar_no_tiktok_e_renomear(
        descricao_personalizada=frase,
        imagem_base=imagem_base,
        imagem_final=imagem_final,
        video_final=video_final,
        idioma=idioma
    )

    logger.info("✅ Execução concluída.")

# -----------------------------------------------------------------------------
# Modo automático (loop)
# -----------------------------------------------------------------------------
def postar_em_intervalo(cada_horas: float, idioma: str):
    """
    Executa a rotina de tempos em tempos (cada_horas).
    Para com Ctrl+C.
    """
    logger.info("⏱️ Modo automático ativado: a cada %.2f horas (Ctrl+C para parar).", cada_horas)
    try:
        while True:
            inicio = datetime.now()
            logger.info("🟢 Nova execução iniciada (%s).", inicio.strftime("%d/%m %H:%M:%S"))

            try:
                rotina(idioma)
            except Exception as e:
                logger.exception("❌ Erro durante a execução: %s", e)

            proxima = inicio + timedelta(hours=cada_horas)
            # tempo restante até próxima execução
            restante = (proxima - datetime.now()).total_seconds()
            if restante <= 0:
                # Se atrasou (execução longa), inicia imediatamente a próxima
                continue

            # Dorme em pedaços de 60s para permitir Ctrl+C responsivo
            logger.info("⏳ Próxima execução em ~%.0f min.", restante / 60)
            while restante > 0:
                dur = min(60, restante)
                time.sleep(dur)
                restante -= dur
    except KeyboardInterrupt:
        logger.info("🛑 Modo automático interrompido pelo usuário.")

# -----------------------------------------------------------------------------
# Entrada do programa
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    idioma = _selecionar_idioma()
    modo = _menu_modo_execucao()

    if modo == "1":
        rotina(idioma)
    else:
        intervalo = _ler_intervalo_horas()
        postar_em_intervalo(intervalo, idioma)
