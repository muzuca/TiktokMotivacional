# utils/tiktok_uploader/browsers.py
# Navegador com/sem proxy upstream por região + perfil/cache persistentes por instância.
# Atualizado: suporte completo a RU (ru-RU, PROXY_RU_*), diretórios DENTRO de "cache/".

import logging
import os
import uuid
from typing import Optional, Tuple

from dotenv import load_dotenv

# Selenium "puro" (sem Selenium-Wire)
from selenium import webdriver as std_webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.edge.options import Options as EdgeOptions

from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.firefox import GeckoDriverManager
from webdriver_manager.microsoft import EdgeChromiumDriverManager

from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.edge.service import Service as EdgeService


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def _idioma_norm(idioma: Optional[str]) -> str:
    s = (idioma or "").strip().lower()
    if s.startswith("ar"):
        return "ar"
    if s.startswith("pt"):
        return "pt"
    if s.startswith("ru"):
        return "ru"
    return "en"


def _use_proxy_from_idioma(idioma: Optional[str]) -> bool:
    """Heurística: liga proxy para EN (US), AR (EG) e RU (RU)."""
    lang = _idioma_norm(idioma)
    return lang in ("en", "ar", "ru")


def _compute_region_from_idioma(idioma: Optional[str]) -> Optional[str]:
    """Mapeia idioma -> região do proxy/perfil."""
    lang = _idioma_norm(idioma)
    if lang == "ar":
        return "EG"
    if lang == "en":
        return "US"
    if lang == "pt":
        return "BR"
    if lang == "ru":
        return "RU"
    return None


def _lang_tag_and_header_from_idioma(idioma: Optional[str]) -> Tuple[str, str]:
    lang = _idioma_norm(idioma)
    if lang == "pt":
        return "pt-BR", "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"
    if lang == "ar":
        return "ar-EG", "ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7"
    if lang == "ru":
        return "ru-RU", "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
    return "en-US", "en-US,en;q=0.9"


def _accept_header_from_tag(tag: Optional[str]) -> str:
    tag = (tag or "").strip() or "en-US"
    tl = tag.lower()
    if tl.startswith("pt"):
        return "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"
    if tl.startswith("ar"):
        return "ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7"
    if tl.startswith("ru"):
        return "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
    return "en-US,en;q=0.9"


def _silenciar_ruido(nivel_wire=logging.ERROR, nivel_net=logging.WARNING, nivel_wdm=logging.WARNING) -> None:
    for name in (
        "seleniumwire",
        "seleniumwire.server",
        "seleniumwire.proxy",
        "seleniumwire.handler",
        "seleniumwire.backend",
        "seleniumwire.thirdparty.mitmproxy",
        "mitmproxy",
    ):
        lg = logging.getLogger(name)
        lg.setLevel(nivel_wire)
        lg.propagate = False

    for name in ("urllib3", "requests.packages.urllib3"):
        lg = logging.getLogger(name)
        lg.setLevel(nivel_net)
        lg.propagate = False

    for name in ("webdriver_manager", "WDM"):
        lg = logging.getLogger(name)
        lg.setLevel(nivel_wdm)
        lg.propagate = False


def _env_first(*keys: str, default: str = "") -> str:
    """Retorna o primeiro env não vazio dentre as chaves fornecidas."""
    for k in keys:
        v = os.getenv(k)
        if v:
            return v
    return default


