# utils/audio.py

import os
import re
import random
import base64
import wave
import datetime
import logging
import requests
import time
import json
from typing import Optional, Callable, TypeVar, Tuple

# moviepy: tentar import "editor-safe" primeiro e cair pro clássico se necessário
try:
    from moviepy import AudioFileClip  # type: ignore
except Exception:  # pragma: no cover
    from moviepy.editor import AudioFileClip  # type: ignore

from dotenv import load_dotenv

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

# ====== Helpers de retry/backoff ======
T = TypeVar("T")
MAX_RETRIES_DEFAULT = 3

def _retry(fn: Callable[[], T],
           *,
           tries: int = MAX_RETRIES_DEFAULT,
           base_sleep: float = 0.8,
           jitter: float = 0.35,
           on_error_msg: str | None = None) -> T | None:
    """
    Executa fn() com até 'tries' tentativas. Em erro, faz backoff exponencial + jitter.
    Retorna o resultado de fn() ou None se falhar todas.
    """
    for attempt in range(1, max(1, tries) + 1):
        try:
            return fn()
        except Exception as e:
            if on_error_msg:
                logger.warning("%s (tentativa %d/%d): %s", on_error_msg, attempt, tries, e)
            if attempt >= tries:
                break
            sleep_s = base_sleep * (2 ** (attempt - 1)) + random.uniform(0, jitter)
            time.sleep(sleep_s)
    return None

# =============================================================================
# ÁUDIO DE FUNDO (local + Freesound)
# =============================================================================

def _duracao_arquivo(path: str) -> float:
    try:
        with AudioFileClip(path) as clip:
            return float(clip.duration or 0.0)
    except Exception:
        return 0.0

def escolher_audio_local(diretorio=PASTA_PADRAO):
    try:
        formatos = (".mp3", ".wav", ".ogg")
        musicas_validas = []
        for f in os.listdir(diretorio):
            if f.lower().endswith(formatos):
                caminho = os.path.join(diretorio, f)
                dur = _duracao_arquivo(caminho)
                if dur >= DURACAO_MINIMA:
                    musicas_validas.append((caminho, dur))

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

