# utils/tiktok_uploader/upload.py
"""
Módulo `tiktok_uploader` para fazer upload de vídeos no TikTok.

Atualizações desta versão:
- Espera inteligente de carregamento: injeção de contador de fetch/XHR e espera por
  "network idle" antes de prosseguir; hidratação de SPA sem sleeps fixos.
- Grace dinâmico antes do Post: quando a rede está quieta e sem modais bloqueando,
  o fluxo publica de imediato (ou com um grace mínimo configurável).
- Captura robusta do perfil autenticado logo após abrir o upload, com retry que volta
  à rota `/tiktokstudio/upload` caso a página redirecione para o hub `/tiktokstudio`.
- Suporte a timezone/locale via CDP (ex.: `Africa/Cairo` + `ar-EG`) para agendamentos.
- Navegação resiliente para rede lenta/proxy: warm-up em `/explore`, múltiplas variantes
  da URL de upload, antiredirecionamento para o hub e fallback para detecção de iframe/DOM.
- Headless estável: flags SwiftShader/WebGL para evitar erros de GPU.
- **Descompartimentação de país/idioma/região** via funções e mapeamentos,
  adicionando **Rússia (RU)**: `ru-RU`, timezone `Europe/Moscow`, e `PROXY_RU_*`.
- **Integração com VPN Manager**: orquestra a conexão de VPN antes de qualquer
  navegação para o TikTok, abortando a operação em caso de falha.
"""

from __future__ import annotations

import logging
import os
from os.path import abspath, exists
import time
import pytz
import datetime
import requests
import re
import random
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from typing import Any, Callable, Literal, Optional, List, Dict, Tuple
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

# ===== Selenium =====
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    TimeoutException,
    NoSuchElementException,
    WebDriverException,
)
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.action_chains import ActionChains

# ===== App modules =====
from .auth import AuthBackend
from . import config

# ===== VPN Manager =====
try:
    from ..vpn_manager import VpnConnectionError
except (ImportError, ValueError):
    class VpnConnectionError(Exception):
        """Fallback de erro customizado para falhas de conexão da VPN."""
        pass

# ----------------------------------------------------------------------
#  MAPAS CENTRALIZADOS (país/idioma/região/proxy/timezone)
# ----------------------------------------------------------------------

def _norm_lang(s: Optional[str]) -> str:
    s = (s or "").strip().lower()
    if s.startswith("ar"): return "ar"
    if s.startswith("pt"): return "pt"
    if s.startswith("ru"): return "ru"
    if s.startswith("id"): return "id"
    return "en"

LANG_TO_REGION = {"ar": "EG", "en": "US", "pt": "BR", "ru": "RU", "id": "ID"}
LANG_TO_TAG = {"ar": "ar-EG", "en": "en-US", "pt": "pt-BR", "ru": "ru-RU", "id": "id-ID"}
REGION_TO_TIME = {
    "EG": {"tz": "Africa/Cairo", "locale": "ar-EG"},
    "US": {"tz": "America/New_York", "locale": "en-US"},
    "BR": {"tz": "America/Sao_Paulo", "locale": "pt-BR"},
    "RU": {"tz": "Europe/Moscow", "locale": "ru-RU"},
    "ID": {"tz": "Asia/Jakarta", "locale": "id-ID"},
}

def _want_proxy_default(idioma: Optional[str]) -> bool:
    return _norm_lang(idioma) in ("en", "ar", "ru", "id")

def _region_default(idioma: Optional[str]) -> Optional[str]:
    return LANG_TO_REGION.get(_norm_lang(idioma), None)

def _lang_tag_default(idioma: Optional[str]) -> str:
    return LANG_TO_TAG.get(_norm_lang(idioma), "en-US")

def _env_first(*keys: str, default: str = "") -> str:
    for k in keys:
        v = os.getenv(k)
        if v: return v
    return default

def _resolve_proxy_env(region: Optional[str]):
    reg = (region or "").upper()
    if reg == "US":
        host = _env_first("PROXY_US_HOST", "PROXY_HOST_US", "PROXY_HOST")
        port = _env_first("PROXY_US_PORT", "PROXY_PORT_US", "PROXY_PORT")
        user = _env_first("PROXY_US_USER", "PROXY_USER_US", "PROXY_USER") or None
        pw = _env_first("PROXY_US_PASS", "PROXY_PASS_US", "PROXY_PASS") or None
        return host, port, user, pw
    if reg == "EG":
        host = _env_first("PROXY_EG_HOST", "PROXY_HOST_EG", "PROXY_HOST")
        port = _env_first("PROXY_EG_PORT", "PROXY_PORT_EG", "PROXY_PORT")
        user = _env_first("PROXY_EG_USER", "PROXY_USER_EG", "PROXY_USER") or None
        pw = _env_first("PROXY_EG_PASS", "PROXY_PASS_EG", "PROXY_PASS") or None
        return host, port, user, pw
    if reg == "RU":
        host = _env_first("PROXY_RU_HOST", "PROXY_HOST_RU", "PROXY_HOST")
        port = _env_first("PROXY_RU_PORT", "PROXY_PORT_RU", "PROXY_PORT")
        user = _env_first("PROXY_RU_USER", "PROXY_USER_RU", "PROXY_USER") or None
        pw = _env_first("PROXY_RU_PASS", "PROXY_PASS_RU", "PROXY_PASS") or None
        return host, port, user, pw
    if reg == "ID":
        host = _env_first("PROXY_ID_HOST", "PROXY_HOST_ID", "PROXY_HOST")
        port = _env_first("PROXY_ID_PORT", "PROXY_PORT_ID", "PROXY_PORT")
        user = _env_first("PROXY_ID_USER", "PROXY_USER_ID", "PROXY_USER") or None
        pw = _env_first("PROXY_ID_PASS", "PROXY_PASS_ID", "PROXY_PASS") or None
        return host, port, user, pw
    host = _env_first("PROXY_BR_HOST", "PROXY_HOST_BR", "PROXY_HOST")
    port = _env_first("PROXY_BR_PORT", "PROXY_PORT_BR", "PROXY_PORT")
    user = _env_first("PROXY_BR_USER", "PROXY_USER_BR", "PROXY_USER") or None
    pw = _env_first("PROXY_BR_PASS", "PROXY_PASS_BR", "PROXY_PASS") or None
    return host, port, user, pw

def _accept_header_from_tag(tag: Optional[str]) -> str:
    tag = (tag or "").strip() or "en-US"
    tl = tag.lower()
    if tl.startswith("pt"): return "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"
    if tl.startswith("ar"): return "ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7"
    if tl.startswith("ru"): return "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
    if tl.startswith("id"): return "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7"
    return "en-US,en;q=0.9"

try:
    from .browsers import get_browser
