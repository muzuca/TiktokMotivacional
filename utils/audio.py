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
import shutil
from typing import Optional, Callable, TypeVar

# moviepy - usa o editor "novo" se disponível
try:
    from moviepy import AudioFileClip  # type: ignore
except Exception:  # pragma: no cover
    from moviepy.editor import AudioFileClip  # type: ignore

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ====================== CONFIG ======================
USE_REMOTE_AUDIO = os.getenv("USE_REMOTE_AUDIO", "1").strip() not in ("0", "false", "no")
FREESOUND_API_KEY = os.getenv("FREESOUND_API_KEY")
DURACAO_MINIMA = int(os.getenv("BG_MIN_SECONDS", "30"))

AUDIO_DIR = os.getenv("AUDIO_DIR", "audios")
TTS_DIR = os.path.join(AUDIO_DIR, "tts")
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(TTS_DIR, exist_ok=True)

CACHE_DIR = "cache"
os.makedirs(CACHE_DIR, exist_ok=True)
AUDIOS_CACHE_FILE = os.path.join(CACHE_DIR, "used_audios.json")

USER_AGENT = "TiktokMotivacional/1.0 (+https://local)"

# ---------- Proxy region-aware ----------
# Regras:
# - Padrão: usa PROXY_HOST/PORT/USER/PASS (EUA)
# - Para Egito: usa PROXY_HOST_EG/PORT_EG/USER_EG/PASS_EG
# - Auto por idioma: se começar com 'ar' -> usar região 'EG' (pode desativar com PROXY_AUTO_BY_LANG=0)
PROXY_AUTO_BY_LANG = os.getenv("PROXY_AUTO_BY_LANG", "1").strip().lower() not in ("0", "false", "no")
DEFAULT_PROXY_REGION = (os.getenv("DEFAULT_PROXY_REGION", "") or "").upper() or None  # ex: 'US' ou 'EG'

def _proxy_url_from_env(prefix: str) -> Optional[str]:
    host = os.getenv(f"{prefix}_HOST", "").strip()
    port = os.getenv(f"{prefix}_PORT", "").strip()
    user = os.getenv(f"{prefix}_USER", "").strip()
    pw   = os.getenv(f"{prefix}_PASS", "").strip()
    if not host or not port:
        return None
    auth = f"{user}:{pw}@" if (user or pw) else ""
    return f"http://{auth}{host}:{port}"

def _pick_proxy_region(explicit_region: Optional[str], idioma: Optional[str]) -> Optional[str]:
    if explicit_region:
        return explicit_region.upper()
    if PROXY_AUTO_BY_LANG and (idioma or "").strip().lower().startswith("ar"):
        return "EG"
    return DEFAULT_PROXY_REGION

def _apply_env_proxy(region: Optional[str]) -> dict:
    """
    Define HTTP(S)_PROXY no ambiente (também em minúsculas) e retorna
    um dict com os valores antigos para restauração.
    """
    old = {
        "HTTP_PROXY": os.environ.get("HTTP_PROXY"),
        "HTTPS_PROXY": os.environ.get("HTTPS_PROXY"),
        "http_proxy": os.environ.get("http_proxy"),
        "https_proxy": os.environ.get("https_proxy"),
    }
    prefix = "PROXY_EG" if (region or "").upper() == "EG" else "PROXY"
    url = _proxy_url_from_env(prefix)
    for k in ("HTTP_PROXY","HTTPS_PROXY","http_proxy","https_proxy"):
        if url:
            os.environ[k] = url
        else:
            os.environ.pop(k, None)
    return old

def _restore_env_proxy(old: dict):
    for k, v in (old or {}).items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

# ================== utils de cache ==================
def load_used_audios():
    if os.path.exists(AUDIOS_CACHE_FILE):
        try:
            with open(AUDIOS_CACHE_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_used_audios(used_audios):
    with open(AUDIOS_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(list(used_audios), f)

# ================== retry/backoff ===================
T = TypeVar("T")
MAX_RETRIES_DEFAULT = 3

def _retry(fn: Callable[[], T],
           *,
           tries: int = MAX_RETRIES_DEFAULT,
           base_sleep: float = 0.8,
           jitter: float = 0.35,
           on_error_msg: str | None = None) -> T | None:
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

# ================== HTTP session (proxy + retries) ==================
from requests.adapters import HTTPAdapter, Retry
_SESSIONS: dict[str, requests.Session] = {}

def _make_session(region: Optional[str] = None) -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.7, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))

    prefix = "PROXY_EG" if (region or "").upper() == "EG" else "PROXY"
    url = _proxy_url_from_env(prefix)
    if url:
        s.proxies.update({"http": url, "https": url})
    s.headers.update({"User-Agent": USER_AGENT})
    return s

