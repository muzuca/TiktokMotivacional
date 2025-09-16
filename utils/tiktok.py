# utils/tiktok.py
import os
import re
import time
import shutil
import logging
from datetime import datetime, timedelta
from typing import Optional
from socket import error as SocketError

from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException

# Uploader local
# Importamos a exceção FailedToUpload para um tratamento mais específico
from .tiktok_uploader.upload import upload_video, FailedToUpload

# Países / cookies / diretórios centralizados
from .countries import (
    normalize_lang, cookies_path_for,
)

try:
    from cache_store import cache
except Exception:
    cache = None

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

PASTA_VIDEOS  = "videos"
PASTA_IMAGENS = "imagens"
PASTA_AUDIOS  = "audios"
PASTA_CACHE   = os.getenv("CACHE_DIR", "cache")

_STRIP_FLAG = os.getenv("STRIP_MARKDOWN_IN_DESC", "1").strip().lower()
STRIP_MARKDOWN_IN_DESC = _STRIP_FLAG not in ("0", "false", "no", "off")

_HASHTAG_RE = re.compile(r'(?<!\S)#([^\s#]+)', flags=re.UNICODE)

def _dedupe_hashtags_in_desc(desc: str, max_n: Optional[int] = None) -> str:
    if not desc:
        return ""
    tags = ["#" + m.group(1) for m in _HASHTAG_RE.finditer(desc)]
    seen = set()
    ordered = []
    for t in tags:
        k = t.lower()
        if k not in seen and len(t) > 1:
            seen.add(k)
            ordered.append(t)
    if isinstance(max_n, int) and max_n >= 0:
        ordered = ordered[:max_n]
    base = _HASHTAG_RE.sub("", desc)
    base = re.sub(r"\s{2,}", " ", base).strip()
    return (base + " " + " ".join(ordered)).strip() if ordered else base

def _safe_remove(path: str) -> None:
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            logger.debug("🗑️ Pasta removida: %s", path)
        elif os.path.isfile(path):
            os.remove(path)
            logger.debug("🗑️ Arquivo removido: %s", path)
    except Exception as e:
        logger.debug("Não consegui remover %s (%s)", path, e)

