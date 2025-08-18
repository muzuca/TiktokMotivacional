# utils/tiktok.py
import os
import re
import time  # Para delays
from datetime import datetime, timedelta
from .tiktok_uploader.upload import upload_video  # Import local da pasta tiktok_uploader
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from socket import error as SocketError  # Para ConnectionResetError
import logging
from typing import Optional

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

# Controla se removemos marcações markdown da legenda
_STRIP_FLAG = os.getenv("STRIP_MARKDOWN_IN_DESC", "1").strip().lower()
STRIP_MARKDOWN_IN_DESC = _STRIP_FLAG not in ("0", "false", "no", "off")


def _normalizar_idioma(v: Optional[str]) -> str:
    """Normaliza a entrada do idioma para 'en', 'pt-br', 'ar' ou 'auto'."""
    s = (v or "").strip().lower()
    if s in ("1", "en", "en-us", "us", "usa", "eua", "ingles", "inglês", "english"):
        return "en"
    if s in ("2", "pt", "pt-br", "br", "brasil", "portugues", "português"):
        return "pt-br"
    if s in ("3", "ar", "ar-eg", "egito", "eg", "árabe", "arabe"):
        return "ar"
    return "auto"


_MD_PATTERNS = (
    (re.compile(r"\*\*(.+?)\*\*", re.DOTALL), r"\1"),   # **bold**
    (re.compile(r"\*(.+?)\*",     re.DOTALL), r"\1"),   # *italic*
    (re.compile(r"__(.+?)__",     re.DOTALL), r"\1"),   # __underline__
    (re.compile(r"_(.+?)_",       re.DOTALL), r"\1"),   # _italic_
    (re.compile(r"~~(.+?)~~",     re.DOTALL), r"\1"),   # ~~strike~~
    (re.compile(r"`([^`]+)`",     re.DOTALL), r"\1"),   # `code`
)

def _strip_markdown(texto: str) -> str:
    """Remove marcações básicas de Markdown e normaliza espaços."""
    s = (texto or "")
    for pat, rep in _MD_PATTERNS:
        s = pat.sub(rep, s)
    # Normaliza aspas “ ” ’
    s = s.replace("“", "\"").replace("”", "\"").replace("’", "'")
    # Colapsa espaços múltiplos
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()


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