except ImportError:
    from dotenv import load_dotenv
    from seleniumwire import webdriver as wire_webdriver
    from selenium import webdriver as std_webdriver
    from selenium.webdriver.chrome.service import Service as ChromeService
    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.webdriver.firefox.options import Options as FirefoxOptions
    from selenium.webdriver.firefox.service import Service as FirefoxService
    from webdriver_manager.firefox import GeckoDriverManager
    from selenium.webdriver.edge.options import Options as EdgeOptions
    from selenium.webdriver.edge.service import Service as EdgeService
    from webdriver_manager.microsoft import EdgeChromiumDriverManager
    load_dotenv()
    def _mk_sw_opts(use_proxy: bool, region: Optional[str]):
        opts = {'request_storage': 'none', 'verify_ssl': False, 'scopes': [r".*\.tiktok\.com.*", r".*\.tiktokcdn\.com.*", r".*\.ttwstatic\.com.*"], 'port': 0, 'addr': '127.0.0.1', 'auto_config': True}
        if not use_proxy: return opts
        host, port, user, pw = _resolve_proxy_env(region)
        if host and port:
            proxy_uri = f"http://{user}:{pw}@{host}:{port}" if user and pw else f"http://{host}:{port}"
            opts['proxy'] = {'http': proxy_uri, 'https': proxy_uri, 'no_proxy': 'localhost,127.0.0.1'}
            logging.info("Selenium-Wire proxy configurado (%s): %s", (region or "DEFAULT"), proxy_uri)
        else:
            logging.warning("want_proxy=True mas variáveis de proxy ausentes (%s). Seguiremos sem upstream.", region or "DEFAULT")
        return opts
    def get_browser(name="chrome", options=None, proxy=None, idioma: str = "auto", headless: bool = False, *, want_proxy: Optional[bool] = None, region: Optional[str] = None, lang_tag: Optional[str] = None, **kwargs):
        if want_proxy is None: want_proxy = _want_proxy_default(idioma)
        if region is None: region = _region_default(idioma)
        if lang_tag is None: lang_tag = _lang_tag_default(idioma)
        logging.info("get_browser (fallback): idioma=%s | want_proxy=%s | region=%s | lang_tag=%s", idioma, want_proxy, region or "-", lang_tag)
        if name == "chrome":
            if options is None: options = ChromeOptions()
            options.add_argument(f"--lang={lang_tag}")
            options.add_argument("--disable-logging")
            options.add_argument("--log-level=3")
            options.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
            options.add_experimental_option("useAutomationExtension", False)
            options.add_argument("--disable-extensions")
            options.add_argument("--disable-popup-blocking")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--remote-debugging-pipe")
            if headless:
                options.add_argument("--headless=new")
                options.add_argument("--ignore-certificate-errors")
                options.add_argument("--disable-gpu")
                options.add_argument("--use-gl=swiftshader")
                options.add_argument("--enable-unsafe-swiftshader")
                options.add_argument("--ignore-gpu-blocklist")
            sw_opts = _mk_sw_opts(bool(want_proxy), region)
            driver = wire_webdriver.Chrome(service=ChromeService(ChromeDriverManager().install(), port=0), options=options, seleniumwire_options=sw_opts)
            try:
                driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {"headers": {"Accept-Language": _accept_header_from_tag(lang_tag), "Upgrade-Insecure-Requests": "1"}})
                driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": "try { Object.defineProperty(navigator,'webdriver',{get:()=>undefined}); } catch(e){}"})
            except Exception as e:
                logging.error(f"Erro ao configurar CDP: {e}")
            return driver
        elif name == "firefox":
            if options is None: options = FirefoxOptions()
            if headless: options.add_argument("-headless")
            try: options.set_preference("intl.accept_languages", _accept_header_from_tag(lang_tag))
            except Exception: pass
            sw_opts = _mk_sw_opts(bool(want_proxy), region)
            return wire_webdriver.Firefox(service=FirefoxService(GeckoDriverManager().install(), port=0), options=options, seleniumwire_options=sw_opts)
        elif name == "edge":
            if options is None: options = EdgeOptions()
            options.add_argument(f"--lang={lang_tag}")
            options.add_argument("--remote-debugging-pipe")
            if headless: options.add_argument("--headless=new")
            sw_opts = _mk_sw_opts(bool(want_proxy), region)
            return wire_webdriver.Edge(service=EdgeService(EdgeChromiumDriverManager().install(), port=0), options=options, seleniumwire_options=sw_opts)
        else:
            raise ValueError(f"Navegador {name} não suportado")

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)
logging.getLogger("webdriver_manager").setLevel(logging.INFO)

from .utils import bold
from .types import VideoDict, ProxyDict, Cookie

session = requests.Session()
retries = Retry(total=0)
session.mount("http://", HTTPAdapter(max_retries=retries))
session.mount("https://", HTTPAdapter(max_retries=retries))

