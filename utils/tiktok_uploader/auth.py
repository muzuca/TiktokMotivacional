"""
Gerencia a autenticação para o TikTokUploader
"""

import logging
from http import cookiejar
from time import time, sleep
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.remote.webdriver import WebDriver
from . import config
from .browsers import get_browser
from .utils import green
from .types import Cookie, cookie_from_dict

# Configuração do logging com timestamps, alinhado com outros arquivos
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

class AuthBackend:
    """
    Gerencia a autenticação para o TikTokUploader
    """

    username: str
    password: str
    cookies: list[Cookie]

    def __init__(
        self,
        username: str = "",
        password: str = "",
        cookies_list: list[Cookie] = [],
        cookies: str | None = None,
        cookies_str: str | None = None,
        sessionid: str | None = None,
    ):
        """
        Cria o backend de autenticação

        Argumentos de palavra-chave:
        - username -> o nome de usuário ou email da conta
        - password -> a senha da conta

        - cookies -> uma lista de dicionários de cookies compatíveis com Selenium
        """
        if (username and not password) or (password and not username):
            raise InsufficientAuth()

        self.cookies = self.get_cookies(path=cookies) if cookies else []
        self.cookies += self.get_cookies(cookies_str=cookies_str) if cookies_str else []
        self.cookies += cookies_list
        self.cookies += [{"name": "sessionid", "value": sessionid}] if sessionid else []

        if not (self.cookies or (username and password)):
            raise InsufficientAuth()

        self.username = username
        self.password = password

        if cookies:
            logger.info(green("Autenticando navegador com cookies"))
        elif username and password:
            logger.info(green("Autenticando navegador com nome de usuário e senha"))
        elif sessionid:
            logger.info(green("Autenticando navegador com sessionid"))
        elif cookies_list:
            logger.info(green("Autenticando navegador com cookies_list"))

    def authenticate_agent(self, driver: WebDriver) -> WebDriver:
        """
        Autentica o agente usando o backend do navegador
        """
        # tenta usar cookies
        if not self.cookies and self.username and self.password:
            self.cookies = login(driver, username=self.username, password=self.password)

        logger.info(green("Autenticando navegador com cookies"))

        driver.get(config["paths"]["main"])

        WebDriverWait(driver, config["explicit_wait"]).until(
            EC.title_contains("TikTok")
        )

        for cookie in self.cookies:
            try:
                driver.add_cookie(cookie)
            except Exception as e:
                logger.error("Falha ao adicionar cookie %s: %s", cookie, str(e))

        return driver

    def get_cookies(
        self, path: str | None = None, cookies_str: str | None = None
    ) -> list[Cookie]:
        """
        Obtém cookies do arquivo passado usando o padrão netscape
        """
        if path:
            with open(path, "r", encoding="utf-8") as file:
                lines = file.read().split("\n")
        elif cookies_str is not None:
            lines = cookies_str.split("\n")
        else:
            raise ValueError("Deve ter um caminho ou uma cookies_str")

        return_cookies: list[Cookie] = []
        for line in lines:
            split = line.split("\t")
            if len(split) < 6:
                continue

            split = [x.strip() for x in split]

            name = split[5]
            value = split[6]
            domain = split[0]
            path = split[2]

            return_cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": path,
                }
            )

            try:
                return_cookies[-1]["expiry"] = int(split[4])
            except ValueError:
                continue
        return return_cookies


def login_accounts(
    driver: WebDriver | None = None, accounts=[(None, None)], *args, **kwargs
) -> dict[str, list[Cookie]]:
    """
    Autentica as contas usando o backend do navegador e salva as credenciais necessárias

    Argumentos de palavra-chave:
    - driver -> o webdriver a ser usado
    - accounts -> uma lista de tuplas na forma (nome de usuário, senha)
    """
    driver = driver or get_browser(headless=False, *args, **kwargs)

    cookies = {}
    for account in accounts:
        username, password = get_username_and_password(account)
        cookies[username] = login(driver, username, password)

    return cookies


def login(driver: WebDriver, username: str, password: str) -> list[Cookie]:
    """
    Faz login do usuário usando o email e a senha
    """
    if not (username and password):
        raise InsufficientAuth("Nome de usuário e senha são obrigatórios")

    if config["paths"]["main"] not in driver.current_url:
        driver.get(config["paths"]["main"])

    if driver.get_cookie(config["selectors"]["login"]["cookie_of_interest"]):
        driver.delete_all_cookies()

    driver.get(config["paths"]["login"])

    username_field = WebDriverWait(driver, config["explicit_wait"]).until(
        EC.presence_of_element_located((By.XPATH, config["selectors"]["login"]["username_field"]))
    )
    username_field.clear()
    username_field.send_keys(username)

    password_field = driver.find_element(By.XPATH, config["selectors"]["login"]["password_field"])
    password_field.clear()
    password_field.send_keys(password)

    submit = driver.find_element(By.XPATH, config["selectors"]["login"]["login_button"])
    submit.click()

    logger.info(f"Complete o captcha para {username}")

    start_time = time()
    while not driver.get_cookie(config["selectors"]["login"]["cookie_of_interest"]):
        sleep(0.5)
        if time() - start_time > config["explicit_wait"]:
            raise InsufficientAuth("Tempo esgotado aguardando o cookie de sessão")

    WebDriverWait(driver, config["explicit_wait"]).until(EC.url_changes(config["paths"]["login"]))

    return driver.get_cookies()


def get_username_and_password(login_info: tuple | dict):
    """
    Analisa a entrada em um nome de usuário e senha
    """
    if not isinstance(login_info, dict):
        return login_info[0], login_info[1]

    if "email" in login_info:
        return login_info["email"], login_info["password"]
    elif "username" in login_info:
        return login_info["username"], login_info["password"]

    raise InsufficientAuth()


def save_cookies(path: str, cookies: list[Cookie]) -> None:
    """
    Salva os cookies em um arquivo netscape
    """
    cookie_jar = cookiejar.MozillaCookieJar(path)
    cookie_jar.load()

    for cookie in cookies:
        cookie_jar.set_cookie(cookie_from_dict(cookie))

    cookie_jar.save()


class InsufficientAuth(Exception):
    """
    Autenticação insuficiente:

    > O TikTok usa cookies para rastrear a autenticação ou sessão do usuário.

    Ou:
        - Use um arquivo de cookies passado como argumento `cookies`
            - facilmente obtido usando https://github.com/kairi003/Get-cookies.txt-LOCALLY
        - Use uma lista de cookies passada como argumento `cookies_list`
            - pode ser obtida nas ferramentas de desenvolvedor do navegador em armazenamento -> cookies
            - apenas o cookie `sessionid` é necessário
    """

    def __init__(self, message: str | None = None):
        super().__init__(message or self.__doc__)