# utils/tiktok_uploader/browsers.py
import logging
import os
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
    return "en"


def _use_proxy_from_idioma(idioma: Optional[str]) -> bool:
    """Heurística padrão: liga proxy para EN e AR (Egito)."""
    lang = _idioma_norm(idioma)
    return lang in ("en", "ar")


def _compute_region_from_idioma(idioma: Optional[str]) -> Optional[str]:
    """Region default por idioma (hoje só especial-caso para árabe)."""
    return "EG" if _idioma_norm(idioma) == "ar" else None


def _lang_tag_and_header_from_idioma(idioma: Optional[str]) -> Tuple[str, str]:
    lang = _idioma_norm(idioma)
    if lang == "pt":
        return "pt-BR", "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"
    if lang == "ar":
        return "ar-EG", "ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7"
    return "en-US", "en-US,en;q=0.9"


def _accept_header_from_tag(tag: Optional[str]) -> str:
    tag = (tag or "").strip() or "en-US"
    if tag.lower().startswith("pt"):
        return "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"
    if tag.lower().startswith("ar"):
        return "ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7"
    return "en-US,en;q=0.9"


def _silenciar_ruido(nivel_wire=logging.ERROR, nivel_net=logging.WARNING, nivel_wdm=logging.WARNING) -> None:
    """Reduz verbosidade de libs ruidosas."""
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


def _resolve_proxy_env(region: Optional[str]) -> Tuple[str, str, Optional[str], Optional[str]]:
    """
    Lê variáveis de ambiente com suporte a região:
      - EG => PROXY_EG_HOST/PORT/USER/PASS
      - padrão => PROXY_HOST/PORT/USER/PASS
    """
    load_dotenv()
    prefix = "PROXY_EG" if (region or "").upper() == "EG" else "PROXY"
    host = os.getenv(f"{prefix}_HOST") or ""
    port = os.getenv(f"{prefix}_PORT") or ""
    user = os.getenv(f"{prefix}_USER")
    pw   = os.getenv(f"{prefix}_PASS")
    return host, port, user, pw


def _mk_seleniumwire_options(
    use_proxy: bool,
    region: Optional[str],
):
    """Monta seleniumwire_options com proxy autenticado + scopes (menos ruído)."""
    host, port, user, pw = _resolve_proxy_env(region)
    opts = {
        "request_storage": "none",
        "verify_ssl": False,
        "scopes": [
            r".*\.tiktok\.com.*",
            r".*\.tiktokcdn\.com.*",
            r".*\.ttwstatic\.com.*",
        ],
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
            logging.info("🌐 UPSTREAM proxy ON (%s): %s", (region or "DEFAULT"), proxy_uri)
        else:
            logging.warning("⚠️ want_proxy=True mas PROXY vars ausentes para região %s. Caindo para conexão direta.", region or "DEFAULT")
    return opts


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
    # NOVOS PARÂMETROS (respeitados se fornecidos)
    want_proxy: Optional[bool] = None,
    region: Optional[str] = None,
    lang_tag: Optional[str] = None,
    **kwargs
):
    """
    Cria um WebDriver:
    - Se want_proxy=True => Selenium-Wire + proxy upstream (PROXY_* ou PROXY_<REG>_*)
    - Se want_proxy=False => Selenium "puro"
    - region pode ser 'EG' p/ árabe (lê PROXY_EG_*). Se None, cai para PROXY_*.
    - Define Accept-Language/--lang segundo lang_tag (ou deduz de 'idioma').
    """
    _silenciar_ruido()

    # Defaults se não vierem explícitos
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
        options.add_argument(f"--lang={lang_tag}")
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
                service=ChromeService(ChromeDriverManager().install()),
                options=options,
                seleniumwire_options=sw_opts,
            )
        else:
            logging.info("Conexão direta (sem proxy/upstream).")
            driver = std_webdriver.Chrome(
                service=ChromeService(ChromeDriverManager().install()),
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
                service=FirefoxService(GeckoDriverManager().install()),
                options=options,
                seleniumwire_options=sw_opts,
            )
        else:
            logging.info("Conexão direta (sem proxy/upstream).")
            return std_webdriver.Firefox(
                service=FirefoxService(GeckoDriverManager().install()),
                options=options,
            )

    # ---------------- EDGE ----------------
    elif name == "edge":
        if options is None:
            options = EdgeOptions()
        options.add_argument(f"--lang={lang_tag}")
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
                service=EdgeService(EdgeChromiumDriverManager().install()),
                options=options,
                seleniumwire_options=sw_opts,
            )
        else:
            logging.info("Conexão direta (sem proxy/upstream).")
            driver = std_webdriver.Edge(
                service=EdgeService(EdgeChromiumDriverManager().install()),
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
        return driver

    else:
        raise ValueError(f"Navegador {name} não suportado")
