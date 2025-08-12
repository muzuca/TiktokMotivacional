"""
Módulo `tiktok_uploader` para fazer upload de vídeos no TikTok

Funções Principais
-----------------
upload_video : Faz o upload de um único vídeo no TikTok
upload_videos : Faz o upload de vários vídeos no TikTok
"""

import logging
import os
from os.path import abspath, exists
import time
import pytz
import datetime
import threading
import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

# ===== Selenium e WebDriver-Manager =====
from selenium import webdriver  # usado só por tipos; driver real vem do browsers.get_browser
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
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.edge.options import Options as EdgeOptions
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.firefox import GeckoDriverManager
from webdriver_manager.microsoft import EdgeChromiumDriverManager
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.edge.service import Service as EdgeService

# ===== App modules =====
from .auth import AuthBackend
from . import config

# Tenta importar get_browser do módulo browsers (selenium-wire).
try:
    from .browsers import get_browser  # preferencial
except ImportError:
    # Fallback com Selenium-Wire embutido
    from dotenv import load_dotenv
    from seleniumwire import webdriver as wire_webdriver  # type: ignore
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.firefox.options import Options as FirefoxOptions
    from selenium.webdriver.edge.options import Options as EdgeOptions

    def _mk_sw_opts(use_proxy: bool, host: str, port: str, user: str | None, pw: str | None):
        opts = {'request_storage': 'none', 'verify_ssl': False}
        if use_proxy:
            if user and pw:
                proxy_uri = f"http://{user}:{pw}@{host}:{port}"
            else:
                proxy_uri = f"http://{host}:{port}"
            opts['proxy'] = {'http': proxy_uri, 'https': proxy_uri, 'no_proxy': 'localhost,127.0.0.1'}
            logging.info("Selenium-Wire proxy configurado (fallback): %s", proxy_uri)
        return opts

    def get_browser(name="chrome", options=None, proxy=None, idioma='en', headless=False, *args, **kwargs):
        load_dotenv()
        proxy_host = os.getenv("PROXY_HOST")
        proxy_port = os.getenv("PROXY_PORT")
        proxy_user = os.getenv("PROXY_USER")
        proxy_pass = os.getenv("PROXY_PASS")
        use_proxy = (idioma == 'en') and proxy_host and proxy_port

        if name == "chrome":
            if options is None:
                options = ChromeOptions()
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

            sw_opts = _mk_sw_opts(bool(use_proxy), proxy_host or "", proxy_port or "", proxy_user, proxy_pass)
            driver = wire_webdriver.Chrome(
                service=ChromeService(ChromeDriverManager().install()),
                options=options,
                seleniumwire_options=sw_opts
            )
            try:
                driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                    "source": "try { Object.defineProperty(navigator,'webdriver',{get:()=>undefined}); } catch(e){}"
                })
            except Exception:
                pass
            return driver

        elif name == "firefox":
            if options is None:
                options = FirefoxOptions()
            if headless:
                options.add_argument("-headless")
            sw_opts = _mk_sw_opts(bool(use_proxy), proxy_host or "", proxy_port or "", proxy_user, proxy_pass)
            return wire_webdriver.Firefox(
                service=FirefoxService(GeckoDriverManager().install()),
                options=options,
                seleniumwire_options=sw_opts
            )

        elif name == "edge":
            if options is None:
                options = EdgeOptions()
            if headless:
                options.add_argument("--headless=new")
            sw_opts = _mk_sw_opts(bool(use_proxy), proxy_host or "", proxy_port or "", proxy_user, proxy_pass)
            return wire_webdriver.Edge(
                service=EdgeService(EdgeChromiumDriverManager().install()),
                options=options,
                seleniumwire_options=sw_opts
            )

        else:
            raise ValueError(f"Navegador {name} não suportado")

# Configuração do logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)
logging.getLogger("webdriver_manager").setLevel(logging.INFO)

from .utils import bold, green, red
# Mantido apenas como referência; não usamos extensão agora
# from .proxy_auth_extension.proxy_auth_extension import proxy_is_working

