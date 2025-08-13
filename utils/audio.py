# utils/audio.py

import os
import random
import requests
from dotenv import load_dotenv
from moviepy import AudioFileClip  # MoviePy editor API segura p/ duração
import logging
import json
import datetime
from typing import Optional

# Carregar .env
load_dotenv()

USE_REMOTE_AUDIO = True  # True => busca no Freesound; False => só local
FREESOUND_API_KEY = os.getenv("FREESOUND_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

DURACAO_MINIMA = 30  # s
PASTA_PADRAO = "audios"
PASTA_TTS = "audios_tts"

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# Cache
CACHE_DIR = "cache"
os.makedirs(CACHE_DIR, exist_ok=True)
AUDIOS_CACHE_FILE = os.path.join(CACHE_DIR, "used_audios.json")

def load_used_audios():
    if os.path.exists(AUDIOS_CACHE_FILE):
        with open(AUDIOS_CACHE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_used_audios(used_audios):
    with open(AUDIOS_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(list(used_audios), f)

# --------------------------------------------------------------------
# ÁUDIO LOCAL / FREESOUND
# --------------------------------------------------------------------
def escolher_audio_local(diretorio=PASTA_PADRAO):
    """Escolhe um áudio local com pelo menos DURACAO_MINIMA (mp3/wav/ogg)."""
    try:
        formatos = (".mp3", ".wav", ".ogg")
        musicas_validas = []
        for f in os.listdir(diretorio):
            if f.lower().endswith(formatos):
                caminho = os.path.join(diretorio, f)
                try:
                    with AudioFileClip(caminho) as clip:
                        duracao = clip.duration
                    if duracao >= DURACAO_MINIMA:
                        musicas_validas.append((caminho, duracao))
                except Exception as e:
                    logger.warning("Erro ao verificar duração de %s: %s", f, e)

        if not musicas_validas:
            raise FileNotFoundError(f"❌ Nenhum áudio válido (>= {DURACAO_MINIMA}s) em {diretorio}.")

        used_audios = load_used_audios()
        nao_usados = [(c, d) for c, d in musicas_validas if c not in used_audios]
        if not nao_usados:
            logger.warning("Nenhum áudio local novo — reutilizando aleatório.")
            nao_usados = musicas_validas

        selecionado, duracao_sel = random.choice(nao_usados)
        used_audios.add(selecionado)
        save_used_audios(used_audios)
        logger.info("🎵 Áudio local escolhido: %s (%.0fs)", selecionado, duracao_sel)
        return selecionado
    except Exception as e:
        logger.error("Falha ao escolher áudio local: %s", e)
        raise

def buscar_audio_freesound(
    query="inspirational",
    output_dir=PASTA_PADRAO,
    sort="rating_desc",
    additional_filters='tag:music tag:instrumental -tag:speech -tag:voice'
):
    """Busca um áudio no Freesound (preview HQ MP3) e salva localmente."""
    if not FREESOUND_API_KEY:
        raise ValueError("❌ FREESOUND_API_KEY não configurada no .env.")

    try:
        logger.info("🔍 Buscando áudio no Freesound: query='%s'", query)
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

        def _call(p):
            r = requests.get(url, headers=headers, params=p, timeout=30)
            r.raise_for_status()
            return r.json()

        data = _call(params)

        if not data.get("results"):
            logger.warning("Sem resultados; relaxando filtros…")
            params["filter"] = f"duration:[{DURACAO_MINIMA} TO 300] tag:music -tag:speech"
            data = _call(params)

        if not data.get("results"):
            logger.warning("Ainda sem resultados; fallback 'uplifting music'…")
            params["query"] = "uplifting music"
            data = _call(params)

        if not data.get("results"):
            logger.warning("Sem resultados; removendo filtros de tag…")
            params["filter"] = f"duration:[{DURACAO_MINIMA} TO 300]"
            data = _call(params)

        if not data.get("results"):
            raise Exception("❌ Nenhum áudio encontrado após fallbacks.")

        used = load_used_audios()
        nao_usados = [a for a in data["results"] if str(a["id"]) not in used]
        if not nao_usados:
            logger.warning("Nenhum áudio novo no Freesound — reutilizando aleatório.")
            nao_usados = data["results"]

        audio = random.choice(nao_usados)
        audio_id = audio["id"]
        nome = (audio["name"] or "sound").split(".")[0].replace(" ", "_")
        os.makedirs(output_dir, exist_ok=True)
        caminho = os.path.join(output_dir, f"{audio_id}_{nome}.mp3")

        if os.path.exists(caminho):
            logger.info("✅ Já existe local: %s", caminho)
            with AudioFileClip(caminho) as clip:
                if clip.duration < DURACAO_MINIMA:
                    os.remove(caminho)
                    raise ValueError("Áudio existente tem duração insuficiente.")
            used.add(str(audio_id))
            save_used_audios(used)
            return caminho

        preview_url = audio["previews"]["preview-hq-mp3"]
        with requests.get(preview_url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            with open(caminho, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)

        with AudioFileClip(caminho) as clip:
            dur = clip.duration
        if dur < DURACAO_MINIMA:
            os.remove(caminho)
            raise ValueError(f"Áudio baixado curto ({dur:.0f}s).")

        used.add(str(audio_id))
        save_used_audios(used)
        logger.info("✅ Áudio (preview HQ) baixado: %s (%.0fs, lic: %s)", caminho, dur, audio.get("license", "N/A"))
        return caminho

    except requests.RequestException as e:
        logger.error("Erro de rede Freesound: %s", e)
        raise
    except Exception as e:
        logger.error("Erro Freesound: %s", e)
        raise

def obter_caminho_audio(
    query="inspirational",
    diretorio=PASTA_PADRAO,
    sort="rating_desc",
    additional_filters='tag:music tag:instrumental -tag:speech -tag:voice'
):
    """Retorna caminho de um áudio válido (Freesound ou local)."""
    try:
        if USE_REMOTE_AUDIO:
            logger.info("Tentando obter áudio remoto com query: %s", query)
            return buscar_audio_freesound(query, diretorio, sort, additional_filters)
        logger.info("Somente áudio local (USE_REMOTE_AUDIO=False)")
        return escolher_audio_local(diretorio)
    except Exception as e:
        logger.warning("Falha no remoto; usando local. Motivo: %s", e)
        return escolher_audio_local(diretorio)

# --------------------------------------------------------------------
# TTS ELEVENLABS (NARRAÇÃO)
# --------------------------------------------------------------------
def gerar_narracao_tts(texto: str, idioma: str = "en", output_dir: str = PASTA_TTS) -> Optional[str]:
    """
    Gera narração TTS via ElevenLabs e retorna o caminho do MP3.
    Requer ELEVENLABS_API_KEY no .env.
    """
    if not ELEVENLABS_API_KEY:
        logger.warning("ELEVENLABS_API_KEY ausente — sem TTS.")
        return None

    # voice_id por idioma (ajuste para as vozes que você prefere)
    voice_ids = {
        "en": "y2Y5MeVPm6ZQXK64WUui",  # inglês
        "pt": "rnJZLKxtlBZt77uIED10",  # português
    }
    lang = (idioma or "en").lower()
    lang = "pt" if lang.startswith("pt") else "en"
    voice_id = voice_ids.get(lang, voice_ids["en"])

    model_id = "eleven_multilingual_v2"
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"

    headers = {
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
        "xi-api-key": ELEVENLABS_API_KEY,  # cabeçalho padrão da ElevenLabs
    }
    payload = {
        "text": texto,
        "model_id": model_id,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.8},
    }

    os.makedirs(output_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    outpath = os.path.join(output_dir, f"tts_{lang}_{stamp}.mp3")

    try:
        with requests.post(url, headers=headers, json=payload, stream=True, timeout=90) as r:
            r.raise_for_status()
            with open(outpath, "wb") as f:
                for chunk in r.iter_content(8192):
                    if chunk:
                        f.write(chunk)
        # valida duração
        with AudioFileClip(outpath) as clip:
            if clip.duration <= 1.0:
                logger.warning("TTS gerado muito curto — ignorando.")
                os.remove(outpath)
                return None
        logger.info("🎧 TTS gerado: %s", outpath)
        return outpath
    except requests.RequestException as e:
        logger.warning("Erro ElevenLabs: %s", e)
        try:
            if os.path.exists(outpath):
                os.remove(outpath)
        except Exception:
            pass
        return None