def _resolve_proxy_env(region: Optional[str]) -> Tuple[str, str, Optional[str], Optional[str], str]:
    """
    Suporta esquema semântico por região:
      - US => PROXY_US_HOST/PORT/USER/PASS (também aceita PROXY_HOST_US/PORT_US/..)
      - EG => PROXY_EG_HOST/PORT/USER/PASS (também aceita PROXY_HOST_EG/PORT_EG/..)
      - RU => PROXY_RU_HOST/PORT/USER/PASS (também aceita PROXY_HOST_RU/PORT_RU/..)
    Fallback (sem região): PROXY_HOST/PORT/USER/PASS
    Retorna: host, port, user, pass, region_key_usada
    """
    load_dotenv()
    reg = (region or "").upper()

    if reg == "US":
        host = _env_first("PROXY_US_HOST", "PROXY_HOST_US", "PROXY_HOST")
        port = _env_first("PROXY_US_PORT", "PROXY_PORT_US", "PROXY_PORT")
        user = _env_first("PROXY_US_USER", "PROXY_USER_US", "PROXY_USER") or None
        pw   = _env_first("PROXY_US_PASS", "PROXY_PASS_US", "PROXY_PASS") or None
        return host, port, user, pw, "US"
    elif reg == "EG":
        host = _env_first("PROXY_EG_HOST", "PROXY_HOST_EG", "PROXY_HOST")
        port = _env_first("PROXY_EG_PORT", "PROXY_PORT_EG", "PROXY_PORT")
        user = _env_first("PROXY_EG_USER", "PROXY_USER_EG", "PROXY_USER") or None
        pw   = _env_first("PROXY_EG_PASS", "PROXY_PASS_EG", "PROXY_PASS") or None
        return host, port, user, pw, "EG"
    elif reg == "RU":
        host = _env_first("PROXY_RU_HOST", "PROXY_HOST_RU", "PROXY_HOST")
        port = _env_first("PROXY_RU_PORT", "PROXY_PORT_RU", "PROXY_PORT")
        user = _env_first("PROXY_RU_USER", "PROXY_USER_RU", "PROXY_USER") or None
        pw   = _env_first("PROXY_RU_PASS", "PROXY_PASS_RU", "PROXY_PASS") or None
        return host, port, user, pw, "RU"
    elif reg == "ID":
        host = _env_first("PROXY_ID_HOST", "PROXY_HOST_ID", "PROXY_HOST")
        port = _env_first("PROXY_ID_PORT", "PROXY_PORT_ID", "PROXY_PORT")
        user = _env_first("PROXY_ID_USER", "PROXY_USER_ID", "PROXY_USER") or None
        pw   = _env_first("PROXY_ID_PASS", "PROXY_PASS_ID", "PROXY_PASS") or None
        return host, port, user, pw, "ID"
    else:
        host = _env_first("PROXY_HOST")
        port = _env_first("PROXY_PORT")
        user = _env_first("PROXY_USER") or None
        pw   = _env_first("PROXY_PASS") or None
        return host, port, user, pw, "DEFAULT"


def _mk_seleniumwire_options(
    use_proxy: bool,
    region: Optional[str],
):
    """Monta seleniumwire_options com proxy autenticado + scopes (menos ruído)."""
    host, port, user, pw, key = _resolve_proxy_env(region)
    opts = {
        "request_storage": "none",
        "verify_ssl": False,
        "scopes": [
            r".*\.tiktok\.com.*",
            r".*\.tiktokcdn\.com.*",
            r".*\.ttwstatic\.com.*",
        ],
        # 👇 evita colisão entre instâncias (proxy interno do Selenium-Wire)
        "port": 0,
        "addr": "127.0.0.1",
        "auto_config": True,
    }
    if use_proxy:
        if host and port:
            if user and pw:
                proxy_uri = f"http://{user}:{pw}@{host}:{port}"
            else:
                proxy_uri = f"http://{host}:{port}"
            opts["proxy"] = {
                "http": proxy_uri,
                "https": proxy_uri,
                "no_proxy": "localhost,127.0.0.1",
            }
            logging.info("🌐 UPSTREAM proxy ON (%s): %s", key, proxy_uri)
        else:
            logging.warning("⚠️ want_proxy=True mas PROXY vars ausentes para %s. Caindo para conexão direta.", key)
    return opts

# ---------- cache/perfil por região + instância ----------
def _norm_region(region: Optional[str]) -> str:
    r = (region or "").strip().upper()
    if r in {"US", "EG", "BR", "RU"}:
        return r
    return "DEFAULT"

def _instance_tag() -> str:
    tag = (os.getenv("INSTANCE_TAG") or "").strip()
    return tag if tag else uuid.uuid4().hex[:8]

