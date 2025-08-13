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
def _use_proxy_from_idioma(idioma: Optional[str]) -> bool:
    """Usa proxy apenas para aliases de EUA/inglês."""
    if not idioma:
        return False
    s = idioma.strip().lower()
    return s in ("en", "en-us", "us", "usa", "eua", "ingles", "inglês", "english")


def _lang_tag_and_header(idioma: Optional[str]) -> Tuple[str, str]:
    """Define lang tag e Accept-Language segundo o idioma."""
    s = (idioma or "").strip().lower()
    if s in ("pt", "pt-br", "br", "brasil", "portugues", "português"):
        return "pt-BR", "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"
    return "en-US", "en-US,en;q=0.9"


def _silenciar_ruido(nivel_wire=logging.ERROR, nivel_net=logging.WARNING, nivel_wdm=logging.WARNING) -> None:
    """Reduz verbosidade de libs ruidosas."""
    # seleniumwire / mitmproxy
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

    # urllib3 / requests
    for name in ("urllib3", "requests.packages.urllib3"):
        lg = logging.getLogger(name)
        lg.setLevel(nivel_net)
        lg.propagate = False

    # webdriver_manager
    for name in ("webdriver_manager", "WDM"):
        lg = logging.getLogger(name)
        lg.setLevel(nivel_wdm)
        lg.propagate = False


def _mk_seleniumwire_options(
    use_proxy: bool,
    host: str,
    port: str,
    user: Optional[str],
    pw: Optional[str],
):
    """Monta seleniumwire_options com proxy autenticado + scopes (menos ruído)."""
    opts = {
        "request_storage": "none",   # menos memória/IO
        "verify_ssl": False,         # evita erros de cert no MITM local
        # captura só o que interessa
        "scopes": [
            r".*\.tiktok\.com.*",
            r".*\.tiktokcdn\.com.*",
            r".*\.ttwstatic\.com.*",
        ],
    }
    if use_proxy and host and port:
        if user and pw:
            proxy_uri = f"http://{user}:{pw}@{host}:{port}"
        else:
            proxy_uri = f"http://{host}:{port}"
        opts["proxy"] = {
            "http": proxy_uri,
            "https": proxy_uri,
            "no_proxy": "localhost,127.0.0.1",
        }
        logging.info("UPSTREAM proxy ON: %s", proxy_uri)
    return opts


# -------------------------------------------------------------------
# API principal
# -------------------------------------------------------------------
def get_browser(
    name: str = "chrome",
    options=None,
    proxy=None,              # compat (não usado; proxy vem do .env)
    idioma: str = "auto",    # default 'auto' => sem proxy
    headless: bool = False,
    *args, **kwargs
):
    """
    Cria um WebDriver:
    - EUA/inglês => Selenium-Wire + proxy externo autenticado (se PROXY_* definidos)
    - PT-BR/others => Selenium "puro" (sem Selenium-Wire, sem MITM local)
    - Ajusta Accept-Language / --lang conforme idioma.
    - Silencia logs ruidosos (sempre).
    """
    # Silencia ruído ANTES de instalar drivers/levantar proxy
    _silenciar_ruido()

    load_dotenv()
    proxy_host = os.getenv("PROXY_HOST") or ""
    proxy_port = os.getenv("PROXY_PORT") or ""
    proxy_user = os.getenv("PROXY_USER")
    proxy_pass = os.getenv("PROXY_PASS")

    want_proxy = _use_proxy_from_idioma(idioma)
    lang_tag, accept_lang = _lang_tag_and_header(idioma)

    logging.info("get_browser: idioma=%s | want_proxy=%s | lang_tag=%s", idioma, want_proxy, lang_tag)

    # ---------------- CHROME ----------------
    if name == "chrome":
        if options is None:
            options = ChromeOptions()
        # flags de higiene
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
            # Import tardio do Selenium-Wire só quando necessário
            try:
                from seleniumwire import webdriver as wire_webdriver  # type: ignore
            except Exception as e:
                raise RuntimeError(f"Selenium-Wire não instalado, mas proxy foi solicitado: {e}")

            sw_opts = _mk_seleniumwire_options(True, proxy_host, proxy_port, proxy_user, proxy_pass)
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

        # Cabeçalhos + anti-webdriver (Chromium/CDP)
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

            sw_opts = _mk_seleniumwire_options(True, proxy_host, proxy_port, proxy_user, proxy_pass)
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

            sw_opts = _mk_seleniumwire_options(True, proxy_host, proxy_port, proxy_user, proxy_pass)
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

        # Edge também via CDP
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
