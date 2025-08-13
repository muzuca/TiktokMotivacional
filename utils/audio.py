# utils/audio.py

import os
import re
import random
import base64
import wave
import datetime
import logging
import requests
from typing import Optional
from dotenv import load_dotenv
from moviepy import AudioFileClip  # editor-safe
import json

# ====== .env / logging ======
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ====== Configs gerais ======
USE_REMOTE_AUDIO = True
FREESOUND_API_KEY = os.getenv("FREESOUND_API_KEY")
DURACAO_MINIMA = 30  # seg
PASTA_PADRAO = "audios"
PASTA_TTS = "audios_tts"
os.makedirs(PASTA_TTS, exist_ok=True)

# ====== Cache de áudios ======
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

# =============================================================================
# ÁUDIO DE FUNDO (local + Freesound)
# =============================================================================

def escolher_audio_local(diretorio=PASTA_PADRAO):
    try:
        formatos = (".mp3", ".wav", ".ogg")
        musicas_validas = []
        for f in os.listdir(diretorio):
            if f.lower().endswith(formatos):
                caminho = os.path.join(diretorio, f)
                try:
                    with AudioFileClip(caminho) as clip:
                        dur = clip.duration
                    if dur >= DURACAO_MINIMA:
                        musicas_validas.append((caminho, dur))
                except Exception as e:
                    logger.warning("Erro ao ler duração de %s: %s", f, e)

        if not musicas_validas:
            raise FileNotFoundError(f"Nenhum áudio >= {DURACAO_MINIMA}s em {diretorio}")

        used = load_used_audios()
        nao_usadas = [(c, d) for c, d in musicas_validas if c not in used]
        if not nao_usadas:
            logger.warning("Sem áudio local novo; reutilizando um aleatório.")
            nao_usadas = musicas_validas

        escolhido, duracao = random.choice(nao_usadas)
        used.add(escolhido)
        save_used_audios(used)
        logger.info("🎵 Áudio local: %s (%.0fs)", escolhido, duracao)
        return escolhido
    except Exception as e:
        logger.error("Falha ao escolher áudio local: %s", e)
        raise

def buscar_audio_freesound(
    query="inspirational",
    output_dir=PASTA_PADRAO,
    sort="rating_desc",
    additional_filters='tag:music tag:instrumental -tag:speech -tag:voice'
):
    if not FREESOUND_API_KEY:
        raise ValueError("FREESOUND_API_KEY não configurada no .env.")

    try:
        logger.info("🔍 Freesound: query='%s', sort='%s', filtros='%s'", query, sort, additional_filters)
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
            logger.warning("Sem resultados; relaxando filtros…")
            params["filter"] = f"duration:[{DURACAO_MINIMA} TO 300] tag:music -tag:speech"
            r = requests.get(url, headers=headers, params=params)
            r.raise_for_status()
            data = r.json()

        if not data.get("results"):
            logger.warning("Ainda sem resultados; fallback amplo…")
            params["query"] = "uplifting music"
            params["filter"] = f"duration:[{DURACAO_MINIMA} TO 300] tag:music -tag:speech"
            r = requests.get(url, headers=headers, params=params)
            r.raise_for_status()
            data = r.json()

        if not data.get("results"):
            logger.warning("Removendo filtros de tag…")
            params["filter"] = f"duration:[{DURACAO_MINIMA} TO 300]"
            r = requests.get(url, headers=headers, params=params)
            r.raise_for_status()
            data = r.json()
            if not data.get("results"):
                raise Exception("Nenhum áudio encontrado no Freesound.")

        used = load_used_audios()
        nao_usados = [a for a in data["results"] if str(a["id"]) not in used]
        if not nao_usados:
            logger.warning("Sem áudio novo no Freesound; reutilizando aleatório.")
            nao_usados = data["results"]

        audio = random.choice(nao_usados)
        audio_id = audio["id"]
        nome = re.sub(r"\W+", "_", audio["name"].split(".")[0])
        caminho = os.path.join(output_dir, f"{audio_id}_{nome}.mp3")

        os.makedirs(output_dir, exist_ok=True)

        if os.path.exists(caminho):
            logger.info("✅ Já existe: %s", caminho)
            with AudioFileClip(caminho) as clip:
                dur = clip.duration
            if dur < DURACAO_MINIMA:
                os.remove(caminho)
                raise ValueError(f"Áudio existente curto ({dur:.0f}s).")
            used.add(str(audio_id))
            save_used_audios(used)
            return caminho

        preview_url = audio["previews"]["preview-hq-mp3"]
        resp = requests.get(preview_url, stream=True)
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
        logger.info("✅ Baixado: %s (%.0fs, lic: %s)", caminho, dur, audio.get('license', 'N/A'))
        return caminho

    except requests.RequestException as e:
        logger.error("Erro de rede Freesound: %s", e)
        raise
    except Exception as e:
        logger.error("Erro Freesound: %s", e)
        raise

def obter_caminho_audio(query="inspirational", diretorio=PASTA_PADRAO,
                        sort="rating_desc",
                        additional_filters='tag:music tag:instrumental -tag:speech -tag:voice'):
    try:
        if USE_REMOTE_AUDIO:
            logger.info("Tentando áudio remoto: %s", query)
            return buscar_audio_freesound(query, diretorio, sort, additional_filters)
        else:
            logger.info("Usando áudio local (USE_REMOTE_AUDIO=False)")
            return escolher_audio_local(diretorio)
    except Exception as e:
        logger.warning("Falha no remoto; fallback local: %s", e)
        return escolher_audio_local(diretorio)