from .types import VideoDict, ProxyDict, Cookie
from typing import Any, Callable, Literal

# Configura sessão HTTP para desativar retries (para evitar engasgos durante o upload)
session = requests.Session()
retries = Retry(total=0)
session.mount("http://", HTTPAdapter(max_retries=retries))
session.mount("https://", HTTPAdapter(max_retries=retries))


def upload_video(
    filename: str,
    description: str | None = None,
    cookies: str = "",
    schedule: datetime.datetime | None = None,
    username: str = "",
    password: str = "",
    sessionid: str | None = None,
    cookies_list: list[Cookie] = [],
    cookies_str: str | None = None,
    proxy: ProxyDict | None = None,
    product_id: str | None = None,
    idioma='en',
    *args,
    **kwargs,
) -> list[VideoDict]:
    """
    Faz o upload de um único vídeo no TikTok.
    """
    auth = AuthBackend(
        username=username,
        password=password,
        cookies=cookies,
        cookies_list=cookies_list,
        cookies_str=cookies_str,
        sessionid=sessionid,
    )

    video_dict: VideoDict = {"path": filename}
    if description:
        video_dict["description"] = description
    if schedule:
        video_dict["schedule"] = schedule
    if product_id:
        video_dict["product_id"] = product_id

    return upload_videos(
        [video_dict],
        auth,
        proxy,
        idioma=idioma,
        *args,
        **kwargs,
    )


def upload_videos(
    videos: list[VideoDict],
    auth: AuthBackend,
    proxy: ProxyDict | None = None,
    browser: Literal["chrome", "safari", "chromium", "edge", "firefox"] = "chrome",
    browser_agent: WebDriver | None = None,
    on_complete: Callable[[VideoDict], None] | None = None,
    headless: bool = False,
    num_retries: int = 1,
    skip_split_window: bool = False,
    idioma='en',
    *args,
    **kwargs,
) -> list[VideoDict]:
    """
    Faz o upload de vários vídeos no TikTok.
    """
    videos = _convert_videos_dict(videos)  # type: ignore

    if videos and len(videos) > 1:
        logger.info("Fazendo upload de %d vídeos", len(videos))

    # (Opcional) teste rápido do proxy antes de abrir o browser
    if idioma == 'en':
        try:
            from proxy_check import quick_proxy_test
            info = quick_proxy_test()
            logger.info("Proxy OK. Resposta do IP: %s", info)
        except Exception as e:
            logger.warning("Falha no teste rápido do proxy (seguindo mesmo assim): %s", e)

    if not browser_agent:  # agente de navegador não foi fornecido
        logger.info("Criando uma instância de navegador %s %s", browser, "em modo headless" if headless else "")
        chrome_options = ChromeOptions()
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--disable-logging")
        chrome_options.add_argument("--log-level=3")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-logging"])
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("--disable-popup-blocking")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        if headless:
            chrome_options.add_argument("--headless=new")
            chrome_options.add_argument("--ignore-certificate-errors")

        # IMPORTANTE: não adicionar --proxy-server. Selenium-Wire injeta o proxy.
        driver = get_browser(browser, headless=headless, proxy=proxy, idioma=idioma, options=chrome_options, *args, **kwargs)
    else:
        logger.info("Usando agente de navegador definido pelo usuário")
        driver = browser_agent

    # Autenticação (cookies/session)
    driver = auth.authenticate_agent(driver)

    failed = []
    # faz upload de cada vídeo
    for video in videos:
        try:
            path = abspath(video.get("path", "."))
            description = video.get("description", "")
            schedule = video.get("schedule", None)
            product_id = video.get("product_id", None)

            logger.info("Postando %s%s", bold(video.get("path", "")), f"\n{' ' * 15}com descrição: {bold(description)}" if description else "")

            # O vídeo deve ser de um tipo suportado
            if not _check_valid_path(path):
                logger.warning("%s é inválido, pulando", path)
                failed.append(video)
                continue

            # Validar/agendar
            if schedule:
                timezone = pytz.UTC
                if schedule.tzinfo is None:
                    schedule = schedule.astimezone(timezone)
                elif (utc_offset := schedule.utcoffset()) is not None and int(utc_offset.total_seconds()) == 0:
                    schedule = timezone.localize(schedule)
                else:
                    logger.warning("%s é inválido, o horário de agendamento deve ser ingênuo ou ciente do fuso UTC, pulando", schedule)
                    failed.append(video)
                    continue

                valid_tiktok_minute_multiple = 5
                schedule = _get_valid_schedule_minute(schedule, valid_tiktok_minute_multiple)
                if not _check_valid_schedule(schedule):
                    logger.warning("%s é inválido, o horário de agendamento deve ser pelo menos 20 minutos no futuro e no máximo 10 dias, pulando", schedule)
                    failed.append(video)
                    continue

            complete_upload_form(driver, path, description, schedule, skip_split_window, product_id, num_retries, *args, **kwargs)
        except WebDriverException as e:
            logger.error("Falha ao fazer upload de %s devido a erro no WebDriver", path)
            logger.error("Detalhes: %s", str(e))
            failed.append(video)
        except Exception as e:
            logger.error("Falha ao fazer upload de %s", path)
            logger.error("Detalhes: %s", str(e))
            failed.append(video)

        if callable(on_complete):
            on_complete(video)

    return failed


