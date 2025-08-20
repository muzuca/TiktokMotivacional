# utils/tiktok.py
import os
import re
import json
import time  # Para delays
from datetime import datetime, timedelta
from typing import Optional

from .tiktok_uploader.upload import upload_video  # Import local da pasta tiktok_uploader
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from socket import error as SocketError  # Para ConnectionResetError

# Para confirmação pós-upload
try:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from .tiktok_uploader.browsers import get_browser
except Exception:
    get_browser = None

import logging

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

# Controla se removemos marcações markdown da legenda
_STRIP_FLAG = os.getenv("STRIP_MARKDOWN_IN_DESC", "1").strip().lower()
STRIP_MARKDOWN_IN_DESC = _STRIP_FLAG not in ("0", "false", "no", "off")


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


_MD_PATTERNS = (
    (re.compile(r"\*\*(.+?)\*\*", re.DOTALL), r"\1"),   # **bold**
    (re.compile(r"\*(.+?)\*",     re.DOTALL), r"\1"),   # *italic*
    (re.compile(r"__(.+?)__",     re.DOTALL), r"\1"),   # __underline__
    (re.compile(r"_(.+?)_",       re.DOTALL), r"\1"),   # _italic_
    (re.compile(r"~~(.+?)~~",     re.DOTALL), r"\1"),   # ~~strike~~
    (re.compile(r"`([^`]+)`",     re.DOTALL), r"\1"),   # `code`
)

def _strip_markdown(texto: str) -> str:
    """Remove marcações básicas de Markdown e normaliza espaços."""
    s = (texto or "")
    for pat, rep in _MD_PATTERNS:
        s = pat.sub(rep, s)
    # Normaliza aspas “ ” ’
    s = s.replace("“", "\"").replace("”", "\"").replace("’", "'")
    # Colapsa espaços múltiplos
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()


def obter_ultimo_video(pasta=PASTA_VIDEOS):
    """
    Encontra o vídeo mais recente na pasta de vídeos (baseado na data de modificação).
    """
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


# =========================
# Confirmação pós-upload
# =========================
def _lang_tag(idioma_norm: str) -> str:
    return "pt-BR" if idioma_norm == "pt-br" else ("ar-EG" if idioma_norm == "ar" else "en-US")

def _region_from_lang(idioma_norm: str) -> str:
    return "EG" if idioma_norm == "ar" else ("US" if idioma_norm == "en" else "BR")

def _cookie_file_for_lang(idioma_norm: str) -> str:
    # Mantém compat com suas envs
    mapping = {
        "en":   os.getenv("COOKIES_US_FILENAME", "cookies_us.txt"),
        "pt-br":os.getenv("COOKIES_BR_FILENAME", "cookies_br.txt"),
        "ar":   os.getenv("COOKIES_EG_FILENAME", "cookies_eg.txt"),
    }
    return mapping.get(idioma_norm, os.getenv("COOKIES_US_FILENAME", "cookies_us.txt"))