def _freesound_busca_lote(query: str,
                          sort: str,
                          filters: str,
                          page_size: int = 20) -> dict:
    url = "https://freesound.org/apiv2/search/text/"
    headers = {"Authorization": f"Token {FREESOUND_API_KEY}"}
    params = {
        "query": query,
        "filter": filters,
        "sort": sort,
        "fields": "id,name,duration,tags,license,previews",
        "page_size": page_size,
    }
    r = requests.get(url, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def _baixar_preview(audio_meta: dict, destino: str) -> float:
    os.makedirs(os.path.dirname(destino) or ".", exist_ok=True)
    preview_url = audio_meta["previews"]["preview-hq-mp3"]
    with requests.get(preview_url, stream=True, timeout=30) as resp:
        resp.raise_for_status()
        with open(destino, "wb") as f:
            for chunk in resp.iter_content(8192):
                if chunk:
                    f.write(chunk)
    dur = _duracao_arquivo(destino)
    if dur < DURACAO_MINIMA:
        try:
            os.remove(destino)
        except Exception:
            pass
        raise ValueError(f"Áudio baixado curto ({dur:.0f}s).")
    return dur

def buscar_audio_freesound(
    query="inspirational",
    output_dir=PASTA_PADRAO,
    sort="rating_desc",
    additional_filters='tag:music tag:instrumental -tag:speech -tag:voice',
    *,
    max_retries: int = MAX_RETRIES_DEFAULT
):
    """
    Busca e baixa um áudio do Freesound com retries.
    Prioriza itens ainda não usados no cache.
    """
    if not FREESOUND_API_KEY:
        raise ValueError("FREESOUND_API_KEY não configurada no .env.")

    def tentar() -> str:
        logger.info("🔍 Freesound: query='%s', sort='%s', filtros='%s'", query, sort, additional_filters)
        # 1) busca com filtros "fortes"
        filters = f"duration:[{DURACAO_MINIMA} TO 300] {additional_filters}".strip()
        data = _freesound_busca_lote(query, sort, filters)

        # 2) afrouxa se vazio
        if not data.get("results"):
            logger.warning("Sem resultados; relaxando filtros…")
            filters2 = f"duration:[{DURACAO_MINIMA} TO 300] tag:music -tag:speech"
            data = _freesound_busca_lote(query, sort, filters2)

        # 3) fallback de query
        if not data.get("results"):
            logger.warning("Ainda sem resultados; fallback amplo…")
            data = _freesound_busca_lote("uplifting music", sort, f"duration:[{DURACAO_MINIMA} TO 300] tag:music -tag:speech")

        # 4) último recurso – só duração
        if not data.get("results"):
            logger.warning("Removendo filtros de tag…")
            data = _freesound_busca_lote(query, sort, f"duration:[{DURACAO_MINIMA} TO 300]")
            if not data.get("results"):
                raise RuntimeError("Nenhum áudio encontrado no Freesound.")

        # Escolher um que não esteja no cache
        used = load_used_audios()
        resultados = data["results"]
        candidatos = [a for a in resultados if str(a["id"]) not in used] or resultados

        audio = random.choice(candidatos)
        audio_id = audio["id"]
        nome = re.sub(r"\W+", "_", audio["name"].split(".")[0])
        caminho = os.path.join(output_dir, f"{audio_id}_{nome}.mp3")

        if os.path.exists(caminho):
            logger.info("✅ Já existe: %s", caminho)
            dur = _duracao_arquivo(caminho)
            if dur < DURACAO_MINIMA:
                os.remove(caminho)
                raise ValueError(f"Áudio existente curto ({dur:.0f}s).")
        else:
            dur = _baixar_preview(audio, caminho)

        used.add(str(audio_id))
        save_used_audios(used)
        logger.info("✅ Pronto: %s (%.0fs, lic: %s)", caminho, dur, audio.get('license', 'N/A'))
        return caminho

    # retry com backoff
    caminho = _retry(tentar, tries=max_retries, on_error_msg="Erro Freesound")
    if caminho is None:
        raise RuntimeError("Falha ao obter áudio do Freesound após múltiplas tentativas.")
    return caminho

def obter_caminho_audio(query="inspirational", diretorio=PASTA_PADRAO,
                        sort="rating_desc",
                        additional_filters='tag:music tag:instrumental -tag:speech -tag:voice'):
    """
    Tenta áudio remoto com retry; se tudo falhar, cai para áudio local.
    """
    if USE_REMOTE_AUDIO:
        logger.info("Tentando áudio remoto: %s", query)

        def tentar_remoto():
            return buscar_audio_freesound(query, diretorio, sort, additional_filters, max_retries=1)

        # fazemos 3 rodadas "externas" para variar um pouco a seleção
        for i in range(MAX_RETRIES_DEFAULT):
            caminho = _retry(tentar_remoto, tries=1, on_error_msg=f"Rodada remota {i+1}")
            if caminho:
                return caminho
            # pequeno atraso entre rodadas
            time.sleep(0.5 + random.random() * 0.5)

        logger.warning("Falha no remoto após retries; tentando áudio local…")
        try:
            return escolher_audio_local(diretorio)
        except Exception as e:
            logger.error("Sem áudio local disponível: %s", e)
            raise
    else:
        logger.info("Usando áudio local (USE_REMOTE_AUDIO=False)")
        return escolher_audio_local(diretorio)

# =============================================================================
# TTS (Gemini = padrão) + ElevenLabs como alternativa (com retries)
# =============================================================================

# ---- Gemini TTS (novo SDK google-genai) ----
_HAS_GOOGLE_GENAI = True
try:
    from google import genai as genai_new
    from google.genai import types as genai_types
except Exception:
    _HAS_GOOGLE_GENAI = False

def _wav_write(filename: str, pcm_bytes: bytes, channels=1, rate=24000, sample_width=2):
    os.makedirs(os.path.dirname(filename) or ".", exist_ok=True)
    with wave.open(filename, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(rate)
        wf.writeframes(pcm_bytes)

def _tts_filename(lang: str, ext: str = "wav") -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(PASTA_TTS, f"tts_{lang}_{ts}.{ext}")

def gerar_narracao_tts_gemini(texto: str, idioma: str = "en",
                              voice_name: Optional[str] = None,
                              model: Optional[str] = None,
                              *,
                              max_retries: int = MAX_RETRIES_DEFAULT) -> Optional[str]:
    """
    Gera WAV (PCM 24 kHz, mono) via Gemini TTS com retries.
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
    default_voice_map = {"en": "Kore", "pt": "Sadaltager"}
    lang = "pt" if (idioma or "").lower().startswith("pt") else "en"
    voice_name = voice_name or os.getenv("GEMINI_TTS_VOICE") or default_voice_map.get(lang, "Kore")

    client = genai_new.Client(api_key=api_key)

    def tentar() -> str:
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

        data = resp.candidates[0].content.parts[0].inline_data.data
        pcm_bytes = base64.b64decode(data) if isinstance(data, str) else data
        out_path = _tts_filename(lang, "wav")
        _wav_write(out_path, pcm_bytes, channels=1, rate=24000, sample_width=2)
        return out_path

    path = _retry(tentar, tries=max_retries, on_error_msg="Falha no TTS Gemini")
    if path:
        logger.info("🎧 TTS (Gemini) gerado: %s", path)
    return path

# ---- ElevenLabs (SDK oficial) ----
_HAS_ELEVENLABS = True
try:
    from elevenlabs.client import ElevenLabs
except Exception:
    _HAS_ELEVENLABS = False

def gerar_narracao_tts_elevenlabs(texto: str, idioma: str = "en",
                                  *,
                                  max_retries: int = MAX_RETRIES_DEFAULT) -> Optional[str]:
    """
    Gera MP3 via ElevenLabs com retries. Requer ELEVENLABS_API_KEY.
    """
    if not _HAS_ELEVENLABS:
        logger.warning("Pacote 'elevenlabs' não instalado. pip install elevenlabs")
        return None

    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        logger.warning("ELEVENLABS_API_KEY ausente.")
        return None

    voice_ids = {
        "en": os.getenv("ELEVENLABS_VOICE_EN", "y2Y5MeVPm6ZQXK64WUui"),
        "pt": os.getenv("ELEVENLABS_VOICE_PT", "rnJZLKxtlBZt77uIED10"),
    }
    lang = "pt" if (idioma or "").lower().startswith("pt") else "en"
    voice_id = voice_ids.get(lang, voice_ids["en"])

    client = ElevenLabs(api_key=api_key)

    def tentar() -> str:
        stream = client.text_to_speech.stream(text=texto, voice_id=voice_id, model_id="eleven_multilingual_v2")
        out_path = _tts_filename(lang, "mp3")
        with open(out_path, "wb") as f:
            for chunk in stream:
                if isinstance(chunk, bytes) and chunk:
                    f.write(chunk)
        if not os.path.exists(out_path) or os.path.getsize(out_path) < 1024:
            raise RuntimeError("Arquivo MP3 inválido/pequeno.")
        return out_path

    path = _retry(tentar, tries=max_retries, on_error_msg="Erro ElevenLabs")
    if path:
        logger.info("🎧 TTS (ElevenLabs) gerado: %s", path)
    return path

# ---- Facade para escolher motor TTS ----
def gerar_narracao_tts(texto: str, idioma: str = "en", engine: str = "gemini") -> Optional[str]:
    """
    Gera narração TTS com o motor escolhido.
      engine = "gemini" (padrão) | "elevenlabs"
    Tenta retries no motor escolhido e, se falhar, faz fallback para o outro (também com retries).
    """
    engine = (engine or "gemini").strip().lower()

    if engine == "gemini":
        path = gerar_narracao_tts_gemini(texto, idioma=idioma, max_retries=MAX_RETRIES_DEFAULT)
        if path:
            return path
        logger.info("Fallback: tentando ElevenLabs…")
        return gerar_narracao_tts_elevenlabs(texto, idioma=idioma, max_retries=MAX_RETRIES_DEFAULT)
    else:
        path = gerar_narracao_tts_elevenlabs(texto, idioma=idioma, max_retries=MAX_RETRIES_DEFAULT)
        if path:
            return path
        logger.info("Fallback: tentando Gemini…")
        return gerar_narracao_tts_gemini(texto, idioma=idioma, max_retries=MAX_RETRIES_DEFAULT)