def _get_session(region: Optional[str]) -> requests.Session:
    key = (region or "DEFAULT").upper()
    if key not in _SESSIONS:
        _SESSIONS[key] = _make_session(region)
    return _SESSIONS[key]

# ================= ÁUDIO DE FUNDO ===================
def _duracao_arquivo(path: str) -> float:
    try:
        with AudioFileClip(path) as clip:
            return float(clip.duration or 0.0)
    except Exception:
        return 0.0

def escolher_audio_local(diretorio=AUDIO_DIR):
    formatos = (".mp3", ".wav", ".ogg")
    musicas_validas = []
    try:
        for f in os.listdir(diretorio):
            if f.lower().endswith(formatos):
                caminho = os.path.join(diretorio, f)
                dur = _duracao_arquivo(caminho)
                if dur >= DURACAO_MINIMA:
                    musicas_validas.append((caminho, dur))
    except FileNotFoundError:
        pass

    if not musicas_validas:
        raise FileNotFoundError(f"Nenhum áudio >= {DURACAO_MINIMA}s em {diretorio}")

    used = load_used_audios()
    nao_usadas = [(c, d) for c, d in musicas_validas if c not in used] or musicas_validas
    escolhido, duracao = random.choice(nao_usadas)
    used.add(escolhido)
    save_used_audios(used)
    logger.info("🎵 Áudio local: %s (%.0fs)", escolhido, duracao)
    return escolhido