def complete_upload_form(driver: WebDriver, path: str, description: str, schedule: datetime.datetime | None, skip_split_window: bool, product_id: str | None = None, num_retries: int = 1, headless: bool = False, *args, **kwargs) -> None:
    """
    Realiza o upload de cada vídeo
    """
    logger.info(f"Navegando para a página de upload: {config['paths']['upload']}")
    _go_to_upload(driver)

    upload_complete_event = threading.Event()

    def upload_video():
        logger.info("Fazendo upload do arquivo de vídeo")
        _set_video(driver, path=path, **kwargs)
        upload_complete_event.set()

    upload_thread = threading.Thread(target=upload_video)
    upload_thread.start()

    upload_complete_event.wait()

    logger.info("Definindo descrição")
    _set_description(driver, description)
    if schedule:
        logger.info("Definindo agendamento")
        _set_schedule_video(driver, schedule)
    if product_id:
        logger.info(f"Tentando adicionar link de produto para ID: {product_id}")
        _add_product_link(driver, product_id)

    time.sleep(20)
    logger.info("Clicando no botão de postagem")
    _post_video(driver)

    closed_successfully = False
    try:
        time.sleep(10)
        driver.quit()
        closed_successfully = True
        logger.info("Navegador fechado com sucesso após 10 segundos")
    except WebDriverException as e:
        logger.error("Falha ao fechar o navegador: %s", str(e))
    finally:
        if not closed_successfully:
            try:
                driver.quit()
                logger.info("Processo do navegador forçado a fechar")
            except WebDriverException as e:
                logger.warning("Falha ao forçar fechamento do navegador, ignorando: %s", str(e))


def _go_to_upload(driver: WebDriver) -> None:
    """ Navega para a página de upload. """
    if driver.current_url != config["paths"]["upload"]:
        driver.get(config["paths"]["upload"])
        logger.info(f"Navegou para: {driver.current_url}")
    else:
        _refresh_with_alert(driver)

    root_selector = EC.presence_of_element_located((By.ID, "root"))
    WebDriverWait(driver, config["explicit_wait"]).until(root_selector)
    driver.switch_to.default_content()


def _change_to_upload_iframe(driver: WebDriver) -> None:
    """ Alterna para o iframe da página de upload. """
    iframe_selector = EC.presence_of_element_located((By.XPATH, config["selectors"]["upload"]["iframe"]))
    iframe = WebDriverWait(driver, config["explicit_wait"]).until(iframe_selector)
    driver.switch_to.frame(iframe)


