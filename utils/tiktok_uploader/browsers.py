import logging
import os
from typing import Optional

from dotenv import load_dotenv

# Selenium-Wire (para proxy autenticado em headless)
from seleniumwire import webdriver as wire_webdriver

# Selenium
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.edge.options import Options as EdgeOptions

from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.firefox import GeckoDriverManager
from webdriver_manager.microsoft import EdgeChromiumDriverManager

from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.edge.service import Service as EdgeService


def _mk_seleniumwire_options(use_proxy: bool, host: str, port: str, user: Optional[str], pw: Optional[str]):
    """Monta seleniumwire_options com proxy autenticado quando solicitado."""
    opts = {
        'request_storage': 'none',  # economiza memória
        'verify_ssl': False,        # evita erros de cert com o MITM do selenium-wire
    }
    if use_proxy:
        if user and pw:
            proxy_uri = f"http://{user}:{pw}@{host}:{port}"
        else:
            proxy_uri = f"http://{host}:{port}"
        opts['proxy'] = {
            'http': proxy_uri,
            'https': proxy_uri,
            'no_proxy': 'localhost,127.0.0.1'
        }
        logging.info("Selenium-Wire proxy configurado: %s", proxy_uri)
    return opts


def get_browser(
    name: str = "chrome",
    options=None,
    proxy=None,              # mantido por compatibilidade (não usado; proxy vem do .env)
    idioma: str = 'en',
    headless: bool = False,
    *args, **kwargs
):
    """
    Cria um WebDriver via Selenium-Wire com proxy autenticado quando idioma == 'en'.
    Funciona em headless.
    """
    load_dotenv()
    proxy_host = os.getenv("PROXY_HOST")
    proxy_port = os.getenv("PROXY_PORT")
    proxy_user = os.getenv("PROXY_USER")
    proxy_pass = os.getenv("PROXY_PASS")

    use_proxy = (idioma == 'en') and proxy_host and proxy_port

    if name == "chrome":
        if options is None:
            options = ChromeOptions()
        # Higiene de flags
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-logging")
        options.add_argument("--log-level=3")
        options.add_experimental_option("excludeSwitches", ["enable-logging"])
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        if headless:
            options.add_argument("--headless=new")
            options.add_argument("--ignore-certificate-errors")

        seleniumwire_options = _mk_seleniumwire_options(use_proxy, proxy_host or "", proxy_port or "", proxy_user, proxy_pass)

        driver = wire_webdriver.Chrome(
            service=ChromeService(ChromeDriverManager().install()),
            options=options,
            seleniumwire_options=seleniumwire_options
        )
        # Pequenas proteções de fingerprint
        try:
            driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {"headers": {"Upgrade-Insecure-Requests": "1"}})
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": """
                    try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch(e) {}
                """
            })
        except Exception:
            pass
        return driver

    elif name == "firefox":
        if options is None:
            options = FirefoxOptions()
        if headless:
            options.add_argument("-headless")

        seleniumwire_options = _mk_seleniumwire_options(use_proxy, proxy_host or "", proxy_port or "", proxy_user, proxy_pass)

        return wire_webdriver.Firefox(
            service=FirefoxService(GeckoDriverManager().install()),
            options=options,
            seleniumwire_options=seleniumwire_options
        )

    elif name == "edge":
        if options is None:
            options = EdgeOptions()
        if headless:
            options.add_argument("--headless=new")

        seleniumwire_options = _mk_seleniumwire_options(use_proxy, proxy_host or "", proxy_port or "", proxy_user, proxy_pass)

        return wire_webdriver.Edge(
            service=EdgeService(EdgeChromiumDriverManager().install()),
            options=options,
            seleniumwire_options=seleniumwire_options
        )

    else:
        raise ValueError(f"Navegador {name} não suportado")