def postar_no_tiktok_e_renomear(
    descricao_personalizada=None,
    imagem_base=None,
    imagem_final=None,
    video_final=None,
    agendar=False,
    idioma='en'
):
    """
    Busca o último vídeo gerado, posta no TikTok e limpa os arquivos gerados após sucesso.
    Parâmetros:
      - descricao_personalizada (str): legenda dinâmica
      - imagem_base, imagem_final, video_final: caminhos dos arquivos
      - agendar (bool): se True, agenda ~20min à frente
      - idioma (str): 'en' / 'pt-br' / 'ar' (controla proxy, hashtags e cookies)
    """
    idioma_norm = _normalizar_idioma(idioma)
    logger.info("postar_no_tiktok_e_renomear: idioma_in=%s | idioma_norm=%s", idioma, idioma_norm)

    video_path = video_final if video_final else obter_ultimo_video()
    if not video_path:
        return

    # Seleciona cookies por idioma (env-first)
    cookies_map = {
        "en":   os.getenv("COOKIES_US_FILENAME", "cookies_us.txt"),
        "pt-br":os.getenv("COOKIES_BR_FILENAME", "cookies_br.txt"),
        "ar":   os.getenv("COOKIES_EG_FILENAME", "cookies_eg.txt"),
    }
    COOKIES_PATH = cookies_map.get(idioma_norm, os.getenv("COOKIES_US_FILENAME", "cookies_us.txt"))
    logger.info("🍪 Cookies utilizados: %s", COOKIES_PATH)

    if not os.path.exists(COOKIES_PATH):
        logger.error(f"❌ Arquivo de cookies não encontrado: {COOKIES_PATH}")
        return

    try:
        # Hashtags conforme idioma (env-first)
        hashtags_en = os.getenv("HASHTAGS_EN", " #Motivation #Inspiration #TikTokMotivational")
        hashtags_pt = os.getenv("HASHTAGS_PT_BR", " #Motivacao #Inspiracao #TikTokMotivacional")
        hashtags_ar = os.getenv("HASHTAGS_AR_EG", " #تاروت #قراءة_تاروت #ابراج #طاقة #توقعات")

        if idioma_norm == "pt-br":
            hashtags = hashtags_pt
        elif idioma_norm == "ar":
            hashtags = hashtags_ar
        else:
            hashtags = hashtags_en

        # Monta a descrição e limpa markdown, se habilitado
        if descricao_personalizada:
            base_desc = descricao_personalizada
        else:
            if idioma_norm == "pt-br":
                base_desc = "Conteúdo motivacional do dia!"
            elif idioma_norm == "ar":
                base_desc = "رسالة اليوم ✨"
            else:
                base_desc = "Motivational content of the day!"

        if STRIP_MARKDOWN_IN_DESC:
            cleaned = _strip_markdown(base_desc)
            if cleaned != base_desc:
                logger.debug("🧹 Limpando markdown da descrição: '%s' -> '%s'", base_desc, cleaned)
            base_desc = cleaned

        description = base_desc + hashtags

        schedule = None
        if agendar:
            schedule = datetime.now() + timedelta(minutes=20)
            logger.info("📅 Agendando post para: %s", schedule.strftime("%H:%M:%S"))

        logger.info("🚀 Postando vídeo no TikTok: %s", video_path)
        logger.info("📝 Descrição final: %s", description)
        time.sleep(2)  # Delay antes do upload para estabilidade da rede

        upload_video(
            filename=video_path,
            description=description,
            cookies=COOKIES_PATH,
            comment=True,
            stitch=True,
            duet=True,
            headless=False,         # troque para True se quiser rodar invisível
            schedule=schedule,
            idioma=idioma_norm      # ex.: 'ar' -> ar-EG na lib
        )
        logger.info("✅ Vídeo postado com sucesso!")

        # Limpa os arquivos gerados se o upload foi bem-sucedido
        if imagem_base and os.path.exists(imagem_base):
            os.remove(imagem_base)
            logger.info("🗑️ Imagem base removida: %s", imagem_base)
        if imagem_final and os.path.exists(imagem_final):
            os.remove(imagem_final)
            logger.info("🗑️ Imagem final removida: %s", imagem_final)
        if video_final and os.path.exists(video_final):
            os.remove(video_final)
            logger.info("🗑️ Vídeo original removido: %s", video_final)

        # Remove o áudio mais recente (se existir)
        try:
            audio_files = [f for f in os.listdir(PASTA_AUDIOS) if f.endswith(".mp3")]
            if audio_files:
                audio_to_remove = max(
                    (os.path.join(PASTA_AUDIOS, f) for f in audio_files),
                    key=os.path.getmtime
                )
                os.remove(audio_to_remove)
                logger.info("🗑️ Áudio removido: %s", audio_to_remove)
        except Exception:
            pass

    except (NoSuchElementException, TimeoutException) as e:
        logger.warning("⚠️ Erro intermediário ignorado: %s. Verifique se o post saiu na conta.", str(e))
    except SocketError as e:
        if getattr(e, "errno", None) == 10054:
            logger.warning("⚠️ Conexão resetada (10054). Post possivelmente concluído. Confira no perfil.")
        else:
            logger.error("❌ Erro de socket inesperado: %s", str(e))
            raise
    except WebDriverException as e:
        logger.error("❌ Erro no WebDriver: %s. Tente atualizar Chrome/Selenium.", str(e))
    except Exception as e:
        logger.error("❌ Erro geral ao postar: %s", str(e))
    finally:
        logger.info("⏳ Aguardando 5 segundos antes de finalizar...")
        time.sleep(5)