def _set_description(driver: WebDriver, description: str) -> None:
    """ Define a descrição do vídeo """
    if description is None:
        return

    description = description.encode("utf-8", "ignore").decode("utf-8")
    saved_description = description

    WebDriverWait(driver, config["implicit_wait"]).until(
        EC.presence_of_element_located((By.XPATH, config["selectors"]["upload"]["description"]))
    )

    desc = driver.find_element(By.XPATH, config["selectors"]["upload"]["description"])

    desc.click()
    WebDriverWait(driver, config["explicit_wait"]).until(lambda driver: desc.text != "")

    desc.send_keys(Keys.END)
    _clear(desc)
    WebDriverWait(driver, config["explicit_wait"]).until(lambda driver: desc.text == "")

    desc.click()
    time.sleep(1)

    try:
        words = description.split(" ")
        for word in words:
            if word and word[0] == "#":
                desc.send_keys(word)
                desc.send_keys(" " + Keys.BACKSPACE)
                WebDriverWait(driver, config["implicit_wait"]).until(
                    EC.presence_of_element_located((By.XPATH, config["selectors"]["upload"]["mention_box"]))
                )
                time.sleep(config["add_hashtag_wait"])
                desc.send_keys(Keys.ENTER)
            elif word and word[0] == "@":
                logger.info("- Adicionando Menção: %s", word)
                desc.send_keys(word)
                desc.send_keys(" ")
                time.sleep(1)
                desc.send_keys(Keys.BACKSPACE)

                WebDriverWait(driver, config["explicit_wait"]).until(
                    EC.presence_of_element_located((By.XPATH, config["selectors"]["upload"]["mention_box_user_id"]))
                )

                found = False
                waiting_interval = 0.5
                timeout = 5
                start_time = time.time()

                while not found and (time.time() - start_time < timeout):
                    user_id_elements = driver.find_elements(By.XPATH, config["selectors"]["upload"]["mention_box_user_id"])
                    time.sleep(1)

                    for i in range(len(user_id_elements)):
                        user_id_element = user_id_elements[i]
                        if user_id_element and user_id_element.is_enabled():
                            username = user_id_element.text.split(" ")[0]
                            if username.lower() == word[1:].lower():
                                found = True
                                logger.info("Usuário correspondente encontrado: Clicando no %s", username)
                                for j in range(i):
                                    desc.send_keys(Keys.DOWN)
                                desc.send_keys(Keys.ENTER)
                                break

                        if not found:
                            logger.info("Sem correspondência. Aguardando %.1f segundos...", waiting_interval)
                            time.sleep(waiting_interval)

            else:
                desc.send_keys((word or "") + " ")

    except Exception as exception:
        logger.error("Falha ao definir descrição: %s", str(exception))
        _clear(desc)
        desc.send_keys(saved_description)


def _clear(element) -> None:
    """ Limpa o texto do elemento (hack para o site do TikTok) """
    element.send_keys(2 * len(element.text) * Keys.BACKSPACE)


def _set_video(driver: WebDriver, path: str = "", num_retries: int = 3, **kwargs) -> None:
    """ Define o vídeo para upload """
    for _ in range(num_retries):
        try:
            driverWait = WebDriverWait(driver, config["explicit_wait"])
            upload_boxWait = EC.presence_of_element_located((By.XPATH, config["selectors"]["upload"]["upload_video"]))
            driverWait.until(upload_boxWait)
            upload_box = driver.find_element(By.XPATH, config["selectors"]["upload"]["upload_video"])
            upload_box.send_keys(path)

            process_confirmation = EC.presence_of_element_located((By.XPATH, config["selectors"]["upload"]["process_confirmation"]))
            WebDriverWait(driver, config["explicit_wait"]).until(process_confirmation)
            return
        except TimeoutException as exception:
            logger.warning("Ocorreu TimeoutException: %s", str(exception))
        except Exception as exception:
            logger.error("Erro ao definir vídeo: %s", str(exception))
            raise FailedToUpload(exception)