# =============================================================================
# TTS (Gemini = padrão) + ElevenLabs como alternativa
# =============================================================================

# ---- Gemini TTS (novo SDK google-genai) ----
_HAS_GOOGLE_GENAI = True
try:
    from google import genai as genai_new
    from google.genai import types as genai_types
except Exception:
    _HAS_GOOGLE_GENAI = False

def _wav_write(filename: str, pcm_bytes: bytes, channels=1, rate=24000, sample_width=2):
    with wave.open(filename, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(rate)
        wf.writeframes(pcm_bytes)

def gerar_narracao_tts_gemini(texto: str, idioma: str = "en",
                              voice_name: Optional[str] = None,
                              model: Optional[str] = None) -> Optional[str]:
    """
    Gera WAV (PCM 24 kHz, mono) via Gemini TTS.
    Requer biblioteca 'google-genai' e GEMINI_API_KEY.
    """
    if not _HAS_GOOGLE_GENAI:
        logger.warning("Pacote 'google-genai' não instalado. pip install google-genai")
        return None

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY ausente. TTS Gemini indisponível.")
        return None

    model = model or os.getenv("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")

    # Voz padrão (pode ajustar via env GEMINI_TTS_VOICE)
    default_voice_map = {"en": "Kore", "pt": "Sadaltager"}
    lang = "pt" if (idioma or "").lower().startswith("pt") else "en"
    voice_name = voice_name or os.getenv("GEMINI_TTS_VOICE") or default_voice_map.get(lang, "Kore")

    try:
        client = genai_new.Client(api_key=api_key)
        resp = client.models.generate_content(
            model=model,
            contents=texto,
            config=genai_types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=genai_types.SpeechConfig(
                    voice_config=genai_types.VoiceConfig(
                        prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                            voice_name=voice_name
                        )
                    )
                ),
            ),
        )

        # Bytes PCM (a SDK Python já retorna bytes, mas tratamos caso venha base64)
        data = resp.candidates[0].content.parts[0].inline_data.data
        if isinstance(data, str):
            pcm_bytes = base64.b64decode(data)
        else:
            pcm_bytes = data

        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(PASTA_TTS, f"tts_{lang}_{ts}.wav")
        _wav_write(path, pcm_bytes, channels=1, rate=24000, sample_width=2)
        logger.info("🎧 TTS (Gemini) gerado: %s", path)
        return path
    except Exception as e:
        logger.warning("Falha no TTS Gemini: %s", e)
        return None

# ---- ElevenLabs (SDK oficial) ----
_HAS_ELEVENLABS = True
try:
    from elevenlabs.client import ElevenLabs
except Exception:
    _HAS_ELEVENLABS = False

def gerar_narracao_tts_elevenlabs(texto: str, idioma: str = "en") -> Optional[str]:
    """
    Gera MP3 via ElevenLabs. Requer ELEVENLABS_API_KEY.
    """
    if not _HAS_ELEVENLABS:
        logger.warning("Pacote 'elevenlabs' não instalado. pip install elevenlabs")
        return None

    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        logger.warning("ELEVENLABS_API_KEY ausente.")
        return None

    # IDs de voz exemplo (troque pelos seus)
    voice_ids = {
        "en": os.getenv("ELEVENLABS_VOICE_EN", "y2Y5MeVPm6ZQXK64WUui"),
        "pt": os.getenv("ELEVENLABS_VOICE_PT", "rnJZLKxtlBZt77uIED10"),
    }
    lang = "pt" if (idioma or "").lower().startswith("pt") else "en"
    voice_id = voice_ids.get(lang, voice_ids["en"])

    try:
        client = ElevenLabs(api_key=api_key)
        stream = client.text_to_speech.stream(text=texto, voice_id=voice_id, model_id="eleven_multilingual_v2")

        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(PASTA_TTS, f"tts_{lang}_{ts}.mp3")
        with open(path, "wb") as f:
            for chunk in stream:
                if isinstance(chunk, bytes) and chunk:
                    f.write(chunk)

        if os.path.exists(path):
            logger.info("🎧 TTS (ElevenLabs) gerado: %s", path)
            return path
        logger.warning("Falha ao salvar TTS ElevenLabs.")
        return None
    except Exception as e:
        logger.warning("Erro ElevenLabs: %s", e)
        return None

# ---- Facade para escolher motor TTS ----
def gerar_narracao_tts(texto: str, idioma: str = "en", engine: str = "gemini") -> Optional[str]:
    """
    Gera narração TTS com o motor escolhido.
      engine = "gemini" (padrão) | "elevenlabs"
    Tenta fallback para o outro motor caso o escolhido falhe.
    """
    engine = (engine or "gemini").strip().lower()
    if engine == "gemini":
        path = gerar_narracao_tts_gemini(texto, idioma=idioma)
        if path:
            return path
        logger.info("Fallback: tentando ElevenLabs…")
        return gerar_narracao_tts_elevenlabs(texto, idioma=idioma)
    else:
        path = gerar_narracao_tts_elevenlabs(texto, idioma=idioma)
        if path:
            return path
        logger.info("Fallback: tentando Gemini…")
        return gerar_narracao_tts_gemini(texto, idioma=idioma)
