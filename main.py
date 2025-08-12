import os
from utils.frase import gerar_prompt_paisagem, gerar_frase_motivacional, gerar_slug
from utils.imagem import gerar_imagem_com_frase, escrever_frase_na_imagem
from utils.video import gerar_video
from utils.tiktok import postar_no_tiktok_e_renomear  # Novo import para upload no TikTok
import logging

# Nova escolha de idioma/país no início
print("\nEscolha o país para referência da língua das mensagens:")
print("1. EUA (Inglês) *padrão")
print("2. Brasil (pt-br)")
idioma = input("Digite o número da opção (1 ou 2): ")
if idioma not in ['1', '2']:
    print("Opção inválida! Usando EUA (Inglês) como padrão.")
    idioma = '1'
idioma = 'en' if idioma == '1' else 'pt-br'

def verificar_videos_pendentes(pasta="videos"):
    """
    Verifica se há vídeos criados na pasta.
    Retorna True se houver vídeos (pendentes ou não), False se não.
    """
    try:
        arquivos = [f for f in os.listdir(pasta) if f.endswith(".mp4")]
        if arquivos:
            logger.info(f"⚠️ Vídeos encontrados: {', '.join(arquivos)}")
            logger.info("Pulando geração de novo conteúdo e postando o último.")
            return True  # Há vídeos
        return False  # Nenhum vídeo
    except Exception as e:
        logger.error(f"❌ Erro ao verificar vídeos: {e}")
        return False

def rotina():
    pendentes = verificar_videos_pendentes()

    if not pendentes:
        # Garantir que as pastas existam
        os.makedirs("imagens", exist_ok=True)
        os.makedirs("videos", exist_ok=True)
        os.makedirs("audios", exist_ok=True)  # Garantir que a pasta de áudios exista

        # Gerar prompt e frase
        prompt_imagem = gerar_prompt_paisagem(idioma)
        frase = gerar_frase_motivacional(idioma)

        # Slugs para nome de arquivos
        slug_imagem = gerar_slug(prompt_imagem)
        slug_frase = gerar_slug(frase)
        imagem_base = f"imagens/{slug_imagem}.jpg"
        imagem_final = f"imagens/{slug_frase}.jpg"
        video_final = f"videos/{slug_frase}.mp4"

        # Gerar imagem com IA ou Pexels (ver variável no imagem.py)
        gerar_imagem_com_frase(prompt=prompt_imagem, arquivo_saida=imagem_base)

        # Escrever frase na imagem
        escrever_frase_na_imagem(imagem_base, frase, imagem_final)

        # Criar vídeo com música
        gerar_video(imagem_final, video_final)

        # Postar no TikTok, passando os caminhos dos arquivos
        postar_no_tiktok_e_renomear(descricao_personalizada=frase, imagem_base=imagem_base, imagem_final=imagem_final, video_final=video_final)

    else:
        # Para vídeos existentes, usar o último vídeo e uma descrição padrão
        video_path = obter_ultimo_video()
        if video_path:
            postar_no_tiktok_e_renomear(descricao_personalizada="Conteúdo motivacional pendente! #Motivacao #Inspiracao #TikTokMotivacional", video_final=video_path)

    logger.info("✅ Tudo pronto!")

def obter_ultimo_video(pasta="videos"):
    """
    Encontra o vídeo mais recente na pasta de vídeos (baseado na data de modificação).
    """
    try:
        arquivos = [os.path.join(pasta, f) for f in os.listdir(pasta) if f.endswith(".mp4")]
        if not arquivos:
            raise FileNotFoundError(f"❌ Nenhum vídeo encontrado em {pasta}.")
        
        ultimo_video = max(arquivos, key=os.path.getmtime)
        logger.info("📹 Último vídeo encontrado: %s", ultimo_video)
        return ultimo_video
    except Exception as e:
        logger.error("❌ Erro ao buscar último vídeo: %s", str(e))
        return None

# Configuração do logging com timestamps (movida para o início)
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    rotina()