def _freesound_busca(session: requests.Session, query: str, sort: str, filters: str, page_size: int = 20) -> dict:
    url = "https://freesound.org/apiv2/search/text/"
    headers = {"Authorization": f"Token {FREESOUND_API_KEY}"}
    params = {
        "query": query,
        "filter": filters,
        "sort": sort,
        "fields": "id,name,duration,tags,license,previews",
        "page_size": page_size,
    }
    r = session.get(url, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def _baixar_preview(session: requests.Session, audio_meta: dict, destino: str) -> float:
    os.makedirs(os.path.dirname(destino) or ".", exist_ok=True)
    preview_url = audio_meta["previews"]["preview-hq-mp3"]
    with session.get(preview_url, stream=True, timeout=30) as resp:
        resp.raise_for_status()
        with open(destino, "wb") as f:
            for chunk in resp.iter_content(8192):
                if chunk:
                    f.write(chunk)
    dur = _duracao_arquivo(destino)
    if dur < DURACAO_MINIMA:
        try: os.remove(destino)
        except Exception: pass
        raise ValueError(f"Áudio baixado curto ({dur:.0f}s).")
    return dur

def buscar_audio_freesound(
    query="inspirational",
    output_dir=AUDIO_DIR,
    sort="rating_desc",
    additional_filters='tag:music tag:instrumental -tag:speech -tag:voice',
    *,
    proxy_region: Optional[str] = None,
    idioma: Optional[str] = None,
    max_retries: int = MAX_RETRIES_DEFAULT
):
    if not FREESOUND_API_KEY:
        raise ValueError("FREESOUND_API_KEY não configurada no .env.")

    region = _pick_proxy_region(proxy_region, idioma)
    session = _get_session(region)

    def tentar() -> str:
        logger.info("🔍 Freesound (%s): query='%s', sort='%s', filtros='%s'", region or "DEFAULT", query, sort, additional_filters)
        filters = f"duration:[{DURACAO_MINIMA} TO 300] {additional_filters}".strip()
        data = _freesound_busca(session, query, sort, filters)
        if not data.get("results"):
            logger.warning("Sem resultados; afrouxando filtros…")
            filters = f"duration:[{DURACAO_MINIMA} TO 300] tag:music -tag:speech"
            data = _freesound_busca(session, query, sort, filters)
        if not data.get("results"):
            data = _freesound_busca(session, "uplifting music", sort, f"duration:[{DURACAO_MINIMA} TO 300] tag:music -tag:speech")
        if not data.get("results"):
            data = _freesound_busca(session, query, sort, f"duration:[{DURACAO_MINIMA} TO 300]")

        used = load_used_audios()
        resultados = data.get("results", [])
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
            dur = _baixar_preview(session, audio, caminho)

        used.add(str(audio_id))
        save_used_audios(used)
        logger.info("✅ Pronto: %s (%.0fs, lic: %s)", caminho, dur, audio.get("license", "N/A"))
        return caminho

    caminho = _retry(tentar, tries=max_retries, on_error_msg="Erro Freesound")
    if caminho is None:
        raise RuntimeError("Falha ao obter áudio do Freesound após múltiplas tentativas.")
    return caminho

def obter_caminho_audio(query="inspirational", diretorio=AUDIO_DIR,
                        sort="rating_desc",
                        additional_filters='tag:music tag:instrumental -tag:speech -tag:voice',
                        *,
                        proxy_region: Optional[str] = None,
                        idioma: Optional[str] = None):
    region = _pick_proxy_region(proxy_region, idioma)
    if USE_REMOTE_AUDIO:
        logger.info("Tentando áudio remoto: %s (proxy=%s)", query, region or "DEFAULT")

        def tentar_remoto():
            return buscar_audio_freesound(query, diretorio, sort, additional_filters,
                                          proxy_region=region, idioma=idioma, max_retries=1)

        for i in range(MAX_RETRIES_DEFAULT):
            caminho = _retry(tentar_remoto, tries=1, on_error_msg=f"Rodada remota {i+1}")
            if caminho:
                return caminho
            time.sleep(0.5 + random.random() * 0.5)

        logger.warning("Falha no remoto; tentando áudio local…")
        return escolher_audio_local(diretorio)
    else:
        logger.info("Usando áudio local (USE_REMOTE_AUDIO=False)")
        return escolher_audio_local(diretorio)

# ======================== TTS =======================
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

def _tts_outname(lang_key: str, ext: str = "wav") -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(TTS_DIR, f"tts_{lang_key}_{ts}.{ext}")

def _lang_key_from(idioma: Optional[str]) -> str:
    s = (idioma or "").strip().lower()
    if s.startswith("pt"):
        return "pt"
    if s.startswith("ar"):
        return "ar"
    return "en"  # default EUA

def gerar_narracao_tts_gemini(texto: str, idioma: str = "en",
                              voice_name: Optional[str] = None,
                              model: Optional[str] = None,
                              *,
                              max_retries: int = MAX_RETRIES_DEFAULT) -> Optional[str]:
    if not _HAS_GOOGLE_GENAI:
        logger.warning("Pacote 'google-genai' não instalado. pip install google-genai")
        return None

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY ausente. TTS Gemini indisponível.")
        return None

    model = model or os.getenv("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")

    # ======= MAPA DE VOZES (pedido do cliente) =======
    # EUA/en -> Sadachbia | PT -> Sadaltager | EG/ar -> Kore
    lang_key = _lang_key_from(idioma)
    default_voice_map = {
        "en": "Sadachbia",
        "pt": "Sadaltager",
        "ar": "Kore",
    }
    # Permite overrides por env, se quiser trocar sem mexer no código
    env_override = (
        os.getenv(f"GEMINI_TTS_VOICE_{lang_key.upper()}") or
        os.getenv("GEMINI_TTS_VOICE")
    )
    voice_name = voice_name or env_override or default_voice_map[lang_key]

    client = genai_new.Client(api_key=api_key)
    # aplica proxy no ambiente se necessário (para libs que não usam requests)
    region = _pick_proxy_region(None, idioma)
    old_env = _apply_env_proxy(region)

    try:
        def tentar() -> str:
            resp = client.models.generate_content(
                model=model,
                contents=texto,
                config=genai_types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=genai_types.SpeechConfig(
                        voice_config=genai_types.VoiceConfig(
                            prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(voice_name=voice_name)
                        )
                    ),
                ),
            )
            data = resp.candidates[0].content.parts[0].inline_data.data
            pcm_bytes = base64.b64decode(data) if isinstance(data, str) else data
            out_path = _tts_outname(lang_key, "wav")
            _wav_write(out_path, pcm_bytes, channels=1, rate=24000, sample_width=2)
            logger.info("🎧 TTS (Gemini): idioma=%s | voice=%s | arquivo=%s", lang_key, voice_name, out_path)
            return out_path

        path = _retry(tentar, tries=max_retries, on_error_msg="Falha no TTS Gemini")
        if path:
            logger.info("🎧 TTS (Gemini) gerado com sucesso.")
        return path
    finally:
        _restore_env_proxy(old_env)