def _remove_cookies_window(driver) -> None:
    """ Remove a janela de cookies se estiver aberta """
    logger.info("Removendo janela de cookies")
    cookies_banner = WebDriverWait(driver, config["implicit_wait"]).until(
        EC.presence_of_element_located((By.TAG_NAME, config["selectors"]["upload"]["cookies_banner"]["banner"]))
    )

    item = WebDriverWait(driver, config["implicit_wait"]).until(
        EC.visibility_of(cookies_banner.shadow_root.find_element(By.CSS_SELECTOR, config["selectors"]["upload"]["cookies_banner"]["button"]))
    )

    decline_button = WebDriverWait(driver, config["implicit_wait"]).until(
        EC.element_to_be_clickable(item.find_elements(By.TAG_NAME, "button")[0])
    )

    decline_button.click()


def _set_interactivity(driver: WebDriver, comment: bool = True, stitch: bool = True, duet: bool = True, *args, **kwargs) -> None:
    """ Define as configurações de interatividade do vídeo """
    try:
        logger.info("Definindo configurações de interatividade")

        comment_box = driver.find_element(By.XPATH, config["selectors"]["upload"]["comment"])
        stitch_box = driver.find_element(By.XPATH, config["selectors"]["upload"]["stitch"])
        duet_box = driver.find_element(By.XPATH, config["selectors"]["upload"]["duet"])

        if comment ^ comment_box.is_selected():
            comment_box.click()

        if stitch ^ stitch_box.is_selected():
            stitch_box.click()

        if duet ^ duet_box.is_selected():
            duet_box.click()

    except NoSuchElementException as e:
        logger.warning("Elementos de interatividade não encontrados: %s. Ignorando configuração.", str(e))
    except Exception as e:
        logger.error("Falha ao definir configurações de interatividade: %s", str(e))


def _set_schedule_video(driver: WebDriver, schedule: datetime.datetime) -> None:
    """ Define o agendamento do vídeo """
    logger.info("Definindo agendamento")

    driver_timezone = __get_driver_timezone(driver)
    schedule = schedule.astimezone(driver_timezone)

    month = schedule.month
    day = schedule.day
    hour = schedule.hour
    minute = schedule.minute

    try:
        switch = driver.find_element(By.XPATH, config["selectors"]["schedule"]["switch"])
        switch.click()
        __date_picker(driver, month, day)
        __time_picker(driver, hour, minute)
    except Exception as e:
        logger.error("Falha ao definir agendamento: %s", str(e))
        raise FailedToUpload()


def __date_picker(driver: WebDriver, month: int, day: int) -> None:
    logger.info("Selecionando data")

    condition = EC.presence_of_element_located((By.XPATH, config["selectors"]["schedule"]["date_picker"]))
    date_picker = WebDriverWait(driver, config["implicit_wait"]).until(condition)
    date_picker.click()

    condition = EC.presence_of_element_located((By.XPATH, config["selectors"]["schedule"]["calendar"]))
    WebDriverWait(driver, config["implicit_wait"]).until(condition)

    calendar_month = driver.find_element(By.XPATH, config["selectors"]["schedule"]["calendar_month"]).text
    n_calendar_month = datetime.datetime.strptime(calendar_month, "%B").month
    if n_calendar_month != month:
        if n_calendar_month < month:
            arrow = driver.find_elements(By.XPATH, config["selectors"]["schedule"]["calendar_arrows"])[-1]
        else:
            arrow = driver.find_elements(By.XPATH, config["selectors"]["schedule"]["calendar_arrows"])[0]
        arrow.click()
    valid_days = driver.find_elements(By.XPATH, config["selectors"]["schedule"]["calendar_valid_days"])

    day_to_click = None
    for day_option in valid_days:
        if day_option.text.isdigit() and int(day_option.text) == day:
            day_to_click = day_option
            break
    if day_to_click:
        day_to_click.click()
    else:
        raise Exception("Dia não encontrado no calendário")

    __verify_date_picked_is_correct(driver, month, day)


