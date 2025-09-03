# utils/tiktok.py
import os
import re
import time
from datetime import datetime, timedelta
from typing import Optional

from .tiktok_uploader.upload import upload_video  # Import local da pasta tiktok_uploader
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from socket import error as SocketError
import logging
import shutil

# >>> cache por idioma (quando disponível)
try:
    from cache_store import cache
except Exception:
    cache = None

# Configuração do logging com timestamps
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

PASTA_VIDEOS = "videos"
PASTA_IMAGENS = "imagens"
PASTA_AUDIOS = "audios"
PASTA_CACHE  = os.getenv("CACHE_DIR", "cache")

# Controla se removemos marcações markdown da legenda
_STRIP_FLAG = os.getenv("STRIP_MARKDOWN_IN_DESC", "1").strip().lower()
STRIP_MARKDOWN_IN_DESC = _STRIP_FLAG not in ("0", "false", "no", "off")

# --- helper para deduplicar hashtags mantendo ordem ---
_HASHTAG_RE = re.compile(r'(?<!\S)#([^\s#]+)', flags=re.UNICODE)


def _dedupe_hashtags_in_desc(desc: str, max_n: Optional[int] = None) -> str:
    """
    Remove hashtags duplicadas (case-insensitive) mantendo a ordem da 1ª ocorrência.
    Remove as hashtags do corpo e as recoloca no final, já deduplicadas.
    """
    if not desc:
        return ""

    tags_encontradas = ["#" + m.group(1) for m in _HASHTAG_RE.finditer(desc)]

    seen = set()
    ordered = []
    for t in tags_encontradas:
        k = t.lower()
        if k not in seen and len(t) > 1:
            seen.add(k)
            ordered.append(t)

    if isinstance(max_n, int) and max_n >= 0:
        ordered = ordered[:max_n]

    base = _HASHTAG_RE.sub("", desc)
    base = re.sub(r"\s{2,}", " ", base).strip()

    return (base + " " + " ".join(ordered)).strip() if ordered else base


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


def _safe_remove(path: str) -> None:
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            logger.info("🗑️ Pasta removida: %s", path)
        elif os.path.isfile(path):
            os.remove(path)
            logger.info("🗑️ Arquivo removido: %s", path)
    except Exception as e:
        logger.debug("Não consegui remover %s (%s)", path, e)


def _cleanup_cache_drawtext(cache_dir: str) -> None:
    """Remove arquivos temporários gerados para drawtext/overlays, mantendo JSONs."""
    try:
        if not os.path.isdir(cache_dir):
            return
        for f in os.listdir(cache_dir):
            if f.startswith("drawtext_") and f.endswith(".txt"):
                _safe_remove(os.path.join(cache_dir, f))
            elif f.startswith("title_overlay_") and f.endswith(".png"):
                _safe_remove(os.path.join(cache_dir, f))
            elif f == "last_filter.txt":
                _safe_remove(os.path.join(cache_dir, f))
    except Exception as e:
        logger.debug("Falha limpando temporários do cache (%s): %s", cache_dir, e)