_HAS_ELEVENLABS = True
try:
    from elevenlabs.client import ElevenLabs
except Exception:
    _HAS_ELEVENLABS = False

def gerar_narracao_tts_elevenlabs(texto: str, idioma: str = "en",
                                  *,
                                  max_retries: int = MAX_RETRIES_DEFAULT) -> Optional[str]:
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
    lang_key = "pt" if (idioma or "").lower().startswith("pt") else "en"
    voice_id = voice_ids.get(lang_key, voice_ids["en"])

    # aplica proxy no ambiente se necessário (lib externa)
    region = _pick_proxy_region(None, idioma)
    old_env = _apply_env_proxy(region)

    try:
        client = ElevenLabs(api_key=api_key)

        def tentar() -> str:
            stream = client.text_to_speech.stream(text=texto, voice_id=voice_id, model_id="eleven_multilingual_v2")
            out_path = _tts_outname(lang_key, "mp3")
            with open(out_path, "wb") as f:
                for chunk in stream:
                    if isinstance(chunk, bytes) and chunk:
                        f.write(chunk)
            if not os.path.exists(out_path) or os.path.getsize(out_path) < 1024:
                raise RuntimeError("Arquivo MP3 inválido/pequeno.")
            logger.info("🎧 TTS (ElevenLabs): idioma=%s | voice_id=%s | arquivo=%s", lang_key, voice_id, out_path)
            return out_path

        path = _retry(tentar, tries=max_retries, on_error_msg="Erro ElevenLabs")
        if path:
            logger.info("🎧 TTS (ElevenLabs) gerado com sucesso.")
        return path
    finally:
        _restore_env_proxy(old_env)

def gerar_narracao_tts(texto: str, idioma: str = "en", engine: str = "gemini") -> Optional[str]:
    engine = (engine or "gemini").strip().lower()
    if engine == "gemini":
        path = gerar_narracao_tts_gemini(texto, idioma=idioma, max_retries=MAX_RETRIES_DEFAULT)
        if path: return path
        logger.info("Fallback: tentando ElevenLabs…")
        return gerar_narracao_tts_elevenlabs(texto, idioma=idioma, max_retries=MAX_RETRIES_DEFAULT)
    else:
        path = gerar_narracao_tts_elevenlabs(texto, idioma=idioma, max_retries=MAX_RETRIES_DEFAULT)
        if path: return path
        logger.info("Fallback: tentando Gemini…")
        return gerar_narracao_tts_gemini(texto, idioma=idioma, max_retries=MAX_RETRIES_DEFAULT)

# ============= Helpers públicos de duração/geração (para main.py) =============
def duracao_arquivo(path: str) -> float:
    """Duração em segundos do arquivo de áudio (usa moviepy). Retorna 0.0 se falhar."""
    try:
        return _duracao_arquivo(path)
    except Exception:
        return 0.0

def gerar_narracao_tts_com_duracao(texto: str, idioma: str = "en", tts_engine: str = "gemini"):
    """
    Gera o TTS com o engine solicitado e retorna (path, duracao_em_segundos).
    Mantém a sua pipeline (proxy, retries). Útil para calcular slides dinamicamente.
    """
    path = gerar_narracao_tts(texto, idioma=idioma, engine=tts_engine)
    if not path or not os.path.exists(path):
        raise RuntimeError("Falha ao gerar TTS para cálculo dinâmico de slides.")
    dur = _duracao_arquivo(path)
    logger.info("🎙️ Duração da voz (ffprobe): %.2fs", dur)
    return path, float(dur or 0.0)

# ------------- util opcional: limpar TTS ------------
def limpar_tts_antigos(max_age_hours: int = 6):
    """Remove arquivos em audios/tts/ mais antigos que N horas."""
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for f in os.listdir(TTS_DIR):
        p = os.path.join(TTS_DIR, f)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
                removed += 1
        except Exception:
            pass
    if removed:
        logger.info("🧹 TTS antigos removidos: %d", removed)
