"""Obtém o navegador com base na entrada do usuário"""

from selenium.webdriver.remote.webdriver import WebDriver

from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.safari.options import Options as SafariOptions
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.common.options import BaseOptions
from selenium.webdriver.common.service import Service

# Gerenciadores de WebDriver
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.firefox.service import Service as FirefoxService
from webdriver_manager.firefox import GeckoDriverManager
from webdriver_manager.microsoft import EdgeChromiumDriverManager
from selenium.webdriver.edge.service import Service as EdgeService

from selenium import webdriver

from . import config
from .proxy_auth_extension.proxy_auth_extension import (
    generate_proxy_auth_extension,
)

from typing import Literal, Any, Type, Callable

browser_t = Literal["chrome", "safari", "chromium", "edge", "firefox"]


def get_browser(
    name: browser_t = "chrome", options: Any | None = None, *args, **kwargs
) -> WebDriver:
    """
    Obtém um navegador com base no nome, com a capacidade de passar argumentos adicionais
    """

    # obtém o web driver para o navegador
    driver_to_use = get_driver(name, *args, **kwargs)

    # obtém as opções para o navegador
    options = options or get_default_options(name, *args, **kwargs)

    # combina-os em um driver completo
    service = get_service(name=name)
    if service:
        driver = driver_to_use(service=service, options=options)  # type: ignore
    else:
        driver = driver_to_use(options=options)

    driver.implicitly_wait(config["implicit_wait"])

    return driver


def get_driver(name: str, *args, **kwargs) -> Type[WebDriver]:
    """
    Obtém a função do web driver para o navegador
    """
    clean_name = _clean_name(name)
    if clean_name in drivers:
        return drivers[clean_name]

    raise UnsupportedBrowserException()


def get_service(name: str):
    """
    Obtém um serviço para instalar o driver do navegador conforme a documentação do webdriver-manager

    https://pypi.org/project/webdriver-manager/
    """
    if _clean_name(name) in services:
        return services[name]()

    return None  # Safari não precisa de um serviço


def get_default_options(name: browser_t, *args, **kwargs) -> BaseOptions:
    """
    Obtém as opções padrão para cada navegador para ajudar a permanecer indetectável
    """
    cleaned_name = _clean_name(name)

    if cleaned_name in defaults:
        return defaults[cleaned_name](*args, **kwargs)

    raise UnsupportedBrowserException()


def chrome_defaults(
    *args, headless: bool = False, proxy: dict | None = None, **kwargs
) -> ChromeOptions:
    """
    Cria o Chrome com Opções
    """

    options = ChromeOptions()

    ## padrão
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--profile-directory=Default")

    ## experimental
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    ## adiciona idioma inglês para evitar erro de tradução de idiomas
    options.add_argument("--lang=en")

    # headless
    if headless:
        options.add_argument("--headless=new")
    if proxy:
        if "user" in proxy.keys() and "pass" in proxy.keys():
            # Isso pode falhar se você executar a função mais de uma vez no mesmo tempo
            extension_file = "temp_proxy_auth_extension.zip"
            generate_proxy_auth_extension(
                proxy["host"],
                proxy["port"],
                proxy["user"],
                proxy["password"],
                extension_file,
            )
            options.add_extension(extension_file)
        else:
            options.add_argument(f"--proxy-server={proxy['host']}:{proxy['port']}")

    return options


def firefox_defaults(
    *args, headless: bool = False, proxy: dict | None = None, **kwargs
) -> FirefoxOptions:
    """
    Cria o Firefox com opções padrão
    """

    options = FirefoxOptions()

    # opções padrão

    if headless:
        options.add_argument("--headless")
    if proxy:
        raise NotImplementedError("Suporte a proxy não implementado para este navegador")
    return options


def safari_defaults(
    *args, headless: bool = False, proxy: dict | None = None, **kwargs
) -> SafariOptions:
    """
    Cria o Safari com opções padrão
    """
    options = SafariOptions()

    # opções padrão

    if headless:
        options.add_argument("--headless")
    if proxy:
        raise NotImplementedError("Suporte a proxy não implementado para este navegador")
    return options


def edge_defaults(
    *args, headless: bool = False, proxy: dict | None = None, **kwargs
) -> EdgeOptions:
    """
    Cria o Edge com opções padrão
    """
    options = EdgeOptions()

    # opções padrão

    if headless:
        options.add_argument("--headless")
    if proxy:
        raise NotImplementedError("Suporte a proxy não implementado para este navegador")
    return options


# Diversos
class UnsupportedBrowserException(Exception):
    """
    Navegador não é suportado pela biblioteca

    Navegadores suportados são:
        - Chrome
        - Firefox
        - Safari
        - Edge
    """

    def __init__(self, message: str | None = None):
        super().__init__(message or self.__doc__)


def _clean_name(name: str) -> str:
    """
    Limpa o nome do navegador para facilitar o uso
    """
    return name.strip().lower()


drivers: dict[str, Type[WebDriver]] = {
    "chrome": webdriver.Chrome,
    "firefox": webdriver.Firefox,
    "safari": webdriver.Safari,
    "edge": webdriver.ChromiumEdge,
}

defaults: dict[str, Callable[..., BaseOptions]] = {
    "chrome": chrome_defaults,
    "firefox": firefox_defaults,
    "safari": safari_defaults,
    "edge": edge_defaults,
}


services: dict[str, Callable[[], Service]] = {
    "chrome": lambda: ChromeService(ChromeDriverManager().install()),
    "firefox": lambda: FirefoxService(GeckoDriverManager().install()),
    "edge": lambda: EdgeService(EdgeChromiumDriverManager().install()),
}