def _attach_cookies(driver, cookies_path: str, base_domain: str = "https://www.tiktok.com"):
    """Aceita cookies em JSON (array/obj) ou Netscape (tab-separated)."""
    if not cookies_path or not os.path.exists(cookies_path):
        raise FileNotFoundError(f"Arquivo de cookies não encontrado: {cookies_path}")
    driver.get(base_domain)
    time.sleep(1.2)
    raw = open(cookies_path, "r", encoding="utf-8", errors="ignore").read().strip()

    # JSON?
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "cookies" in data:
            data = data["cookies"]
        if isinstance(data, list):
            for c in data:
                cookie = {
                    "name": c.get("name"),
                    "value": c.get("value"),
                    "domain": c.get("domain", ".tiktok.com"),
                    "path": c.get("path", "/"),
                    "secure": bool(c.get("secure", True)),
                    "httpOnly": bool(c.get("httpOnly", False)),
                }
                if "expiry" in c:
                    try:
                        cookie["expiry"] = int(c["expiry"])
                    except Exception:
                        pass
                try:
                    driver.add_cookie(cookie)
                except Exception:
                    pass
            driver.get(base_domain)
            return
    except Exception:
        pass

    # Netscape
    for line in raw.splitlines():
        if not line or line.startswith("#") or line.startswith("http"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            domain, flag, path, secure, expiry, name, value = parts[:7]
            cookie = {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path or "/",
                "secure": secure.upper() == "TRUE",
            }
            try:
                cookie["expiry"] = int(expiry)
            except Exception:
                pass
            try:
                driver.add_cookie(cookie)
            except Exception:
                pass
    driver.get(base_domain)

def _page_has_snippet(driver, snippet: str) -> bool:
    """Procura snippet em qualquer nó de texto visível."""
    try:
        elems = driver.find_elements(By.XPATH, f"//*[contains(normalize-space(), \"{snippet}\")]")
        return bool(elems)
    except Exception:
        return False

def _confirm_posted_by_manage(idioma_norm: str, descricao_base: str, *, want_proxy: bool) -> bool:
    """
    Abre o Creator Center (manage) com cookies e tenta localizar um trecho da descrição.
    Retorna True se localizar; False caso contrário.
    """
    if get_browser is None:
        logger.warning("get_browser indisponível; não foi possível confirmar a postagem.")
        return False

    region = _region_from_lang(idioma_norm)
    tag = _lang_tag(idioma_norm)
    cookies_file = _cookie_file_for_lang(idioma_norm)

    # Trecho curto para busca (evita variação de hashtags)
    snippet = (descricao_base or "").strip()
    snippet = re.sub(r"\s+", " ", snippet)[:25]  # primeiros ~25 chars

    urls = [
        f"https://www.tiktok.com/creator-center/content/manage?lang={tag}",
        f"https://www.tiktok.com/creator-center/manage?lang={tag}",
        f"https://www.tiktok.com/creator-center/upload?lang={tag}",
        f"https://www.tiktok.com/creator-center?lang={tag}",
    ]

    def _try_once(use_proxy: bool) -> bool:
        try:
            drv = get_browser(idioma=idioma_norm, want_proxy=use_proxy, region=region, lang_tag=tag)
        except Exception as e:
            logger.warning("Falha ao abrir browser p/ confirmar (proxy=%s): %s", use_proxy, e)
            return False

        try:
            _attach_cookies(drv, cookies_file)
            for u in urls:
                try:
                    drv.get(u)
                    WebDriverWait(drv, 30).until(lambda d: "creator" in d.current_url.lower())
                    # tenta achar texto; se não achar, dá um refresh curto algumas vezes
                    end = time.time() + 35
                    while time.time() < end:
                        if _page_has_snippet(drv, snippet):
                            return True
                        time.sleep(2)
                        try:
                            drv.refresh()
                        except Exception:
                            break
                except Exception:
                    continue
            return False
        finally:
            try:
                drv.quit()
            except Exception:
                pass

    # 1ª tentativa: respeitando WANT_PROXY
    if _try_once(want_proxy):
        return True
    # fallback: tenta sem proxy só para confirmar (evita proxy lento matar a verificação)
    logger.info("Tentando confirmar sem proxy (fallback de verificação)...")
    return _try_once(False)


def postar_no_tiktok_e_renomear(
    descricao_personalizada=None,
    imagem_base=None,
    imagem_final=None,
    video_final=None,
    agendar=False,
    idioma='en'
) -> bool:
    """
    Busca o último vídeo gerado, posta no TikTok e limpa os arquivos gerados APÓS confirmação.
    Retorna True se confirmado no Creator Center; False caso contrário.
    """
    idioma_norm = _normalizar_idioma(idioma)
    logger.info("postar_no_tiktok_e_renomear: idioma_in=%s | idioma_norm=%s", idioma, idioma_norm)

    video_path = video_final if video_final else obter_ultimo_video()
    if not video_path:
        return False

    # Evita caso "0 bytes" local
    try:
        if os.path.getsize(video_path) <= 0:
            logger.error("❌ Vídeo está com 0 bytes localmente: %s", video_path)
            return False
    except Exception:
        pass

    # Seleciona cookies por idioma (env-first)
    cookies_map = {
        "en":   os.getenv("COOKIES_US_FILENAME", "cookies_us.txt"),
        "pt-br":os.getenv("COOKIES_BR_FILENAME", "cookies_br.txt"),
        "ar":   os.getenv("COOKIES_EG_FILENAME", "cookies_eg.txt"),
    }
    COOKIES_PATH = cookies_map.get(idioma_norm, os.getenv("COOKIES_US_FILENAME", "cookies_us.txt"))
    logger.info("🍪 Cookies utilizados: %s", COOKIES_PATH)

    if not os.path.exists(COOKIES_PATH):
        logger.error(f"❌ Arquivo de cookies não encontrado: {COOKIES_PATH}")
        return False

    try:
        # Hashtags conforme idioma (env-first)
        hashtags_en = os.getenv("HASHTAGS_EN", " #Motivation #Inspiration #TikTokMotivational")
        hashtags_pt = os.getenv("HASHTAGS_PT_BR", " #Motivacao #Inspiracao #TikTokMotivacional")
        hashtags_ar = os.getenv("HASHTAGS_AR_EG", " #تاروت #قراءة_تاروت #ابراج #طاقة #توقعات")

        if idioma_norm == "pt-br":
            hashtags = hashtags_pt
        elif idioma_norm == "ar":
            hashtags = hashtags_ar
        else:
            hashtags = hashtags_en

        # Monta a descrição e limpa markdown, se habilitado
        if descricao_personalizada:
            base_desc = descricao_personalizada
        else:
            if idioma_norm == "pt-br":
                base_desc = "Conteúdo motivacional do dia!"
            elif idioma_norm == "ar":
                base_desc = "رسالة اليوم ✨"
            else:
                base_desc = "Motivational content of the day!"

        if STRIP_MARKDOWN_IN_DESC:
            cleaned = _strip_markdown(base_desc)
            if cleaned != base_desc:
                logger.debug("🧹 Limpando markdown da descrição: '%s' -> '%s'", base_desc, cleaned)
            base_desc = cleaned

        description = (base_desc + " " + hashtags).strip()

        schedule = None
        if agendar:
            schedule = datetime.now() + timedelta(minutes=20)
            logger.info("📅 Agendando post para: %s", schedule.strftime("%H:%M:%S"))

        logger.info("🚀 Postando vídeo no TikTok: %s", video_path)
        logger.info("📝 Descrição final: %s", description)
        time.sleep(1.5)  # pequeno delay

        # ========== EXECUTA UPLOAD ==========
        ok_upload = False
        try:
            upload_video(
                filename=video_path,
                description=description,
                cookies=COOKIES_PATH,
                comment=True,
                stitch=True,
                duet=True,
                headless=False,         # troque para True se quiser rodar invisível
                schedule=schedule,
                idioma=idioma_norm      # ex.: 'ar' -> ar-EG na lib
            )
            ok_upload = True  # a lib não retorna status; consideramos que concluiu fluxo sem exceção
        except (NoSuchElementException, TimeoutException) as e:
            logger.warning("⚠️ Erro intermediário durante upload: %s", e)
        except SocketError as e:
            if getattr(e, "errno", None) == 10054:
                logger.warning("⚠️ Conexão resetada (10054). Post possivelmente concluído. Vamos confirmar.")
            else:
                logger.error("❌ Erro de socket inesperado: %s", e)
        except WebDriverException as e:
            logger.error("❌ Erro no WebDriver durante upload: %s", e)
        except Exception as e:
            logger.error("❌ Erro geral ao postar: %s", e)

        # ========== CONFIRMAÇÃO NO CREATOR CENTER ==========
        want_proxy = os.getenv("WANT_PROXY", "1").strip().lower() in {"1","true","yes"}
        ok_confirm = _confirm_posted_by_manage(idioma_norm, base_desc, want_proxy=want_proxy)

        ok = bool(ok_upload and ok_confirm)
        if ok:
            logger.info("✅ Publicação confirmada no Creator Center.")
        else:
            logger.error("❌ Não foi possível confirmar a publicação (upload_ok=%s, confirm_ok=%s).", ok_upload, ok_confirm)

        # ========== LIMPEZA CONDICIONADA ==========
        if ok:
            if imagem_base and os.path.exists(imagem_base):
                os.remove(imagem_base)
                logger.info("🗑️ Imagem base removida: %s", imagem_base)
            if imagem_final and os.path.exists(imagem_final):
                os.remove(imagem_final)
                logger.info("🗑️ Imagem final removida: %s", imagem_final)
            if video_final and os.path.exists(video_final):
                os.remove(video_final)
                logger.info("🗑️ Vídeo original removido: %s", video_final)

            # Remove o áudio mais recente (se existir)
            try:
                audio_files = [f for f in os.listdir(PASTA_AUDIOS) if f.endswith(".mp3")]
                if audio_files:
                    audio_to_remove = max(
                        (os.path.join(PASTA_AUDIOS, f) for f in audio_files),
                        key=os.path.getmtime
                    )
                    os.remove(audio_to_remove)
                    logger.info("🗑️ Áudio removido: %s", audio_to_remove)
            except Exception:
                pass
        else:
            logger.warning("⚠️ Upload não confirmado — preservando arquivos locais para retry.")

        return ok

    finally:
        logger.info("⏳ Aguardando 5 segundos antes de finalizar...")
        time.sleep(5)