def _cleanup_cache_drawtext(cache_dir: str) -> None:
    try:
        if not os.path.isdir(cache_dir): return
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
    """Limpa imagens/, audios/ (inclui tts/) e temporários do cache."""
    try:
        if os.path.isdir(PASTA_IMAGENS):
            for f in os.listdir(PASTA_IMAGENS):
                _safe_remove(os.path.join(PASTA_IMAGENS, f))
        if os.path.isdir(PASTA_AUDIOS):
            for root, dirs, files in os.walk(PASTA_AUDIOS, topdown=False):
                for name in files:
                    if any(name.lower().endswith(ext) for ext in (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")):
                        _safe_remove(os.path.join(root, name))
                for d in dirs:
                    try:
                        full = os.path.join(root, d)
                        if not os.listdir(full):
                            _safe_remove(full)
                    except Exception:
                        pass
        _cleanup_cache_drawtext(PASTA_CACHE)
    except Exception as e:
        logger.debug("Falha na limpeza intermediária: %s", e)

def _cleanup_prompts_and_video(video_path: Optional[str]) -> None:
    """Após post, remove videos/prompts e o próprio vídeo postado."""
    try:
        prompts_dir = os.path.join(PASTA_VIDEOS, "prompts")
        if os.path.isdir(prompts_dir):
            _safe_remove(prompts_dir)
        if video_path and os.path.isfile(video_path):
            _safe_remove(video_path)
        _cleanup_mid_artifacts()
    except Exception as e:
        logger.debug("Falha limpando prompts/vídeo: %s", e)

def _strip_markdown(texto: str) -> str:
    patterns = (
        (re.compile(r"\*\*(.+?)\*\*", re.DOTALL), r"\1"),
        (re.compile(r"\*(.+?)\*",     re.DOTALL), r"\1"),
        (re.compile(r"__(.+?)__",     re.DOTALL), r"\1"),
        (re.compile(r"_(.+?)_",       re.DOTALL), r"\1"),
        (re.compile(r"~~(.+?)~~",     re.DOTALL), r"\1"),
        (re.compile(r"`([^`]+)`",     re.DOTALL), r"\1"),
    )
    s = (texto or "")
    for pat, rep in patterns:
        s = pat.sub(rep, s)
    s = s.replace("“", "\"").replace("”", "\"").replace("’", "'")
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()

def obter_ultimo_video(pasta=PASTA_VIDEOS) -> Optional[str]:
    try:
        arquivos = [os.path.join(pasta, f) for f in os.listdir(pasta) if f.endswith(".mp4")]
        if not arquivos:
            raise FileNotFoundError(f"⚠️ Nenhum vídeo novo encontrado em {pasta}.")
        ultimo_video = max(arquivos, key=os.path.getmtime)
        logger.info("📹 Último vídeo encontrado: %s", ultimo_video)
        return ultimo_video
    except Exception as e:
        logger.error("❌ Erro ao buscar último vídeo: %s", str(e))
        return None

def postar_no_tiktok_e_renomear(
    descricao_personalizada: Optional[str] = None,
    imagem_base: Optional[str] = None,
    imagem_final: Optional[str] = None,
    video_final: Optional[str] = None,
    agendar: bool = False,
    idioma: str = "en",
    max_upload_attempts: int = 3,
    use_vpn: bool = False,
) -> bool:
    lang = normalize_lang(idioma)
    logger.info("postar_no_tiktok_e_renomear: idioma_in=%s | idioma_norm=%s | use_vpn=%s", idioma, lang, use_vpn)

    video_path = video_final if video_final else obter_ultimo_video()
    if not video_path:
        return False
    try:
        if os.path.getsize(video_path) <= 0:
            logger.error("❌ Vídeo está com 0 bytes: %s", video_path)
            return False
    except Exception:
        pass

    COOKIES_PATH = cookies_path_for(lang)
    logger.info("🍪 Cookies utilizados: %s", COOKIES_PATH)
    if not os.path.exists(COOKIES_PATH):
        logger.error("❌ Arquivo de cookies não encontrado: %s", COOKIES_PATH)
        return False

    if descricao_personalizada:
        base_desc = descricao_personalizada
    else:
        base_desc = "Conteúdo do dia!"

    if STRIP_MARKDOWN_IN_DESC:
        base_desc = _strip_markdown(base_desc)
    description = _dedupe_hashtags_in_desc(base_desc)

    if cache:
        try:
            cache.add("used_phrases", description, lang=lang)
        except Exception:
            pass

    if use_vpn:
        tt_headless = False
        logger.info("🌍 VPN ativada, forçando modo não-headless.")
    else:
        tt_headless = os.getenv('HEADLESS_UPLOAD', '0').strip() != '0'
    logger.info("🤖 TikTok headless: %s", "ON" if tt_headless else "OFF")

    logger.info("🚀 Postando vídeo no TikTok: %s", video_path)
    logger.info("📝 Descrição final: %s", description)
    time.sleep(1.0)

    # ===== LÓGICA DE UPLOAD E RETENTATIVA APRIMORADA =====
    success = False
    for attempt in range(1, max_upload_attempts + 1):
        logger.info(">>> Iniciando tentativa de upload %d/%d...", attempt, max_upload_attempts)
        try:
            # A função upload_video agora deve retornar True em sucesso
            # ou lançar uma exceção em caso de erro.
            uploaded = upload_video(
                filename=video_path,
                description=description,
                cookies=COOKIES_PATH,
                schedule=False,
                headless=tt_headless,
                lang=lang,
                use_vpn=use_vpn,
            )

            if uploaded:
                logger.info("✅ SUCESSO! O TikTok confirmou o upload na tentativa %d.", attempt)
                success = True
                break  # Sai do loop de tentativas pois o upload foi bem-sucedido
            else:
                # Este caso pode ocorrer se a função for modificada para retornar False
                # em vez de lançar uma exceção para falhas "leves".
                logger.warning("⚠️ A função de upload retornou uma falha não esperada na tentativa %d. Tentando novamente...", attempt)

        except FailedToUpload as e:
            logger.error("❌ Falha controlada no upload (tentativa %d/%d): %s", attempt, max_upload_attempts, e)
        except (WebDriverException, SocketError) as e:
            logger.error("❌ Erro de WebDriver/Rede na tentativa %d/%d: %s", attempt, max_upload_attempts, e)
        except Exception as e:
            logger.critical("❌ Erro inesperado e grave na tentativa %d/%d: %s", attempt, max_upload_attempts, e, exc_info=True)

        # Se não for a última tentativa, aguarda um pouco antes de tentar de novo
        if attempt < max_upload_attempts:
            wait_time = attempt * 5  # Espera um pouco mais a cada tentativa
            logger.info("...aguardando %d segundos antes da próxima tentativa.", wait_time)
            time.sleep(wait_time)

    # ===== VERIFICAÇÃO FINAL E LIMPEZA =====
    if success:
        logger.info("🎉 Processo finalizado com sucesso. Limpando arquivos de vídeo.")
        _cleanup_prompts_and_video(video_path)
    else:
        logger.error("❌❌❌ O UPLOAD FALHOU APÓS %d TENTATIVAS. O vídeo NÃO será removido.", max_upload_attempts)
        # Opcional: mover o vídeo para uma pasta de "falhas" em vez de deixá-lo
        # failed_dir = "videos/failed"
        # os.makedirs(failed_dir, exist_ok=True)
        # shutil.move(video_path, os.path.join(failed_dir, os.path.basename(video_path)))

    return success
