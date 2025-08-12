# utils/video.py

import os
import random
from moviepy import ImageClip, AudioFileClip
from utils.audio import obter_caminho_audio
import logging
import io
import sys

# Configuração do logging com timestamps
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'  # Formato de horário (HH:MM:SS)
)
logger = logging.getLogger(__name__)

DURACAO_MAXIMA_VIDEO = 10  # em segundos

def gerar_video(imagem_path, saida_path):
    """
    Gera um vídeo com uma imagem e uma música de fundo aleatória.

    Parâmetros:
        imagem_path (str): Caminho da imagem de entrada.
        saida_path (str): Caminho completo do vídeo de saída.
    """
    # Redireciona a saída do MoviePy para capturar logs
    old_stdout = sys.stdout
    sys.stdout = output = io.StringIO()

    try:
        pasta_saida = os.path.dirname(saida_path)
        os.makedirs(pasta_saida, exist_ok=True)

        # Busca música via audio.py
        audio_path = obter_caminho_audio()

        if not audio_path or not os.path.isfile(audio_path):
            logger.error("❌ Caminho de áudio inválido ou arquivo não encontrado.")
            return

        with AudioFileClip(audio_path) as audio:  # Usa with para fechar o arquivo automaticamente
            duracao_audio = audio.duration
            duracao_final = min(duracao_audio, DURACAO_MAXIMA_VIDEO)

            logger.info("🎧 Duração do áudio: %.2fs | Usando: %.2fs", duracao_audio, duracao_final)

            # Cria clipe de imagem com a duração do áudio (limitada)
            clip = ImageClip(imagem_path).with_duration(duracao_final)
            # Obtém dimensões originais
            original_w, original_h = clip.size

            # Redimensiona proporcionalmente, limitando a 1080x1920
            if original_w > 1080 or original_h > 1920:
                if original_w > original_h:
                    nova_largura = 1080
                    nova_altura = int((original_h * 1080) / original_w)
                else:
                    nova_altura = 1920
                    nova_largura = int((original_w * 1920) / original_h)
                clip = clip.resized((nova_largura, nova_altura))
            else:
                # Mantém as dimensões originais se menores que o limite
                clip = clip.resized((original_w, original_h))

            audio_recorte = audio.subclipped(0, duracao_final)
            video = clip.with_audio(audio_recorte)

            logger.info("🎬 Gerando vídeo em: %s", saida_path)
            video.write_videofile(saida_path, fps=24)
            logger.info("✅ Vídeo salvo com sucesso: %s", saida_path)

            # Captura e processa a saída do MoviePy
            moviepy_output = output.getvalue()
            for line in moviepy_output.splitlines():
                if line.strip():
                    logger.info("🎥 %s", line.strip())

    except Exception as e:
        logger.error("❌ Erro ao gerar vídeo: %s", str(e))
    finally:
        sys.stdout = old_stdout  # Restaura a saída padrão