def _profile_roots(region: Optional[str]) -> Tuple[str, str]:
    """
    Diretórios por região E instância (agora DENTRO de cache/):
      - base de perfis: CHROME_PROFILE_BASE (default: cache/chrome_profiles)
      - base de cache:  CHROME_DISK_CACHE_BASE (default: cache/chrome_cache)
    Gera: <base>/<REGIÃO>/<INSTANCE_TAG>
    """
    reg = _norm_region(region)
    tag = _instance_tag()
    base_profile = os.getenv("CHROME_PROFILE_BASE", os.path.join("cache", "chrome_profiles"))
    base_cache   = os.getenv("CHROME_DISK_CACHE_BASE", os.path.join("cache", "chrome_cache"))
    user_data_dir  = os.path.join(base_profile, reg, tag)
    disk_cache_dir = os.path.join(base_cache, reg, tag)
    os.makedirs(user_data_dir, exist_ok=True)
    os.makedirs(disk_cache_dir, exist_ok=True)
    return user_data_dir, disk_cache_dir

def _unlock_profile(user_data_dir: str) -> None:
    """Remove locks residuais quando o Chrome é encerrado à força."""
    try:
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "SingletonTabs"):
            p = os.path.join(user_data_dir, name)
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
    except Exception:
        pass


# -------------------------------------------------------------------
# API principal
# -------------------------------------------------------------------
def get_browser(
    name: str = "chrome",
    options=None,
    proxy=None,                    # compat
    idioma: str = "auto",
    headless: bool = False,
    *,
    want_proxy: Optional[bool] = None,
    region: Optional[str] = None,
    lang_tag: Optional[str] = None,
    **kwargs
):
    """
    Cria um WebDriver:
    - Se want_proxy=True => Selenium-Wire + proxy upstream (PROXY_* ou PROXY_<REG>_*).
    - Se want_proxy=False => Selenium "puro".
    - region pode ser 'US', 'EG', 'BR' ou 'RU'. Se None, usa heurística por idioma.
    - Define Accept-Language/--lang segundo lang_tag (ou deduz de 'idioma').
    - Chrome/Edge: perfil e cache persistentes por **instância** (agora dentro de cache/).
    """
    _silenciar_ruido()

    if want_proxy is None:
        want_proxy = _use_proxy_from_idioma(idioma)
    if region is None:
        region = _compute_region_from_idioma(idioma)
    if lang_tag is None:
        lang_tag, _ = _lang_tag_and_header_from_idioma(idioma)

    accept_lang = _accept_header_from_tag(lang_tag)

    logging.info("get_browser: idioma=%s | want_proxy=%s | region=%s | lang_tag=%s",
                 idioma, want_proxy, region or "-", lang_tag)

    # ---------------- CHROME ----------------
    if name == "chrome":
        if options is None:
            options = ChromeOptions()
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-logging")
        options.add_argument("--log-level=3")
        options.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--remote-debugging-pipe")  # 👈 evita porta
        options.add_argument(f"--lang={lang_tag}")
        # ===== perfil/cache por instância =====
        user_data_dir, disk_cache_dir = _profile_roots(region)
        _unlock_profile(user_data_dir)
        options.add_argument(f"--user-data-dir={os.path.abspath(user_data_dir)}")
        options.add_argument(f"--disk-cache-dir={os.path.abspath(disk_cache_dir)}")
        cache_size = os.getenv("CHROME_DISK_CACHE_SIZE", "").strip()
        if cache_size.isdigit():
            options.add_argument(f"--disk-cache-size={cache_size}")
        else:
            options.add_argument("--disk-cache-size=1073741824")  # ~1GiB

        if headless:
            options.add_argument("--headless=new")
            options.add_argument("--ignore-certificate-errors")

        if want_proxy:
            try:
                from seleniumwire import webdriver as wire_webdriver  # type: ignore
            except Exception as e:
                raise RuntimeError(f"Selenium-Wire não instalado, mas proxy foi solicitado: {e}")

            sw_opts = _mk_seleniumwire_options(True, region)
            driver = wire_webdriver.Chrome(
                service=ChromeService(ChromeDriverManager().install(), port=0),  # 👈 porta aleatória
                options=options,
                seleniumwire_options=sw_opts,
            )
        else:
            logging.info("Conexão direta (sem proxy/upstream).")
            driver = std_webdriver.Chrome(
                service=ChromeService(ChromeDriverManager().install(), port=0),  # 👈 porta aleatória
                options=options,
            )

        try:
            driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {
                "headers": {"Accept-Language": accept_lang, "Upgrade-Insecure-Requests": "1"}
            })
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": "try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch(e) {}"
            })
        except Exception:
            pass

        try:
            pid = getattr(getattr(driver, "service", None), "process", None)
            pid = getattr(pid, "pid", None)
            setattr(driver, "_tm_instance", {"tag": _instance_tag(), "pid": pid})
        except Exception:
            pass

        logging.info("🗂️ Perfil Chrome: %s | Cache: %s | Região: %s | Headless: %s",
                     os.path.abspath(user_data_dir), os.path.abspath(disk_cache_dir), (region or 'DEFAULT'), headless)
        return driver

    # ---------------- FIREFOX ----------------
    elif name == "firefox":
        if options is None:
            options = FirefoxOptions()
        if headless:
            options.add_argument("-headless")
        try:
            options.set_preference("intl.accept_languages", accept_lang)
        except Exception:
            pass

        if want_proxy:
            try:
                from seleniumwire import webdriver as wire_webdriver  # type: ignore
            except Exception as e:
                raise RuntimeError(f"Selenium-Wire não instalado, mas proxy foi solicitado: {e}")

            sw_opts = _mk_seleniumwire_options(True, region)
            return wire_webdriver.Firefox(
                service=FirefoxService(GeckoDriverManager().install(), port=0),  # 👈 aleatória
                options=options,
                seleniumwire_options=sw_opts,
            )
        else:
            logging.info("Conexão direta (sem proxy/upstream).")
            return std_webdriver.Firefox(
                service=FirefoxService(GeckoDriverManager().install(), port=0),  # 👈 aleatória
                options=options,
            )

    # ---------------- EDGE ----------------
    elif name == "edge":
        if options is None:
            options = EdgeOptions()
        options.add_argument("--remote-debugging-pipe")
        options.add_argument(f"--lang={lang_tag}")
        # ===== perfil/cache por instância =====
        user_data_dir, disk_cache_dir = _profile_roots(region)
        _unlock_profile(user_data_dir)
        options.add_argument(f"--user-data-dir={os.path.abspath(user_data_dir)}")
        options.add_argument(f"--disk-cache-dir={os.path.abspath(disk_cache_dir)}")
        cache_size = os.getenv("CHROME_DISK_CACHE_SIZE", "").strip()
        if cache_size.isdigit():
            options.add_argument(f"--disk-cache-size={cache_size}")
        else:
            options.add_argument("--disk-cache-size=1073741824")

        if headless:
            options.add_argument("--headless=new")
            options.add_argument("--ignore-certificate-errors")

        if want_proxy:
            try:
                from seleniumwire import webdriver as wire_webdriver  # type: ignore
            except Exception as e:
                raise RuntimeError(f"Selenium-Wire não instalado, mas proxy foi solicitado: {e}")

            sw_opts = _mk_seleniumwire_options(True, region)
            driver = wire_webdriver.Edge(
                service=EdgeService(EdgeChromiumDriverManager().install(), port=0),  # 👈 aleatória
                options=options,
                seleniumwire_options=sw_opts,
            )
        else:
            logging.info("Conexão direta (sem proxy/upstream).")
            driver = std_webdriver.Edge(
                service=EdgeService(EdgeChromiumDriverManager().install(), port=0),  # 👈 aleatória
                options=options,
            )

        try:
            driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {
                "headers": {"Accept-Language": accept_lang, "Upgrade-Insecure-Requests": "1"}
            })
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": "try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch(e) {}"
            })
        except Exception:
            pass

        try:
            pid = getattr(getattr(driver, "service", None), "process", None)
            pid = getattr(pid, "pid", None)
            setattr(driver, "_tm_instance", {"tag": _instance_tag(), "pid": pid})
        except Exception:
            pass

        logging.info("🗂️ Perfil Edge: %s | Cache: %s | Região: %s | Headless: %s",
                     os.path.abspath(user_data_dir), os.path.abspath(disk_cache_dir), (region or 'DEFAULT'), headless)
        return driver

    else:
        raise ValueError(f"Navegador {name} não suportado")