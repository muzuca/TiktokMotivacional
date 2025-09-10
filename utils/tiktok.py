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
from .tiktok_uploader.upload import upload_video

# Países / cookies / diretórios centralizados
from .countries import (
    normalize_lang, cookies_path_for, tiktok_headless_default,
)

try:
    from cache_store import cache
except Exception:  # pragma: no cover
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
            logger.info("🗑️ Pasta removida: %s", path)
        elif os.path.isfile(path):
            os.remove(path)
            logger.info("🗑️ Arquivo removido: %s", path)
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
        logger.info("📼 Último vídeo encontrado: %s", ultimo_video)
        return ultimo_video
    except Exception as e:
        logger.error("⚠️ Erro ao buscar último vídeo: %s", str(e))
        return None

def postar_no_tiktok_e_renomear(
    descricao_personalizada: Optional[str] = None,
    imagem_base: Optional[str] = None,
    imagem_final: Optional[str] = None,
    video_final: Optional[str] = None,
    agendar: bool = False,
    idioma: str = "en",
    max_upload_attempts: int = 3,  # NOVO: padrão 3 tentativas de upload
) -> bool:
    """
    Posta o último vídeo gerado (ou 'video_final' se informado) e,
    após sucesso, faz a faxina (imagens/, audios/, cache temp, videos/prompts/ e o mp4 postado).
    Agora, se o upload/post falhar, NÃO apaga vídeo nem recursos — só faxina no sucesso!
    """
    lang = normalize_lang(idioma)
    logger.info("postar_no_tiktok_e_renomear: idioma_in=%s | idioma_norm=%s", idioma, lang)

    video_path = video_final if video_final else obter_ultimo_video()
    if not video_path:
        return False
    try:
        if os.path.getsize(video_path) <= 0:
            logger.error("⚠️ Vídeo está com 0 bytes: %s", video_path)
            return False
    except Exception:
        pass

    COOKIES_PATH = cookies_path_for(lang)
    logger.info("🍪 Cookies utilizados: %s", COOKIES_PATH)
    if not os.path.exists(COOKIES_PATH):
        logger.error("⚠️ Arquivo de cookies não encontrado: %s", COOKIES_PATH)
        return False

    # Descrição base por idioma
    if descricao_personalizada:
        base_desc = descricao_personalizada
    else:
        if lang == "pt-br":
            base_desc = "Conteúdo motivacional do dia!"
        elif lang == "ar":
            base_desc = "سيطر على تفكيرك، تسيطر على حياتك."
        elif lang == "ru":
            base_desc = "Контент для мотивации на день!"
        else:
            base_desc = "Motivational content of the day!"

    if STRIP_MARKDOWN_IN_DESC:
        base_desc = _strip_markdown(base_desc)
    description = _dedupe_hashtags_in_desc(base_desc)

    if cache:
        try:
            cache.add("used_phrases", description, lang=lang)
        except Exception:
            pass

    schedule = None
    if agendar:
        schedule = datetime.now() + timedelta(minutes=20)
        logger.info("⏰ Agendando post para: %s", schedule.strftime("%H:%M:%S"))

    tt_headless = tiktok_headless_default()
    logger.info("🌐 TikTok headless: %s", "ON" if tt_headless else "OFF")

    logger.info("🚀 Postando vídeo no TikTok: %s", video_path)
    logger.info("📝 Descrição final: %s", description)
    time.sleep(1.0)

    # ====== NOVO FLUXO DE RETRY PROTEÇÃO ==========
    upload_ok = False
    last_upload_error = None

    for tentativa in range(1, max_upload_attempts + 1):
        try:
            upload_video(
                filename=video_path,
                description=description,
                cookies=COOKIES_PATH,
                comment=True,
                stitch=True,
                duet=True,
                headless=tt_headless,
                schedule=schedule,
                idioma=lang,
            )
            upload_ok = True
            logger.info(f"✅ Upload/postagem bem-sucedida na tentativa {tentativa}.")
            break
        except (NoSuchElementException, TimeoutException) as e:
            logger.warning(f"⚠️ Erro Selenium durante upload (tentativa {tentativa}): {e}")
            last_upload_error = e
        except SocketError as e:
            if getattr(e, "errno", None) == 10054:
                logger.warning("⚠️ Conexão resetada (10054). Pode ter concluído, mas não confirmamos. Mantendo arquivos.")
            else:
                logger.error(f"⚠️ Erro socket inesperado durante upload (tentativa {tentativa}): {e}")
            last_upload_error = e
        except WebDriverException as e:
            logger.error(f"⚠️ Erro WebDriver durante upload (tentativa {tentativa}): {e}")
            last_upload_error = e
        except Exception as e:
            logger.error(f"⚠️ Erro geral ao postar (tentativa {tentativa}): {e}")
            last_upload_error = e

        if tentativa < max_upload_attempts:
            logger.info(f"↩️ Tentando novamente upload/post em 30s... (tentativa {tentativa+1}/{max_upload_attempts})")
            time.sleep(30)
        else:
            logger.error(
                f"🔥 Todas as tentativas ({max_upload_attempts}) de upload falharam para: {video_path}.\n"
                f"Último erro: {last_upload_error}\n"
                "O arquivo NÃO será apagado para não perder créditos do Flow! Resolva manualmente antes de tentar gerar novamente."
            )

    if upload_ok:
        # Pós-POST: limpeza (só no sucesso)
        if imagem_base and os.path.exists(imagem_base):
            _safe_remove(imagem_base)
        if imagem_final and os.path.exists(imagem_final):
            _safe_remove(imagem_final)
        if video_final and os.path.exists(video_final):
            _safe_remove(video_final)

        _cleanup_mid_artifacts()
        _cleanup_prompts_and_video(video_path)
        return True
    else:
        # Não apaga nada, loga o fracasso.
        return False
