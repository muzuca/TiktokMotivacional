# utils/audio.py

import os
import random
import requests
from dotenv import load_dotenv
from moviepy import AudioFileClip  # Use editor import for safety
import logging
import json

# Carregar .env
load_dotenv()

USE_REMOTE_AUDIO = True  # Altere para False para usar somente local
FREESOUND_API_KEY = os.getenv("FREESOUND_API_KEY")

DURACAO_MINIMA = 30  # em segundos
PASTA_PADRAO = "audios"

# Configuração do logging com timestamps
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'  # Formato de horário (HH:MM:SS)
)
logger = logging.getLogger(__name__)

# Pasta de cache
CACHE_DIR = "cache"
os.makedirs(CACHE_DIR, exist_ok=True)
AUDIOS_CACHE_FILE = os.path.join(CACHE_DIR, "used_audios.json")

def load_used_audios():
    """Carrega a lista de áudios já usados do cache."""
    if os.path.exists(AUDIOS_CACHE_FILE):
        with open(AUDIOS_CACHE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_used_audios(used_audios):
    """Salva a lista de áudios usados no cache."""
    with open(AUDIOS_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(list(used_audios), f)

def escolher_audio_local(diretorio=PASTA_PADRAO):
    """
    Escolhe um áudio local com pelo menos DURACAO_MINIMA de duração.
    Suporta MP3, WAV, OGG.
    """
    try:
        formatos_suportados = (".mp3", ".wav", ".ogg")
        musicas_validas = []

        for f in os.listdir(diretorio):
            if f.lower().endswith(formatos_suportados):
                caminho = os.path.join(diretorio, f)
                try:
                    with AudioFileClip(caminho) as clip:
                        duracao = clip.duration
                    if duracao >= DURACAO_MINIMA:
                        musicas_validas.append((caminho, duracao))
                except Exception as e:
                    logger.warning("Erro ao verificar duração de %s: %s. Arquivo será ignorado.", f, str(e))

        if not musicas_validas:
            raise FileNotFoundError(f"❌ Nenhum áudio local válido (>= {DURACAO_MINIMA}s) encontrado em {diretorio}.")

        used_audios = load_used_audios()
        musicas_nao_usadas = [(c, d) for c, d in musicas_validas if c not in used_audios]
        if not musicas_nao_usadas:
            logger.warning("Nenhum áudio local novo disponível. Reutilizando um áudio aleatório.")
            musicas_nao_usadas = musicas_validas

        selecionado, duracao_sel = random.choice(musicas_nao_usadas)
        used_audios.add(selecionado)
        save_used_audios(used_audios)
        logger.info("🎵 Áudio local escolhido: %s (duração: %.0f segundos)", selecionado, duracao_sel)
        return selecionado
    except Exception as e:
        logger.error("Falha ao escolher áudio local: %s", str(e))
        raise

def buscar_audio_freesound(
    query="inspirational",
    output_dir=PASTA_PADRAO,
    sort="rating_desc",
    additional_filters='tag:music tag:instrumental -tag:speech -tag:voice'
):
    """
    Busca um áudio motivacional no Freesound, priorizando alta qualidade, e salva localmente.
    Usa preview-hq-mp3 (não requer auth para download).
    """
    if not FREESOUND_API_KEY:
        raise ValueError("❌ Chave API do Freesound (FREESOUND_API_KEY) não configurada no .env.")

    try:
        logger.info("🔍 Buscando áudio no Freesound: query='%s', sort='%s', filtros adicionais='%s'", query, sort, additional_filters)
        url = "https://freesound.org/apiv2/search/text/"
        headers = {"Authorization": f"Token {FREESOUND_API_KEY}"}
        filters = f"duration:[{DURACAO_MINIMA} TO 300] {additional_filters}".strip()
        params = {
            "query": query,
            "filter": filters,
            "sort": sort,
            "fields": "id,name,duration,tags,license,previews",
            "page_size": 20,
        }

        r = requests.get(url, headers=headers, params=params)
        r.raise_for_status()
        data = r.json()

        if not data.get("results"):
            # Relaxa filtros: remove instrumental e voice
            logger.warning("Nenhum resultado; relaxando filtros (removendo 'instrumental' e 'voice')...")
            filters = f"duration:[{DURACAO_MINIMA} TO 300] tag:music -tag:speech"
            params["filter"] = filters
            r = requests.get(url, headers=headers, params=params)
            r.raise_for_status()
            data = r.json()

        if not data.get("results"):
            # Fallback: Query mais ampla com filtros mínimos
            logger.warning("Ainda sem resultados; tentando fallback com 'uplifting music' e filtros mínimos")
            params["query"] = "uplifting music"
            params["filter"] = f"duration:[{DURACAO_MINIMA} TO 300] tag:music -tag:speech"
            r = requests.get(url, headers=headers, params=params)
            r.raise_for_status()
            data = r.json()

        if not data.get("results"):
            # Fallback final: Remove todos os filtros de tag
            logger.warning("Ainda sem resultados; removendo todos os filtros de tag...")
            params["filter"] = f"duration:[{DURACAO_MINIMA} TO 300]"
            r = requests.get(url, headers=headers, params=params)
            r.raise_for_status()
            data = r.json()
            if not data.get("results"):
                raise Exception("❌ Nenhum áudio encontrado no Freesound após todos os fallbacks.")

        # Escolhe aleatoriamente para variedade
        used_audios = load_used_audios()
        audios_nao_usados = [a for a in data["results"] if str(a["id"]) not in used_audios]
        if not audios_nao_usados:
            logger.warning("Nenhum áudio novo disponível no Freesound. Reutilizando um aleatório.")
            audios_nao_usados = data["results"]

        audio = random.choice(audios_nao_usados)
        audio_id = audio["id"]
        nome = audio["name"].split(".")[0].replace(" ", "_")  # Limpa nome
        caminho = os.path.join(output_dir, f"{audio_id}_{nome}.mp3")

        # Verifica se já existe localmente
        if os.path.exists(caminho):
            logger.info("✅ Áudio já existe localmente: %s", caminho)
            with AudioFileClip(caminho) as clip:
                duracao = clip.duration
            if duracao < DURACAO_MINIMA:
                os.remove(caminho)
                raise ValueError(f"❌ Áudio existente {caminho} tem duração insuficiente ({duracao:.0f}s).")
            used_audios.add(str(audio_id))
            save_used_audios(used_audios)
            return caminho

        # Baixa o preview-hq-mp3 (sem auth necessária)
        preview_url = audio["previews"]["preview-hq-mp3"]
        os.makedirs(output_dir, exist_ok=True)
        resp = requests.get(preview_url, stream=True)
        resp.raise_for_status()  # Garante sucesso antes de escrever
        with open(caminho, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        # Verifica duração pós-download
        with AudioFileClip(caminho) as clip:
            duracao = clip.duration
        if duracao < DURACAO_MINIMA:
            os.remove(caminho)  # Remove se inválido
            raise ValueError(f"❌ Áudio baixado tem duração insuficiente ({duracao:.0f}s).")

        used_audios.add(str(audio_id))
        save_used_audios(used_audios)
        logger.info("✅ Áudio (preview HQ) baixado: %s (duração: %.0f segundos, licença: %s, tags: %s)", caminho, duracao, audio.get('license', 'N/A'), ', '.join(audio.get('tags', [])))
        return caminho

    except requests.RequestException as e:
        logger.error("Erro de rede ao acessar Freesound: %s", str(e))
        raise
    except Exception as e:
        logger.error("Erro ao buscar/baixar áudio no Freesound: %s", str(e))
        raise

def obter_caminho_audio(query="inspirational", diretorio=PASTA_PADRAO, sort="rating_desc", additional_filters='tag:music tag:instrumental -tag:speech -tag:voice'):
    """
    Retorna o caminho de um áudio válido (local ou Freesound), com fallback automático.
    """
    try:
        if USE_REMOTE_AUDIO:
            logger.info("Tentando obter áudio remoto com query: %s", query)
            return buscar_audio_freesound(query, diretorio, sort, additional_filters)
        else:
            logger.info("Usando apenas áudio local, pois USE_REMOTE_AUDIO é False")
            return escolher_audio_local(diretorio)
    except Exception as e:
        logger.warning("Falha ao obter áudio remoto; usando local como fallback: %s", str(e))
        return escolher_audio_local(diretorio)