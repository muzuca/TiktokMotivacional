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
    descricao_personalizada: str,
    video_final: str,
    idioma: str,
    use_vpn: bool,
    headless: bool,  # <- Este é o parâmetro que já estamos recebendo do main.py
    **kwargs
) -> bool:
    """Posta o vídeo no TikTok e renomeia o arquivo com base no sucesso."""
    lang = normalize_lang(idioma)
    logger.info(
        "postar_no_tiktok_e_renomear: idioma_in=%s | idioma_norm=%s | use_vpn=%s",
        idioma, lang, use_vpn
    )

    video_path = video_final
    # ... (o resto da sua lógica de verificação de arquivo, cookies, etc. permanece igual)

    COOKIES_PATH = cookies_path_for(lang)
    logger.info("🍪 Cookies utilizados: %s", COOKIES_PATH)
    # ...
    
    description = descricao_personalizada # Simplificado para o exemplo

    # ##############################################################
    # CORREÇÃO APLICADA AQUI
    # ##############################################################
    # A variável 'tt_headless' agora usa diretamente o parâmetro 'headless'
    # que foi decidido no menu do main.py. Ela não tenta mais ler do os.getenv.
    tt_headless = headless
    logger.info("🤖 TikTok headless: %s", "ON" if tt_headless else "OFF")
    # ##############################################################

    logger.info("🚀 Postando vídeo no TikTok: %s", video_path)
    logger.info("📝 Descrição final: %s", description)
    time.sleep(1.0)

    # ===== LÓGICA DE UPLOAD E RETENTATIVA APRIMORADA =====
    success = False
    max_upload_attempts = 3 # Definido um valor padrão
    for attempt in range(1, max_upload_attempts + 1):
        logger.info(">>> Iniciando tentativa de upload %d/%d...", attempt, max_upload_attempts)
        try:
            uploaded = upload_video(
                filename=video_path,
                description=description,
                cookies=COOKIES_PATH,
                headless=tt_headless, # Passa a variável correta
                idioma=lang,
                use_vpn=use_vpn,
            )

            if uploaded:
                logger.info("✅ SUCESSO! O TikTok confirmou o upload na tentativa %d.", attempt)
                success = True
                break
            else:
                logger.warning("⚠️ A função de upload retornou uma falha não esperada na tentativa %d.", attempt)

        except Exception as e:
            logger.error("❌ Erro na tentativa de upload %d/%d: %s", attempt, max_upload_attempts, e)

        if attempt < max_upload_attempts:
            wait_time = attempt * 5
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