def __verify_date_picked_is_correct(driver: WebDriver, month: int, day: int) -> None:
    date_selected = driver.find_element(By.XPATH, config["selectors"]["schedule"]["date_picker"]).text
    parts = date_selected.split("-")
    date_selected_month = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else -1
    date_selected_day = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else -1

    if date_selected_month == month and date_selected_day == day:
        logger.info("Data selecionada corretamente")
    else:
        msg = f"Algo deu errado com o seletor de data, esperado {month}-{day} mas recebido {date_selected_month}-{date_selected_day}"
        logger.error(msg)
        raise Exception(msg)


def __time_picker(driver: WebDriver, hour: int, minute: int) -> None:
    logger.info("Selecionando horário")

    condition = EC.presence_of_element_located((By.XPATH, config["selectors"]["schedule"]["time_picker"]))
    time_picker = WebDriverWait(driver, config["implicit_wait"]).until(condition)
    time_picker.click()

    condition = EC.presence_of_element_located((By.XPATH, config["selectors"]["schedule"]["time_picker_container"]))
    WebDriverWait(driver, config["implicit_wait"]).until(condition)

    hour_options = driver.find_elements(By.XPATH, config["selectors"]["schedule"]["timepicker_hours"])
    minute_options = driver.find_elements(By.XPATH, config["selectors"]["schedule"]["timepicker_minutes"])

    hour_to_click = hour_options[hour]
    minute_option_correct_index = int(minute / 5)
    minute_to_click = minute_options[minute_option_correct_index]

    time.sleep(1)
    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", hour_to_click)
    time.sleep(1)
    hour_to_click.click()

    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", minute_to_click)
    time.sleep(2)
    minute_to_click.click()

    time_picker.click()
    time.sleep(0.5)
    __verify_time_picked_is_correct(driver, hour, minute)


def __verify_time_picked_is_correct(driver: WebDriver, hour: int, minute: int) -> None:
    time_selected = driver.find_element(By.XPATH, config["selectors"]["schedule"]["time_picker_text"]).text
    parts = time_selected.split(":")
    time_selected_hour = int(parts[0]) if parts and parts[0].isdigit() else -1
    time_selected_minute = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else -1

    if time_selected_hour == hour and time_selected_minute == minute:
        logger.info("Horário selecionado corretamente")
    else:
        msg = f"Algo deu errado com o seletor de horário, esperado {hour:02d}:{minute:02d} mas recebido {time_selected_hour:02d}:{time_selected_minute:02d}"
        logger.error(msg)
        raise Exception(msg)


def _post_video(driver: WebDriver) -> None:
    """ Clica no botão de postagem """
    logger.info("Clicando no botão de postagem")

    try:
        post = WebDriverWait(driver, config["uploading_wait"]).until(
            lambda d: (el := d.find_element(By.XPATH, config["selectors"]["upload"]["post"])) and
                      el.get_attribute("data-disabled") == "false" and el
        )
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", post)
        post.click()
        time.sleep(5)
    except ElementClickInterceptedException:
        logger.info("Tentando clicar no botão novamente (fallback JS)")
        driver.execute_script('document.querySelector(".TUXButton--primary").click()')
        time.sleep(5)
    except WebDriverException as e:
        logger.error("Erro ao clicar no botão de postagem: %s", str(e))
        raise

    try:
        post_confirmation = EC.presence_of_element_located((By.XPATH, config["selectors"]["upload"]["post_confirmation"]))
        WebDriverWait(driver, 20).until(post_confirmation)
        logger.info("Vídeo postado com sucesso")
    except TimeoutException:
        logger.warning("Confirmação de postagem não encontrada em 20 segundos. Prosseguindo assumindo sucesso.")
    except WebDriverException as e:
        logger.error("Falha ao confirmar postagem: %s", str(e))
        raise FailedToUpload("Postagem não confirmada devido a erro no WebDriver")


def _check_valid_path(path: str) -> bool:
    """ Retorna se o tipo de arquivo é suportado pelo TikTok """
    return exists(path) and path.split(".")[-1] in config["supported_file_types"]


