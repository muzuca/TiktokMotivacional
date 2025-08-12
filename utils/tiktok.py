import os
import time  # Para delays
from datetime import datetime, timedelta
from .tiktok_uploader.upload import upload_video  # Import local da pasta tiktok_uploader
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from socket import error as SocketError  # Para ConnectionResetError
import logging

# Configuração do logging com timestamps
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'  # Formato de horário (HH:MM:SS)
)
logger = logging.getLogger(__name__)

PASTA_VIDEOS = "videos"
PASTA_IMAGENS = "imagens"
PASTA_AUDIOS = "audios"
COOKIES_PATH = "cookies.txt"

def obter_ultimo_video(pasta=PASTA_VIDEOS):
    """
    Encontra o vídeo mais recente na pasta de vídeos (baseado na data de modificação).
    """
    try:
        arquivos = [os.path.join(pasta, f) for f in os.listdir(pasta) if f.endswith(".mp4")]
        if not arquivos:
            raise FileNotFoundError(f"❌ Nenhum vídeo novo encontrado em {pasta}.")
        
        ultimo_video = max(arquivos, key=os.path.getmtime)
        logger.info("📹 Último vídeo encontrado: %s", ultimo_video)
        return ultimo_video
    except Exception as e:
        logger.error("❌ Erro ao buscar último vídeo: %s", str(e))
        return None

def postar_no_tiktok_e_renomear(descricao_personalizada=None, imagem_base=None, imagem_final=None, video_final=None, agendar=False, idioma='en'):
    """
    Busca o último vídeo gerado, posta no TikTok e limpa os arquivos gerados após sucesso.
    Parâmetros: descricao_personalizada (str) para legenda dinâmica, caminhos dos arquivos gerados,
                agendar (bool) para post futuro, idioma (str) para definir hashtags e proxy.
    """
    video_path = video_final if video_final else obter_ultimo_video()
    if not video_path:
        return

    if not os.path.exists(COOKIES_PATH):
        logger.error(f"❌ Arquivo de cookies não encontrado: {COOKIES_PATH}")
        return

    try:
        # Determina as hashtags com base no idioma
        hashtags_en = " #Motivation #Inspiration #TikTokMotivational"
        hashtags_pt = " #Motivacao #Inspiracao #TikTokMotivacional"
        hashtags = hashtags_pt if idioma == 'pt-br' else hashtags_en

        if descricao_personalizada:
            description = descricao_personalizada + hashtags
        else:
            description = "Motivational content of the day!" + hashtags_en if idioma == 'en' else "Conteúdo motivacional do dia!" + hashtags_pt

        schedule = None
        if agendar:
            schedule = datetime.now() + timedelta(minutes=20)
            logger.info("📅 Agendando post para: %s", schedule.strftime("%H:%M:%S"))

        logger.info("🚀 Postando vídeo no TikTok: %s", video_path)
        time.sleep(2)  # Delay antes do upload para estabilidade da rede

        upload_video(
            filename=video_path,
            description=description,
            cookies=COOKIES_PATH,
            comment=True,
            stitch=True,
            duet=True,
            headless=False,  # Defina como False para ver o browser; mude para True se funcionar
            schedule=schedule,
            idioma=idioma  # Passa o idioma para upload_video
        )
        logger.info("✅ Vídeo postado com sucesso!")

        # Limpa os arquivos gerados se o upload foi bem-sucedido
        if imagem_base and os.path.exists(imagem_base):
            os.remove(imagem_base)
            logger.info("🗑️ Imagem base removida: %s", imagem_base)
        if imagem_final and os.path.exists(imagem_final):
            #os.remove(imagem_final)
            logger.info("🗑️ Imagem final removida: %s", imagem_final)
        if video_final and os.path.exists(video_final):
            os.remove(video_final)
            logger.info("🗑️ Vídeo original removido: %s", video_final)

        # Tenta remover o áudio (assumindo que o nome contém parte do slug ou é o último adicionado)
        audio_files = [f for f in os.listdir(PASTA_AUDIOS) if f.endswith(".mp3")]
        if audio_files:
            audio_to_remove = max((os.path.join(PASTA_AUDIOS, f) for f in audio_files if f.endswith(".mp3")), key=os.path.getmtime)
            os.remove(audio_to_remove)
            logger.info("🗑️ Áudio removido: %s", audio_to_remove)

    except (NoSuchElementException, TimeoutException) as e:
        logger.warning("⚠️ Erro intermediário ignorado (ex: split window ou interatividade não encontrada): %s. Verificando se post foi bem-sucedido manualmente.", str(e))
    except SocketError as e:
        if e.errno == 10054:
            logger.warning("⚠️ Conexão resetada (erro 10054), mas post provavelmente sucedido: %s. Verifique a conta no TikTok.", str(e))
        else:
            logger.error("❌ Erro de socket inesperado: %s", str(e))
            raise
    except WebDriverException as e:
        logger.error("❌ Erro no WebDriver: %s. Tente atualizar o Chrome ou Selenium.", str(e))
    except Exception as e:
        logger.error("❌ Erro geral ao postar: %s", str(e))
    finally:
        logger.info("⏳ Aguardando 5 segundos antes de finalizar...")
        time.sleep(5)  # Delay final para qualquer verificação de rede