def _int_env(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        return int(v) if v is not None and str(v).strip() != "" else default
    except Exception: return default
def _float_env(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v is not None and str(v).strip() != "" else default
    except Exception: return default
def _bool_env(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "": return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")

IMPLICIT_WAIT = _int_env("IMPLICIT_WAIT_SEC", 5)
EXPLICIT_WAIT = _int_env("EXPLICIT_WAIT_SEC", 30)
UPLOADING_WAIT = _int_env("UPLOADING_WAIT_SEC", 240)
ADD_HASHTAG_WAIT = _float_env("ADD_HASHTAG_WAIT_SEC", 0.5)
POST_CLICK_WAIT = _int_env("POST_CLICK_WAIT_SEC", 20)
NAV_MAX_RETRIES = _int_env("UPLOAD_NAV_RETRIES", 3)
IFRAME_MAX_RETRY = _int_env("UPLOAD_IFRAME_RETRIES", 2)
NAV_RETRY_BACKOFF = _float_env("UPLOAD_RETRY_BACKOFF_SEC", 3.0)
POST_MIN_GRACE_SEC = _int_env("POST_MIN_GRACE_SEC", 30)
POST_AFTER_ENABLED_EXTRA_SEC = _int_env("POST_AFTER_ENABLED_EXTRA_SEC", 2)
OVERLAYS_SETTLE_SEC = _int_env("OVERLAYS_SETTLE_SEC", 3)
PUBLICATIONS_WAIT_SEC = _int_env("PUBLICATIONS_WAIT_SEC", 120)
VERIFY_POST_IN_PUBLICATIONS = _bool_env("VERIFY_POST_IN_PUBLICATIONS", True)
DEFAULT_BEGIN_WORDS = _int_env("PUBLICATIONS_DESC_BEGIN_WORDS", 2)
JITTER_START_MIN_SEC = _int_env("JITTER_START_MIN_SEC", 0)
JITTER_START_MAX_SEC = _int_env("JITTER_START_MAX_SEC", 0)
SLOW_NET_EXTRA_WAIT_SEC = _int_env("SLOW_NET_EXTRA_WAIT_SEC", 60)
UPLOAD_PAGE_SETTLE_SEC = _int_env("UPLOAD_PAGE_SETTLE_SEC", 15)
HEADER_MAX_WAIT_SEC = _int_env("HEADER_MAX_WAIT_SEC", 90)
WARMUP_EXPLORE_WAIT_SEC = _int_env("WARMUP_EXPLORE_WAIT_SEC", 45)
PROFILE_RETRIES = _int_env("PROFILE_RETRIES", 3)
SMART_WAIT_ENABLE = _bool_env("SMART_WAIT_ENABLE", True)
NET_IDLE_QUIET_MS = _int_env("NET_IDLE_QUIET_MS", 1200)
SPA_READY_MAX_SEC = _int_env("SPA_READY_MAX_SEC", 60)
DYNAMIC_POST_GRACE = _bool_env("DYNAMIC_POST_GRACE", True)
POST_GRACE_MIN_SEC = _int_env("POST_GRACE_MIN_SEC", 0)
JITTER_MIN_MINUTES = _int_env("JITTER_MIN_MINUTES", -15)
JITTER_MAX_MINUTES = _int_env("JITTER_MAX_MINUTES", 18)

def human_interval_seconds(base_hours: float, jitter_min_minutes: Optional[int] = None, jitter_max_minutes: Optional[int] = None) -> int:
    jmin = JITTER_MIN_MINUTES if jitter_min_minutes is None else jitter_min_minutes
    jmax = JITTER_MAX_MINUTES if jitter_max_minutes is None else jitter_max_minutes
    offs_minutes = random.randint(jmin, jmax)
    base_sec = int(base_hours * 3600)
    return max(60, base_sec + offs_minutes * 60)

def sleep_jitter_before_post() -> int:
    lo, hi = sorted((max(0, JITTER_START_MIN_SEC), max(0, JITTER_START_MAX_SEC)))
    if hi <= 0: return 0
    sl = random.randint(lo, hi)
    if sl > 0:
        logger.info("⏳ Jitter antes da postagem: aguardando %ds para desalinhamento humano...", sl)
        time.sleep(sl)
    return sl

def _idioma_norm(idioma: Optional[str]) -> str:
    return _norm_lang(idioma)

def _use_proxy_from_idioma(idioma: Optional[str]) -> bool:
    return _want_proxy_default(idioma)

def _region_from_idioma(idioma: Optional[str]) -> Optional[str]:
    return _region_default(idioma)

def _lang_tag_from_idioma(idioma: Optional[str]) -> str:
    return _lang_tag_default(idioma)

def _is_seleniumwire_driver(driver) -> bool:
    try:
        return driver.__class__.__module__.startswith("seleniumwire")
    except Exception:
        return False

PUB_ROW_CONTAINER_XPATH = "//div[@data-tt='components_PostInfoCell_Container']"
PUB_ROW_LINK_REL_XPATH = ".//a[@data-tt='components_PostInfoCell_a']"
PUBLICACOES_SEARCH_XPATHES = [
    "//input[contains(translate(@placeholder,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'descri')]",
    "//div[contains(@class,'Search') or contains(@data-tt,'SearchBar')]//input",
    "(//div[.//text()[contains(.,'Views') or contains(.,'Visualiza') or contains(.,'Commentaires') or contains(.,'Kommentare') or contains(.,'تعليقات')]]//input)[1]",
]

def _find_publications_search_input(driver) -> Optional[Any]:
    for xp in PUBLICACOES_SEARCH_XPATHES:
        try:
            el = driver.find_element(By.XPATH, xp)
            if el and el.is_enabled(): return el
        except Exception: pass
    return None

def _wait_publications_page(driver: WebDriver, timeout: int = 90) -> None:
    def _ready(d: WebDriver):
        try: d.switch_to.default_content()
        except Exception: pass
        try:
            if d.find_elements(By.XPATH, PUB_ROW_CONTAINER_XPATH): return True
        except Exception: pass
        try:
            return _find_publications_search_input(d) is not None
        except Exception: return False
    WebDriverWait(driver, timeout).until(_ready)

def _snippet_from_beginning(description: str, begin_words: int = DEFAULT_BEGIN_WORDS, max_chars: int = 60) -> str:
    if not description: return ""
    tokens = [t for t in re.split(r"\s+", description.strip()) if t]
    keep: List[str] = []
    for t in tokens:
        if t.startswith("#") or t.startswith("@"):
            if not keep: continue
        keep.append(t)
        if len(keep) >= max(1, begin_words): break
    snippet = " ".join(keep).strip().replace('"', " ").replace("'", " ")
    snippet = re.sub(r"\s+", " ", snippet)
    if len(snippet) > max_chars:
        snippet = snippet[:max_chars].rstrip()
    return snippet

def _maybe_filter_publications_by_query(driver: WebDriver, query: str) -> None:
    try:
        search = _find_publications_search_input(driver)
        if not search: return
        search.click()
        try: search.clear()
        except Exception: pass
        search.send_keys(query)
        search.send_keys(Keys.RETURN)
        time.sleep(0.8)
    except Exception: pass

def _confirm_post_in_publications(driver: WebDriver, description: str, timeout: int, *, begin_words: int = DEFAULT_BEGIN_WORDS) -> bool:
    try: driver.switch_to.default_content()
    except Exception: pass
    _wait_publications_page(driver, timeout=min(60, timeout))
    snippet = _snippet_from_beginning(description or "", begin_words=begin_words).strip()
    if snippet:
        _maybe_filter_publications_by_query(driver, snippet)
    deadline = time.time() + max(10, timeout)
    snippet_lower = snippet.lower()
    while time.time() < deadline:
        try:
            containers = driver.find_elements(By.XPATH, PUB_ROW_CONTAINER_XPATH)
            for c in containers:
                try:
                    text = (c.text or "")
                    link_titles = [a.get_attribute("title") or "" for a in c.find_elements(By.XPATH, PUB_ROW_LINK_REL_XPATH) if a.get_attribute("title")]
                    haystack = (text + " " + " ".join(link_titles)).lower()
                    if snippet_lower and snippet_lower in haystack:
                        logger.info("✅ Post localizado na lista por início da descrição: %r", snippet)
                        return True
                except Exception: pass
            if not snippet and containers:
                logger.info("✅ Publicações carregadas; descrição vazia, assumindo sucesso.")
                return True
        except Exception: pass
        time.sleep(1.0)
    logger.warning("Não consegui confirmar o post na lista (início=%r) em %ds.", snippet, timeout)
    return False

PROFILE_AVATAR_BTN_XPATHES = [
    "//button[@data-tt='Header_NewHeader_Clickable']",
    "//div[@data-tt='Header_NewHeader_FlexRow_4']//button[@data-tt='Header_NewHeader_Clickable']",
    "//img[contains(@data-tt,'components_Avatar_AvatarImg')]/ancestor::button[1]",
    "//button[@aria-haspopup='dialog' and contains(@class,'e1rf0ws82')]",
    "//button[contains(@class,'e1rf0ws82')]",
]
PROFILE_MENU_LINK_XPATHES = [
    "//a[@data-tt='Header_NewHeader_TUXMenuItem' and contains(@href,'tiktok.com/@')]",
    "//a[contains(@class,'TUXMenuItem') and contains(@href,'tiktok.com/@')]",
    "//a[contains(@href,'/@@') or contains(@href,'tiktok.com/@')]",
]

def _await_document_ready(driver: WebDriver, timeout: int = 60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if driver.execute_script("return document.readyState") == "complete": return
        except Exception: pass
        time.sleep(0.2)

def _wait_header_ready(driver: WebDriver, timeout: int = 45) -> bool:
    driver.switch_to.default_content()
    deadline = time.time() + timeout
    while time.time() < deadline:
        for xp in PROFILE_AVATAR_BTN_XPATHES:
            try:
                el = WebDriverWait(driver, 2).until(EC.presence_of_element_located((By.XPATH, xp)))
                if el.is_displayed(): return True
            except Exception: pass
        for xp in PROFILE_MENU_LINK_XPATHES:
            try:
                if driver.find_elements(By.XPATH, xp): return True
            except Exception: pass
        time.sleep(0.3)
    return False

def _open_profile_menu(driver: WebDriver, timeout: int = 20) -> None:
    driver.switch_to.default_content()
    if not _wait_header_ready(driver, timeout=max(3, timeout - 2)): return
    for xp in PROFILE_AVATAR_BTN_XPATHES:
        try:
            btn = driver.find_element(By.XPATH, xp)
            driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'end'});", btn)
            try: ActionChains(driver).move_to_element(btn).pause(0.05).click(btn).perform()
            except Exception:
                try: btn.click()
                except Exception: driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('click',{bubbles:true}))", btn)
            time.sleep(0.4)
            for mxp in PROFILE_MENU_LINK_XPATHES:
                if driver.find_elements(By.XPATH, mxp): return
        except Exception: pass

def _extract_profile_from_menu(driver: WebDriver, timeout: int = 8) -> Optional[Tuple[str, str]]:
    driver.switch_to.default_content()
    for xp in PROFILE_MENU_LINK_XPATHES:
        try:
            a = WebDriverWait(driver, timeout).until(EC.presence_of_element_located((By.XPATH, xp)))
            href = (a.get_attribute("href") or "").strip()
            if not href: continue
            user = ""
            try:
                parsed = urlparse(href)
                if parsed.path and parsed.path.startswith("/@"):
                    user = parsed.path[2:].split("/")[0]
            except Exception: user = ""
            return (user, href)
        except Exception: pass
    return None

def _profile_from_page_state(driver: WebDriver) -> Optional[Tuple[str, str]]:
    try:
        js = """
        try {
            if (window.SIGI_STATE && window.SIGI_STATE.UserModule) {
                const u = window.SIGI_STATE.UserModule.user || {};
                const id = u.uniqueId || u.secUid || "";
                if (id) return [id, 'https://www.tiktok.com/@'+id];
            }
            const g = window.__UNIVERSAL_DATA__ && window.__UNIVERSAL_DATA__.__INITIAL_STATE__;
            if (g && g.user) {
                const id = g.user.uniqueId || "";
                if (id) return [id, 'https://www.tiktok.com/@'+id];
            }
        } catch (e) {}
        return null;
        """
        res = driver.execute_script(js)
        if res and isinstance(res, (list, tuple)) and len(res) == 2:
            return (str(res[0]), str(res[1]))
    except Exception: pass
    return None

def _close_any_menu(driver: WebDriver) -> None:
    try: driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
    except Exception:
        try: driver.execute_script("document.body && document.body.click && document.body.click();")
        except Exception: pass

def _log_current_profile(driver: WebDriver, timeout: int = 20, *, want_proxy: bool = False) -> Optional[Tuple[str, str]]:
    try:
        _await_document_ready(driver, timeout=max(10, timeout))
        if want_proxy:
            time.sleep(min(15, SLOW_NET_EXTRA_WAIT_SEC))
        _open_profile_menu(driver, timeout=max(8, timeout // 2))
        info = _extract_profile_from_menu(driver, timeout=max(6, timeout // 2)) or _profile_from_page_state(driver)
        if info:
            username, href = info
            handle = f"@{username}" if username else "(desconhecido)"
            logger.info("👤 Perfil autenticado (header): %s — %s", handle, href)
            return info
        else:
            logger.warning("Não consegui capturar o perfil atual no header (menu não disponível/sem href).")
            return None
    except Exception as e:
        logger.warning("Falha ao capturar o perfil atual: %s", e)
        return None
    finally:
        _close_any_menu(driver)

def _install_network_watch(driver: WebDriver) -> None:
    js = r"""(function(){if(window.__netmon_installed)return;try{window.__pendingRequests=0;const a=window.fetch;if(a){window.fetch=function(){window.__pendingRequests++;return a.apply(this,arguments).finally(function(){window.__pendingRequests=Math.max(0,(window.__pendingRequests||1)-1)})}}if(window.XMLHttpRequest&&window.XMLHttpRequest.prototype.send){const b=XMLHttpRequest.prototype.send;XMLHttpRequest.prototype.send=function(){try{this.addEventListener("loadend",function(){window.__pendingRequests=Math.max(0,(window.__pendingRequests||1)-1)},{once:!0})}catch(c){}window.__pendingRequests=(window.__pendingRequests||0)+1;return b.apply(this,arguments)}}window.__netmon_installed=!0}catch(c){}})();"""
    try: driver.execute_script(js)
    except Exception: pass

def _pending_requests(driver: WebDriver) -> int:
    try: return int(driver.execute_script("return window.__pendingRequests||0;") or 0)
    except Exception: return 0

def _wait_network_idle(driver: WebDriver, quiet_ms: int, timeout: int) -> bool:
    deadline = time.time() + max(1, timeout)
    last_change = time.time()
    last_count = _pending_requests(driver)
    while time.time() < deadline:
        cnt = _pending_requests(driver)
        if cnt != last_count:
            last_count = cnt
            last_change = time.time()
        if cnt == 0 and (time.time() - last_change) >= (quiet_ms / 1000.0):
            return True
        time.sleep(0.1)
    return False

def _wait_spa_ready(driver: WebDriver, *, lang_tag: Optional[str], slow_network: bool) -> None:
    settle_extra = SLOW_NET_EXTRA_WAIT_SEC if slow_network else 0
    _await_document_ready(driver, timeout=EXPLICIT_WAIT + settle_extra)
    _install_network_watch(driver)
    WebDriverWait(driver, EXPLICIT_WAIT + settle_extra).until(EC.presence_of_element_located((By.ID, "root")))
    ok_idle = _wait_network_idle(driver, NET_IDLE_QUIET_MS, timeout=min(SPA_READY_MAX_SEC + settle_extra, 120))
    if not ok_idle:
        logger.info("SPA_READY: seguiu sem network-idle (teto de tempo atingido)")
    try: driver.switch_to.default_content()
    except Exception: pass
    selectors = config["selectors"]["upload"]
    try:
        iframe = WebDriverWait(driver, IMPLICIT_WAIT + settle_extra).until(EC.presence_of_element_located((By.XPATH, selectors["iframe"])))
        driver.switch_to.frame(iframe)
        WebDriverWait(driver, EXPLICIT_WAIT + settle_extra).until(EC.presence_of_element_located((By.XPATH, selectors["upload_video"])))
        logger.info("Upload UI pronta (iframe).")
        return
    except Exception:
        try: driver.switch_to.default_content()
        except Exception: pass
        try:
            WebDriverWait(driver, EXPLICIT_WAIT + settle_extra).until(EC.presence_of_element_located((By.XPATH, selectors["upload_video"])))
            logger.info("Upload UI pronta (documento principal).")
            return
        except Exception: pass
    logger.info("Upload UI não evidente após hidratação; continuando com rotina de detecção.")

def _candidate_upload_urls(lang_tag: Optional[str]) -> List[str]:
    base = "https://www.tiktok.com/tiktokstudio/upload"
    lt = (lang_tag or "en-US")
    return [f"{base}?from=webapp&lang={lt}", f"{base}?from=creator_center&lang={lt}", f"{base}?lang={lt}"]

def _warmup_explore(driver: WebDriver, *, slow_network: bool) -> None:
    if not slow_network: return
    try:
        driver.get("https://www.tiktok.com/explore")
        logger.info("Warmup: /explore")
        WebDriverWait(driver, EXPLICIT_WAIT + SLOW_NET_EXTRA_WAIT_SEC).until(EC.presence_of_element_located((By.ID, "root")))
        time.sleep(min(WARMUP_EXPLORE_WAIT_SEC, 90))
    except Exception: pass

def _apply_lang_to_url(url: str, lang_tag: Optional[str]) -> str:
    if not lang_tag: return url
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        qs["lang"] = [lang_tag]
        new_q = urlencode(qs, doseq=True)
        return urlunparse(parsed._replace(query=new_q))
    except Exception: return url

def _is_upload_url(url: str) -> bool:
    try: return urlparse(url).path.rstrip('/') == "/tiktokstudio/upload"
    except Exception: return False
def _on_wrong_hub(url: str) -> bool:
    try: return urlparse(url).path.rstrip('/') == "/tiktokstudio"
    except Exception: return False

def _nav_with_retries(driver: WebDriver, *, lang_tag: Optional[str] = None, slow_network: bool = False) -> None:
    _warmup_explore(driver, slow_network=slow_network)
    candidates = _candidate_upload_urls(lang_tag)
    attempts = NAV_MAX_RETRIES + (1 if slow_network else 0)
    settle_extra = SLOW_NET_EXTRA_WAIT_SEC if slow_network else 0
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        for target in candidates:
            try:
                driver.get(target)
                logger.info("Navegou para: %s", driver.current_url)
                if SMART_WAIT_ENABLE:
                    _wait_spa_ready(driver, lang_tag=lang_tag, slow_network=slow_network)
                else:
                    WebDriverWait(driver, EXPLICIT_WAIT + settle_extra).until(EC.presence_of_element_located((By.ID, "root")))
                    time.sleep(min(UPLOAD_PAGE_SETTLE_SEC + settle_extra, 90))
                if _on_wrong_hub(driver.current_url):
                    logger.info("Redirecionado ao hub /tiktokstudio. Forçando novamente a rota de upload…")
                    continue
                driver.switch_to.default_content()
                return
            except Exception as e: last_error = e
        logger.warning("Timeout/falha abrindo upload (tentativa %d/%d). Retentando em %.1fs…", attempt, attempts, NAV_RETRY_BACKOFF)
        time.sleep(NAV_RETRY_BACKOFF)
    raise TimeoutException(f"Não consegui abrir a página de upload após tentativas. Último erro: {last_error}")

def upload_video(
    filename: str,
    description: Optional[str] = None,
    cookies: str = "",
    username: str = "",
    password: str = "",
    sessionid: Optional[str] = None,
    cookies_list: List[Cookie] = [],
    proxy: Optional[ProxyDict] = None,
    idioma: str = "auto",
    headless: bool = False,
    browser: Literal["chrome", "edge", "firefox"] = "chrome",
    browser_agent: Optional[WebDriver] = None,
    use_vpn: bool = False,
    num_retries: int = 1,
    begin_words: Optional[int] = None,
    *args,
    **kwargs
) -> bool:
    """
    Faz o upload de um único vídeo para o TikTok.
    Unifica a lógica, gerencia o navegador e retorna True para sucesso ou False para falha.
    """
    if not _check_valid_path(filename):
        logger.error("%s é um caminho de arquivo inválido. Abortando.", filename)
        return False

    sleep_jitter_before_post()

    # Determina as configurações de rede e idioma
    want_proxy = _use_proxy_from_idioma(idioma) if not use_vpn else False
    region = _region_from_idioma(idioma)
    lang_tag = _lang_tag_from_idioma(idioma)
    logger.info("upload_video: idioma=%s | want_proxy=%s | use_vpn=%s | region=%s | lang_tag=%s", idioma, want_proxy, use_vpn, region or "-", lang_tag)

    # ##############################################################
    # CORREÇÃO APLICADA AQUI
    # Recupera o nome do perfil da VPN com base no provedor selecionado no .env
    # ##############################################################
    vpn_profile_name = None
    if use_vpn:
        provider = os.getenv("VPN_PROVIDER", "none").lower()
        if provider == 'urban':
            vpn_profile_name = os.getenv("URBANVPN_PROFILE_NAME")
        elif provider == 'zoog':
            vpn_profile_name = os.getenv("ZOOGVPN_PROFILE_NAME")
    # ##############################################################

    # Gerenciamento do WebDriver
    driver = None
    we_created_driver = False
    if not browser_agent:
        we_created_driver = True
    else:
        logger.info("Usando agente de navegador definido pelo usuário")
        driver = browser_agent
        # Lógica para recriar o driver se necessário (mantida)
        is_wire = _is_seleniumwire_driver(driver)
        needs_network_features = want_proxy or use_vpn
        if needs_network_features and not is_wire:
            logger.warning("Driver existente não suporta proxy/VPN. Recriando.")
            try: driver.quit()
            except Exception: pass
            we_created_driver = True
            driver = None

    try:
        if not driver:
            we_created_driver = True
            logger.info("Criando uma instância de navegador %s %s", browser, "(headless)" if headless else "")
            
            driver = get_browser(
                browser,
                headless=headless,
                proxy=proxy,
                idioma=idioma,
                options=ChromeOptions(), # Garante que options seja passado
                want_proxy=want_proxy,
                region=region,
                lang_tag=lang_tag,
                vpn_profile_name=vpn_profile_name, # Passa o nome do perfil CORRETO
                *args,
                **kwargs
            )

        # Autenticação e Configuração
        if use_vpn:
            from ..vpn_manager import connect_vpn
            logger.info("Iniciando procedimento de conexão da VPN...")
            if not connect_vpn(driver):
                raise VpnConnectionError("Não foi possível estabelecer conexão com a VPN.")

        auth = AuthBackend(username=username, password=password, cookies=cookies, cookies_list=cookies_list, sessionid=sessionid)
        driver = auth.authenticate_agent(driver)

        try:
            reg = (region or "").upper()
            tz_info = REGION_TO_TIME.get(reg, None)
            if tz_info:
                _force_timezone(driver, os.getenv(f"TZ_{reg}", tz_info["tz"]), locale=tz_info["locale"])
        except Exception as e:
            logger.warning("Falha ao configurar timezone/locale: %s", e)

        # Execução do Upload
        success = complete_upload_form(
            driver,
            path=abspath(filename),
            description=(description or ""),
            num_retries=num_retries,
            headless=headless,
            idioma=idioma,
            lang_tag=lang_tag,
            begin_words=(begin_words or DEFAULT_BEGIN_WORDS),
            want_proxy=want_proxy,
            **kwargs
        )
        return success

    except Exception as e:
        logger.error("Ocorreu um erro fatal durante o processo de upload: %s", e)
        return False
    finally:
        if we_created_driver and driver:
            try:
                driver.quit()
            except Exception:
                pass
            
def complete_upload_form(
    driver: WebDriver,
    path: str,
    description: str,
    num_retries: int = 1,
    headless: bool = False,
    *,
    idioma: Optional[str] = None,
    lang_tag: Optional[str] = None,
    begin_words: int = DEFAULT_BEGIN_WORDS,
    want_proxy: bool = False,
    **kwargs
) -> bool:
    """
    Preenche o formulário de upload e retorna True em caso de sucesso e False em caso de falha.
    """
    slow = bool(want_proxy or _use_proxy_from_idioma(idioma))
    _nav_with_retries(driver, lang_tag=lang_tag, slow_network=slow)
    _maybe_close_cookie_banner(driver)

    for i in range(1, max(1, PROFILE_RETRIES) + 1):
        info = _log_current_profile(driver, timeout=HEADER_MAX_WAIT_SEC if slow else 12, want_proxy=slow)
        if info:
            break
        if not _is_upload_url(driver.current_url):
            logger.info("Não está na rota de upload (atual=%s). Tentando voltar para /tiktokstudio/upload…", driver.current_url)
            _nav_with_retries(driver, lang_tag=lang_tag, slow_network=slow)
        else:
            logger.info("Perfil não exibido; reabrindo o menu e tentando novamente (%d/%d)…", i, PROFILE_RETRIES)
        time.sleep(2)

    _ensure_upload_ui(driver, slow_network=slow)
    send_ts = _set_video(driver, path=path, num_retries=max(1, num_retries))
    logger.info("Definindo descrição")
    _set_description(driver, description)

    _wait_post_enabled(driver, timeout=UPLOADING_WAIT)

    if DYNAMIC_POST_GRACE:
        logger.info("⏳ Post: usando grace dinâmico (rede/overlays)…")
        _install_network_watch(driver)
        _wait_network_idle(driver, NET_IDLE_QUIET_MS, timeout=15)
        _wait_blocking_overlays_gone(driver, timeout=OVERLAYS_SETTLE_SEC)
        if POST_GRACE_MIN_SEC > 0:
            time.sleep(POST_GRACE_MIN_SEC)
    else:
        elapsed = time.time() - send_ts
        remaining = max(0, POST_MIN_GRACE_SEC - int(elapsed))
        if remaining > 0:
            logger.info("⏳ Grace antes de postar: aguardando %ds (desde o envio)…", remaining)
            time.sleep(remaining)

    if POST_AFTER_ENABLED_EXTRA_SEC > 0:
        time.sleep(POST_AFTER_ENABLED_EXTRA_SEC)

    _wait_blocking_overlays_gone(driver, timeout=OVERLAYS_SETTLE_SEC)
    logger.info("Clicando no botão de postagem")
    _post_video(driver)
    time.sleep(4)

    # **LÓGICA DE RETORNO CORRIGIDA**
    ok = False  # Assume falha por padrão
    if VERIFY_POST_IN_PUBLICATIONS:
        try:
            ok = _confirm_post_in_publications(driver, description or "", timeout=PUBLICATIONS_WAIT_SEC, begin_words=max(1, begin_words))
            if ok:
                logger.info("✅ Post confirmado na tela de Publicações.")
            else:
                logger.warning("⚠️ Não foi possível confirmar o post na tela de Publicações dentro do tempo limite.")
        except Exception as e:
            logger.warning("Falha ao validar na tela de Publicações: %s", e)
            ok = False  # Garante que a exceção resulte em falha
    else:
        logger.info("Verificação de publicação desativada. Assumindo sucesso após o clique.")
        ok = True

    # A limpeza de cookies não afeta o resultado do sucesso/falha
    try:
        time.sleep(6)
        driver.delete_all_cookies()
    except Exception:
        pass

    return ok

def _ensure_upload_ui(driver: WebDriver, *, slow_network: bool = False) -> None:
    selectors = config["selectors"]["upload"]
    def _find_input_in_default() -> Optional[Any]:
        try: return driver.find_element(By.XPATH, selectors["upload_video"])
        except Exception:
            try: return driver.find_element(By.XPATH, "//input[@type='file' and contains(@accept,'video')]")
            except Exception: return None
    for attempt in range(1, IFRAME_MAX_RETRY + 2):
        try:
            driver.switch_to.default_content()
            try:
                iframe = WebDriverWait(driver, IMPLICIT_WAIT + (SLOW_NET_EXTRA_WAIT_SEC if slow_network else 0)).until(EC.presence_of_element_located((By.XPATH, selectors["iframe"])))
                driver.switch_to.frame(iframe)
                WebDriverWait(driver, EXPLICIT_WAIT + (SLOW_NET_EXTRA_WAIT_SEC if slow_network else 0)).until(EC.presence_of_element_located((By.XPATH, selectors["upload_video"])))
                logger.info("Entrou no iframe de upload")
                return
            except TimeoutException:
                driver.switch_to.default_content()
            if _find_input_in_default() is not None:
                logger.info("Upload UI detectada no documento principal (sem iframe)")
                return
        except Exception: pass
        logger.warning("Upload UI não disponível (tentativa %d/%d).", attempt, IFRAME_MAX_RETRY + 1)
        time.sleep(NAV_RETRY_BACKOFF)
        try: _nav_with_retries(driver, slow_network=slow_network)
        except Exception: pass
    raise TimeoutException("UI de upload não apareceu (iframe/input).")

def _maybe_close_cookie_banner(driver: WebDriver) -> None:
    try: _remove_cookies_window(driver)
    except Exception: pass

def _wait_post_enabled(driver: WebDriver, timeout: int = 30) -> None:
    try: WebDriverWait(driver, timeout).until(lambda d: (el := d.find_element(By.XPATH, config["selectors"]["upload"]["post"])) and el.get_attribute("data-disabled") == "false")
    except Exception: pass

def _has_blocking_overlay(driver: WebDriver) -> bool:
    try:
        if any(d.is_displayed() for d in driver.find_elements(By.XPATH, '//*[@role="dialog" and not(@aria-hidden="true")]')): return True
    except Exception: pass
    try:
        if any(m.is_displayed() for m in driver.find_elements(By.XPATH, '//*[contains(@class,"TUXModal") or contains(@class,"TUXAlert")]')): return True
    except Exception: pass
    return False

def _wait_blocking_overlays_gone(driver: WebDriver, timeout: int = 6) -> None:
    deadline = time.time() + max(0, timeout)
    while time.time() < deadline:
        if not _has_blocking_overlay(driver):
            time.sleep(0.5)
            if not _has_blocking_overlay(driver): return
        time.sleep(0.5)

def _click_publish_now_if_modal(driver: WebDriver, wait_secs: int = 30) -> bool:
    deadline = time.time() + max(1, wait_secs)
    def _try_find_and_click() -> bool:
        try: driver.switch_to.default_content()
        except Exception: pass
        texts = ("Publicar agora", "Publish now", "انشر الآن", "Опубликовать сейчас")
        for t in texts:
            try: btn = driver.find_element(By.XPATH, f"//div[contains(@class,'TUXModal') and not(@aria-hidden='true')]//button[contains(@class,'TUXButton--primary') and .//div[normalize-space()='{t}']]")
            except Exception: btn = None
            if btn:
                try:
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(0.8)
                    return True
                except Exception: pass
        try:
            any_primary = driver.find_element(By.XPATH, "//div[contains(@class,'TUXModal') and not(@aria-hidden='true')]//button[contains(@class,'TUXButton--primary')]")
            driver.execute_script("arguments[0].click();", any_primary)
            time.sleep(0.8)
            return True
        except Exception: pass
        return False
    while time.time() < deadline:
        if _try_find_and_click():
            logger.info("✅ Modal de verificação tratado — clicado 'Publicar agora'.")
            return True
        time.sleep(0.5)
    return False

def _set_description(driver: WebDriver, description: str) -> None:
    if description is None: return
    description = (description or "").encode("utf-8", "ignore").decode("utf-8")
    saved_description = description
    WebDriverWait(driver, IMPLICIT_WAIT).until(EC.presence_of_element_located((By.XPATH, config["selectors"]["upload"]["description"])))
    desc = driver.find_element(By.XPATH, config["selectors"]["upload"]["description"])
    desc.click()
    time.sleep(0.2)
    desc.send_keys(Keys.END)
    _clear(desc)
    time.sleep(0.2)
    try:
        words = description.split(" ")
        for word in words:
            if word and word[0] == "#":
                desc.send_keys(word)
                desc.send_keys(" " + Keys.BACKSPACE)
                WebDriverWait(driver, IMPLICIT_WAIT).until(EC.presence_of_element_located((By.XPATH, config["selectors"]["upload"]["mention_box"])))
                time.sleep(ADD_HASHTAG_WAIT)
                desc.send_keys(Keys.ENTER)
            elif word and word[0] == "@":
                logger.info("- Adicionando Menção: %s", word)
                desc.send_keys(word + " ")
                time.sleep(1)
                desc.send_keys(Keys.BACKSPACE)
                WebDriverWait(driver, EXPLICIT_WAIT).until(EC.presence_of_element_located((By.XPATH, config["selectors"]["upload"]["mention_box_user_id"])))
                found = False
                start_time = time.time()
                while not found and (time.time() - start_time < 5):
                    user_id_elements = driver.find_elements(By.XPATH, config["selectors"]["upload"]["mention_box_user_id"])
                    time.sleep(1)
                    for i, user_id_element in enumerate(user_id_elements):
                        if user_id_element and user_id_element.is_enabled():
                            username = (user_id_element.text or "").split(" ")[0]
                            if username.lower() == word[1:].lower():
                                found = True
                                logger.info("Usuário correspondente encontrado: Clicando no %s", username)
                                for _ in range(i): desc.send_keys(Keys.DOWN)
                                desc.send_keys(Keys.ENTER)
                                break
                    if not found: time.sleep(0.5)
            else:
                desc.send_keys((word or "") + " ")
    except Exception as exception:
        logger.error("Falha ao definir descrição: %s", str(exception))
        _clear(desc)
        desc.send_keys(saved_description)

def _clear(element) -> None:
    try: element.send_keys(2 * len(element.text) * Keys.BACKSPACE)
    except Exception: pass

def _wait_upload_started(driver: WebDriver, start_timeout: int) -> bool:
    deadline = time.time() + max(5, start_timeout)
    while time.time() < deadline:
        try:
            if driver.find_elements(By.XPATH, config["selectors"]["upload"]["process_confirmation"]): return True
        except Exception: pass
        try:
            upload_box = driver.find_element(By.XPATH, config["selectors"]["upload"]["upload_video"])
            if driver.execute_script("return arguments[0].files && arguments[0].files.length > 0;", upload_box): return True
        except Exception: pass
        time.sleep(0.5)
    return False

def _set_video(driver: WebDriver, path: str = "", num_retries: int = 3, **kwargs) -> float:
    last_exc: Optional[Exception] = None
    for attempt in range(1, max(1, num_retries) + 1):
        logger.info("Fazendo upload do arquivo de vídeo (tentativa %d/%d)", attempt, num_retries)
        try:
            _change_to_upload_iframe(driver)
            WebDriverWait(driver, EXPLICIT_WAIT).until(EC.presence_of_element_located((By.XPATH, config["selectors"]["upload"]["upload_video"])))
            upload_box = driver.find_element(By.XPATH, config["selectors"]["upload"]["upload_video"])
            upload_box.send_keys(path)
            send_ts = time.time()
            if _wait_upload_started(driver, start_timeout=UPLOADING_WAIT):
                logger.info("✅ Upload iniciou/preview detectado.")
                return send_ts
            last_exc = TimeoutException(f"Timeout esperando início do upload após {UPLOADING_WAIT}s")
            logger.warning(str(last_exc))
            try: driver.switch_to.default_content()
            except Exception: pass
            try: _ensure_upload_ui(driver)
            except Exception: pass
        except Exception as exception:
            last_exc = exception
            logger.warning("Falha ao anexar vídeo (tentativa %d/%d): %s", attempt, num_retries, exception)
            try: driver.switch_to.default_content()
            except Exception: pass
            try: _ensure_upload_ui(driver)
            except Exception: pass
        time.sleep(min(10, NAV_RETRY_BACKOFF * attempt))
    raise FailedToUpload(last_exc or Exception("Falha ao anexar vídeo."))

def _change_to_upload_iframe(driver: WebDriver) -> None:
    try:
        driver.switch_to.default_content()
        iframe = WebDriverWait(driver, IMPLICIT_WAIT).until(EC.presence_of_element_located((By.XPATH, config["selectors"]["upload"]["iframe"])))
        driver.switch_to.frame(iframe)
    except TimeoutException:
        driver.switch_to.default_content()

def _remove_cookies_window(driver) -> None:
    try:
        cookies_banner = WebDriverWait(driver, IMPLICIT_WAIT).until(EC.presence_of_element_located((By.TAG_NAME, config["selectors"]["upload"]["cookies_banner"]["banner"])))
        item = WebDriverWait(driver, IMPLICIT_WAIT).until(EC.visibility_of(cookies_banner.shadow_root.find_element(By.CSS_SELECTOR, config["selectors"]["upload"]["cookies_banner"]["button"])))
        decline_button = WebDriverWait(driver, IMPLICIT_WAIT).until(EC.element_to_be_clickable(item.find_elements(By.TAG_NAME, "button")[0]))
        decline_button.click()
        logger.info("Banner de cookies fechado")
    except Exception: pass

def _set_interactivity(driver: WebDriver, comment: bool = True, stitch: bool = True, duet: bool = True, *args, **kwargs) -> None:
    try:
        logger.info("Definindo configurações de interatividade")
        comment_box = driver.find_element(By.XPATH, config["selectors"]["upload"]["comment"])
        stitch_box = driver.find_element(By.XPATH, config["selectors"]["upload"]["stitch"])
        duet_box = driver.find_element(By.XPATH, config["selectors"]["upload"]["duet"])
        if comment ^ comment_box.is_selected(): comment_box.click()
        if stitch ^ stitch_box.is_selected(): stitch_box.click()
        if duet ^ duet_box.is_selected(): duet_box.click()
    except NoSuchElementException as e:
        logger.warning("Elementos de interatividade não encontrados: %s. Ignorando.", str(e))
    except Exception as e:
        logger.error("Falha ao definir interatividade: %s", str(e))

def _post_video(driver: WebDriver) -> None:
    """
    Localiza o botão de postagem, aguarda até que ele esteja habilitado e, em seguida,
    clica nele usando um clique via JavaScript para maior robustez.
    """
    post_button_xpath = "//button[@data-e2e='post_video_button']"
    
    try:
        # CORREÇÃO: Define uma condição de espera que retorna o elemento do botão
        # apenas quando ele está presente E habilitado (data-disabled="false").
        def post_button_is_ready(d: WebDriver):
            try:
                # Tenta encontrar o botão
                button = d.find_element(By.XPATH, post_button_xpath)
                # Verifica se o botão está habilitado
                if button.get_attribute("data-disabled") == "false":
                    return button  # CONDIÇÃO SATISFEITA: Retorna o ELEMENTO
                return False  # Botão encontrado, mas desabilitado. Continua esperando.
            except NoSuchElementException:
                return False  # Botão ainda não existe. Continua esperando.

        # Aguarda a condição ser satisfeita e armazena o ELEMENTO retornado
        logger.info("Aguardando o botão 'Publicar' ficar habilitado...")
        post_button = WebDriverWait(driver, UPLOADING_WAIT).until(post_button_is_ready)
        
        # Agora a variável `post_button` contém o elemento do botão, e o clique funcionará.
        logger.info("Botão 'Publicar' habilitado. Clicando...")
        driver.execute_script("arguments[0].click();", post_button)
        time.sleep(1.0)

    except TimeoutException:
        logger.error("Timeout: O botão de postagem não ficou habilitado no tempo esperado.")
        raise FailedToUpload("Botão de postagem não habilitado")
    except WebDriverException as e:
        logger.error("Erro ao clicar no botão de postagem: %s", str(e))
        raise

    # Lógica para tratar o modal de confirmação "Publicar agora" (mantida)
    try:
        if _click_publish_now_if_modal(driver, wait_secs=30):
            logger.info("✅ Modal de verificação tratado — clicado 'Publicar agora'.")
    except Exception:
        pass
    
    time.sleep(4)

def _check_valid_path(path: str) -> bool:
    return exists(path) and path.split(".")[-1].lower() in config["supported_file_types"]

def _convert_videos_dict(videos_list_of_dictionaries: List[Dict[str, Any]]) -> List[VideoDict]:
    if not videos_list_of_dictionaries: raise RuntimeError("Nenhum vídeo para upload")
    valid_path = config["valid_path_names"]
    valid_description = config["valid_descriptions"]
    correct_path, correct_description = valid_path[0], valid_description[0]
    def intersection(lst1, lst2): return list(set(lst1) & set(lst2))
    return_list: List[VideoDict] = []
    for elem in videos_list_of_dictionaries:
        elem = {k.strip().lower(): v for k, v in elem.items()}
        keys = elem.keys()
        path_intersection = intersection(valid_path, keys)
        description_intersection = intersection(valid_description, keys)
        if path_intersection:
            path = elem[path_intersection.pop()]
            if not _check_valid_path(path): raise RuntimeError("Caminho inválido: " + path)
            elem[correct_path] = path
        else:
            for _, value in elem.items():
                if isinstance(value, str) and _check_valid_path(value):
                    elem[correct_path] = value
                    break
            else:
                raise RuntimeError("Caminho não encontrado no dicionário: " + str(elem))
        if description_intersection:
            elem[correct_description] = elem[description_intersection.pop()]
        else:
            for _, value in elem.items():
                if isinstance(value, str) and not _check_valid_path(value):
                    elem[correct_description] = value
                    break
            else:
                elem[correct_description] = ""
        return_list.append(elem)
    return return_list

def __get_driver_timezone(driver: WebDriver) -> Any:
    return pytz.timezone(driver.execute_script("return Intl.DateTimeFormat().resolvedOptions().timeZone"))

def _refresh_with_alert(driver: WebDriver) -> None:
    try:
        driver.refresh()
        WebDriverWait(driver, EXPLICIT_WAIT).until(EC.alert_is_present())
        driver.switch_to.alert.accept()
    except Exception as e:
        logger.debug("Sem alert ao atualizar: %s", e)

def _force_timezone(driver: WebDriver, tz: str, locale: Optional[str] = None) -> None:
    try:
        driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": tz})
        if locale:
            try: driver.execute_cdp_cmd("Emulation.setLocaleOverride", {"locale": locale})
            except Exception: pass
        z = driver.execute_script("return Intl.DateTimeFormat().resolvedOptions().timeZone")
        logger.info("🕒 Timezone ativo no navegador: %s", z)
    except Exception as e:
        logger.warning("Falha ao forçar timezone: %s", e)

class FailedToUpload(Exception):
    def __init__(self, message: Optional[str] = None):
        super().__init__(message or self.__doc__)