def _get_valid_schedule_minute(schedule: datetime.datetime, valid_multiple) -> datetime.datetime:
    """ Ajusta minuto para múltiplos aceitos pelo TikTok """
    if _is_valid_schedule_minute(schedule.minute, valid_multiple):
        return schedule
    return _set_valid_schedule_minute(schedule, valid_multiple)


def _is_valid_schedule_minute(minute: int, valid_multiple) -> bool:
    return minute % valid_multiple == 0


def _set_valid_schedule_minute(schedule: datetime.datetime, valid_multiple: int) -> datetime.datetime:
    minute = schedule.minute
    remainder = minute % valid_multiple
    integers_to_valid_multiple = valid_multiple - remainder
    return schedule + datetime.timedelta(minutes=integers_to_valid_multiple)


def _check_valid_schedule(schedule: datetime.datetime) -> bool:
    """ Retorna se o agendamento é suportado pelo TikTok """
    valid_tiktok_minute_multiple = 5
    margin_to_complete_upload_form = 5

    datetime_utc_now = pytz.UTC.localize(datetime.datetime.utcnow())
    min_datetime_tiktok_valid = datetime_utc_now + datetime.timedelta(minutes=15 + margin_to_complete_upload_form)
    max_datetime_tiktok_valid = datetime_utc_now + datetime.timedelta(days=10)
    return min_datetime_tiktok_valid <= schedule <= max_datetime_tiktok_valid and _is_valid_schedule_minute(schedule.minute, valid_tiktok_minute_multiple)


def _get_splice_index(nearest_mention: int, nearest_hashtag: int, description: str) -> int:
    """ Retorna o índice para dividir a descrição """
    if nearest_mention == -1 and nearest_hashtag == -1:
        return len(description)
    elif nearest_hashtag == -1:
        return nearest_mention
    elif nearest_mention == -1:
        return nearest_hashtag
    return min(nearest_mention, nearest_hashtag)


def _convert_videos_dict(videos_list_of_dictionaries: list[dict[str, Any]]) -> list[VideoDict]:
    """ Converte lista de dicionários de vídeos para formato interno. """
    if not videos_list_of_dictionaries:
        raise RuntimeError("Nenhum vídeo para upload")

    valid_path = config["valid_path_names"]
    valid_description = config["valid_descriptions"]
    correct_path = valid_path[0]
    correct_description = valid_description[0]

    def intersection(lst1, lst2):
        return list(set(lst1) & set(lst2))

    return_list: list[VideoDict] = []
    for elem in videos_list_of_dictionaries:
        elem = {k.strip().lower(): v for k, v in elem.items()}
        keys = elem.keys()
        path_intersection = intersection(valid_path, keys)
        description_intersection = intersection(valid_description, keys)

        if path_intersection:
            path = elem[path_intersection.pop()]
            if not _check_valid_path(path):
                raise RuntimeError("Caminho inválido: " + path)
            elem[correct_path] = path
        else:
            for _, value in elem.items():
                if _check_valid_path(value):
                    elem[correct_path] = value
                    break
            else:
                raise RuntimeError("Caminho não encontrado no dicionário: " + str(elem))

        if description_intersection:
            elem[correct_description] = elem[description_intersection.pop()]
        else:
            for _, value in elem.items():
                if not _check_valid_path(value):
                    elem[correct_description] = value
                    break
            else:
                elem[correct_description] = ""
        return_list.append(elem)
    return return_list


def __get_driver_timezone(driver: WebDriver) -> Any:
    """ Retorna o fuso horário do driver """
    timezone_str = driver.execute_script("return Intl.DateTimeFormat().resolvedOptions().timeZone")
    return pytz.timezone(timezone_str)


def _refresh_with_alert(driver: WebDriver) -> None:
    try:
        driver.refresh()
        WebDriverWait(driver, config["explicit_wait"]).until(EC.alert_is_present())
        driver.switch_to.alert.accept()
    except Exception as e:
        logger.warning("Exceção ao atualizar alerta: %s", str(e))


class DescriptionTooLong(Exception):
    """ Descrição excede o máximo suportado pelo uploader web do TikTok """
    def __init__(self, message: str | None = None):
        super().__init__(message or self.__doc__)