def _cleanup_mid_artifacts() -> None:
    """
    Faxina robusta e conservadora para pós-upload OU uso manual:
    - Remove imagens do diretório IMAGENS.
    - Remove todos os áudios do diretório AUDIOS (inclui subpasta tts).
    - Remove temporários de cache (drawtext_*.txt, title_overlay_*.png, last_filter.txt).
    """
    try:
        # imagens/*
        if os.path.isdir(PASTA_IMAGENS):
            for f in os.listdir(PASTA_IMAGENS):
                _safe_remove(os.path.join(PASTA_IMAGENS, f))

        # audios/*  e audios/tts/*
        if os.path.isdir(PASTA_AUDIOS):
            for root, dirs, files in os.walk(PASTA_AUDIOS, topdown=False):
                for name in files:
                    if any(name.lower().endswith(ext) for ext in (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")):
                        _safe_remove(os.path.join(root, name))
                # limpa pastas vazias (inclusive tts)
                for d in dirs:
                    try:
                        full = os.path.join(root, d)
                        if not os.listdir(full):
                            _safe_remove(full)
                    except Exception:
                        pass

        # cache/drawtext_*.txt e title_overlay_*.png
        _cleanup_cache_drawtext(PASTA_CACHE)

    except Exception as e:
        logger.debug("Falha na limpeza intermediária: %s", e)


def _cleanup_prompts_and_video(video_path: Optional[str]) -> None:
    """
    Após post bem-sucedido:
    - Remove 'videos/prompts/' inteiro (se existir).
    - Remove o próprio arquivo de vídeo postado (se existir).
    - Executa limpeza intermediária por segurança.
    """
    try:
        prompts_dir = os.path.join(PASTA_VIDEOS, "prompts")
        if os.path.isdir(prompts_dir):
            _safe_remove(prompts_dir)

        if video_path and os.path.isfile(video_path):
            _safe_remove(video_path)

        # Por redundância (casos de reuso/erros no fluxo), limpa temporários também aqui.
        _cleanup_mid_artifacts()

    except Exception as e:
        logger.debug("Falha limpando prompts/vídeo: %s", e)


def obter_ultimo_video(pasta=PASTA_VIDEOS):
    """Encontra o vídeo mais recente na pasta de vídeos (baseado na data de modificação)."""
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


def _strip_markdown(texto: str) -> str:
    """Remove marcações básicas de Markdown e normaliza espaços."""
    _MD_PATTERNS = (
        (re.compile(r"\*\*(.+?)\*\*", re.DOTALL), r"\1"),
        (re.compile(r"\*(.+?)\*",     re.DOTALL), r"\1"),
        (re.compile(r"__(.+?)__",     re.DOTALL), r"\1"),
        (re.compile(r"_(.+?)_",       re.DOTALL), r"\1"),
        (re.compile(r"~~(.+?)~~",     re.DOTALL), r"\1"),
        (re.compile(r"`([^`]+)`",     re.DOTALL), r"\1"),
    )
    s = (texto or "")
    for pat, rep in _MD_PATTERNS:
        s = pat.sub(rep, s)
    s = s.replace("“", "\"").replace("”", "\"").replace("’", "'")
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()


def postar_no_tiktok_e_renomear(
    descricao_personalizada=None,
    imagem_base=None,
    imagem_final=None,
    video_final=None,
    agendar=False,
    idioma='en'
) -> bool:
    """
    Busca o último vídeo gerado, posta no TikTok e limpa os arquivos gerados APÓS upload sem erro.
    NÃO abre navegador extra de confirmação.
    Retorna True se o fluxo de upload terminou sem exceção; False caso contrário.
    """
    idioma_norm = _normalizar_idioma(idioma)
    logger.info("postar_no_tiktok_e_renomear: idioma_in=%s | idioma_norm=%s", idioma, idioma_norm)

    video_path = video_final if video_final else obter_ultimo_video()
    if not video_path:
        return False

    try:
        if os.path.getsize(video_path) <= 0:
            logger.error("❌ Vídeo está com 0 bytes localmente: %s", video_path)
            return False
    except Exception:
        pass

    # Seleciona cookies por idioma (env-first)
    cookies_map = {
        "en":    os.getenv("COOKIES_US_FILENAME", "cookies_us.txt"),
        "pt-br": os.getenv("COOKIES_BR_FILENAME", "cookies_br.txt"),
        "ar":    os.getenv("COOKIES_EG_FILENAME", "cookies_eg.txt"),
    }
    COOKIES_PATH = cookies_map.get(idioma_norm, os.getenv("COOKIES_US_FILENAME", "cookies_us.txt"))
    logger.info("🍪 Cookies utilizados: %s", COOKIES_PATH)

    if not os.path.exists(COOKIES_PATH):
        logger.error(f"❌ Arquivo de cookies não encontrado: {COOKIES_PATH}")
        return False

    try:
        # Descrição base
        if descricao_personalizada:
            base_desc = descricao_personalizada
        else:
            if idioma_norm == "pt-br":
                base_desc = "Conteúdo motivacional do dia!"
            elif idioma_norm == "ar":
                base_desc = "رسالة اليوم ✨"
            else:
                base_desc = "Motivational content of the day!"

        # Limpa markdown, se habilitado
        if STRIP_MARKDOWN_IN_DESC:
            cleaned = _strip_markdown(base_desc)
            if cleaned != base_desc:
                logger.debug("🧹 Limpando markdown da descrição: '%s' -> '%s'", base_desc, cleaned)
            base_desc = cleaned

        # Dedup de hashtags, mantendo só as que já vieram
        description = _dedupe_hashtags_in_desc(base_desc)

        # >>> grava no cache por idioma (para o Gemini não repetir depois)
        if cache:
            cache.add("used_phrases", description, lang=idioma_norm)

        schedule = None
        if agendar:
            schedule = datetime.now() + timedelta(minutes=20)
            logger.info("📅 Agendando post para: %s", schedule.strftime("%H:%M:%S"))

        logger.info("🚀 Postando vídeo no TikTok: %s", video_path)
        logger.info("📝 Descrição final: %s", description)
        time.sleep(1.2)

        upload_video(
            filename=video_path,
            description=description,
            cookies=COOKIES_PATH,
            comment=True,
            stitch=True,
            duet=True,
            headless=True,  # controle de headless é feito upstream; mantemos ON por padrão aqui
            schedule=schedule,
            idioma=idioma_norm
        )

        ok = True

        # --- PÓS-POST: limpeza completa e conservadora ---
        # Remove arquivos auxiliares passados como parâmetros (se existirem)
        if imagem_base and os.path.exists(imagem_base):
            _safe_remove(imagem_base)
        if imagem_final and os.path.exists(imagem_final):
            _safe_remove(imagem_final)
        if video_final and os.path.exists(video_final):
            _safe_remove(video_final)

        # Faxina intermediária robusta (imagens/, audios/, cache temporários)
        _cleanup_mid_artifacts()

        # Apaga prompts (videos/prompts) e o próprio vídeo postado
        _cleanup_prompts_and_video(video_path)

        return ok

    except (NoSuchElementException, TimeoutException) as e:
        logger.warning("⚠️ Erro intermediário durante upload: %s", e)
        return False
    except SocketError as e:
        if getattr(e, "errno", None) == 10054:
            logger.warning("⚠️ Conexão resetada (10054). Pode ter concluído, mas não confirmamos. Mantendo arquivos locais.")
            return False
        logger.error("❌ Erro de socket inesperado: %s", e)
        return False
    except WebDriverException as e:
        logger.error("❌ Erro no WebDriver durante upload: %s", e)
        return False
    except Exception as e:
        logger.error("❌ Erro geral ao postar: %s", e)
        return False
    finally:
        logger.info("⏳ Aguardando 5 segundos antes de finalizar...")
        time.sleep(5)