class FailedToUpload(Exception):
    """ Um vídeo falhou ao fazer upload """
    def __init__(self, message=None):
        super().__init__(message or self.__doc__)


def _add_product_link(driver: WebDriver, product_id: str) -> None:
    """ Adiciona o link do produto ao vídeo usando o ID fornecido. """
    logger.info(f"Tentando adicionar link de produto para ID: {product_id}")
    try:
        wait = WebDriverWait(driver, 20)

        add_link_button_xpath = "//button[contains(@class, 'Button__root') and contains(., 'Adicionar')]"
        add_link_button = wait.until(EC.element_to_be_clickable((By.XPATH, add_link_button_xpath)))
        add_link_button.click()
        logger.info("Clicou no botão 'Adicionar Link de Produto'")
        time.sleep(1)

        try:
            first_next_button_xpath = "//button[contains(@class, 'TUXButton--primary') and .//div[text()='Próximo']]"
            first_next_button = wait.until(EC.element_to_be_clickable((By.XPATH, first_next_button_xpath)))
            first_next_button.click()
            logger.info("Clicou no primeiro botão 'Próximo' no modal")
            time.sleep(1)
        except TimeoutException:
            logger.info("Botão 'Próximo' inicial não encontrado ou não necessário, prosseguindo...")

        search_input_xpath = "//input[@placeholder='Pesquisar produtos']"
        search_input = wait.until(EC.visibility_of_element_located((By.XPATH, search_input_xpath)))
        search_input.clear()
        search_input.send_keys(product_id)
        search_input.send_keys(Keys.RETURN)
        logger.info(f"Inseriu o ID do produto '{product_id}' e pressionou Enter")
        time.sleep(3)

        product_radio_xpath = f"//tr[.//span[contains(text(), '{product_id}')] or .//div[contains(text(), '{product_id}')]]//input[@type='radio' and contains(@class, 'TUXRadioStandalone-input')]"
        logger.info(f"Procurando botão de rádio com XPath: {product_radio_xpath}")
        product_radio = wait.until(EC.element_to_be_clickable((By.XPATH, product_radio_xpath)))
        driver.execute_script("arguments[0].click();", product_radio)
        logger.info(f"Selecionou botão de rádio do produto para ID: {product_id}")
        time.sleep(1)

        second_next_button_xpath = "//button[contains(@class, 'TUXButton--primary') and .//div[text()='Próximo']]"
        second_next_button = wait.until(EC.element_to_be_clickable((By.XPATH, second_next_button_xpath)))
        second_next_button.click()
        logger.info("Clicou no segundo botão 'Próximo'")
        time.sleep(1)

        final_add_button_xpath = "//button[contains(@class, 'TUXButton--primary') and .//div[text()='Adicionar']]"
        final_add_button = wait.until(EC.element_to_be_clickable((By.XPATH, final_add_button_xpath)))
        final_add_button.click()
        logger.info("Clicou no botão final 'Adicionar'. O link do produto deve estar adicionado")

        wait.until(EC.invisibility_of_element_located((By.XPATH, final_add_button_xpath)))
        logger.info("Modal de link de produto fechado")

    except TimeoutException:
        logger.error("Erro: Tempo esgotado ao esperar por elemento durante a adição do link de produto. O XPath pode estar errado ou o elemento não apareceu")
        logger.info(f"Aviso: Falha ao adicionar link de produto {product_id} devido a tempo esgotado. Continuando o upload sem link")
    except NoSuchElementException:
        logger.error("Erro: Não foi possível encontrar elemento durante a adição do link de produto. O XPath pode estar errado")
        logger.info(f"Aviso: Falha ao adicionar link de produto {product_id} porque um elemento não foi encontrado. Continuando o upload sem link")
    except Exception as e:
        logger.error(f"Ocorreu um erro inesperado ao adicionar link de produto: {e}")
        logger.info(f"Aviso: Ocorreu um erro inesperado ao adicionar link de produto {product_id}. Continuando o upload sem link")
