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
import webdriver_manager

from selenium import webdriver
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
from selenium.webdriver.common.proxy import Proxy, ProxyType
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.firefox import GeckoDriverManager
from webdriver_manager.microsoft import EdgeChromiumDriverManager
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.edge.service import Service as EdgeService

from .auth import AuthBackend
from . import config
try:
    from .browsers import get_browser  # Tenta importar get_browser do módulo browsers
except ImportError:
    # Definição mínima de get_browser como fallback
    def get_browser(name="chrome", options=None, *args, **kwargs):
        if name == "chrome":
            options = options or ChromeOptions()
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-logging")
            options.add_argument("--log-level=3")
            options.add_experimental_option("excludeSwitches", ["enable-logging"])
            return webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=options)
        elif name == "firefox":
            options = options or FirefoxOptions()
            return webdriver.Firefox(service=FirefoxService(GeckoDriverManager().install()), options=options)
        elif name == "edge":
            options = options or EdgeOptions()
            return webdriver.Edge(service=EdgeService(EdgeChromiumDriverManager().install()), options=options)
        else:
            raise ValueError(f"Navegador {name} não suportado")

# Configuração do logging com timestamps, como em imagem.py
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)
logging.getLogger("webdriver_manager").setLevel(logging.INFO)  # Força o nível INFO para webdriver_manager

from .utils import bold, green, red
from .proxy_auth_extension.proxy_auth_extension import proxy_is_working

from .types import VideoDict, ProxyDict, Cookie
from typing import Any, Callable, Literal

# Configura sessão HTTP para desativar retries
session = requests.Session()
retries = Retry(total=0)  # Desativa retries
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
    *args,
    **kwargs,
) -> list[VideoDict]:
    """
    Faz o upload de um único vídeo no TikTok.

    Considere usar `upload_videos` se estiver usando vários vídeos

    Parâmetros
    ----------
    filename : str
        O caminho para o vídeo a ser enviado
    description : str
        A descrição a ser definida para o vídeo
    schedule: datetime.datetime
        O horário para agendar o vídeo, deve ser ingênuo ou ciente do fuso horário UTC, se ingênuo será ciente com fuso UTC
    cookies : str
        Os cookies a serem usados para o upload
    sessionid: str
        O `sessionid` é o único cookie necessário para o upload,
            mas é recomendado usar todos os cookies para evitar detecção
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
    *args,
    **kwargs,
) -> list[VideoDict]:
    """
    Faz o upload de vários vídeos no TikTok

    Parâmetros
    ----------
    videos : list
        Uma lista de dicionários contendo o caminho do vídeo ('path') e descrição ('description')
    proxy: dict
        Um dicionário contendo o usuário, senha, host e porta do proxy
    browser : str
        O navegador a ser usado para o upload
    browser_agent : selenium.webdriver
        Um objeto selenium webdriver a ser usado para o upload
    on_complete : function
        Uma função a ser chamada quando o upload for concluído
    headless : bool
        Se o navegador deve ser executado em modo headless ou não
    num_retries : int
        O número de tentativas a serem feitas se o upload falhar
    options : SeleniumOptions
        As opções a serem passadas para o navegador -> configurações de privacidade personalizadas, etc.
    *args :
        Argumentos adicionais a serem passados para a função de upload
    **kwargs :
        Argumentos de palavra-chave adicionais a serem passados para a função de upload

    Retorna
    -------
    failed : list
        Uma lista de vídeos que falharam no upload
    """
    videos = _convert_videos_dict(videos)  # type: ignore

    if videos and len(videos) > 1:
        logger.info("Fazendo upload de %d vídeos", len(videos))

    if not browser_agent:  # agente de navegador especificado pelo usuário
        logger.info("Criando uma instância de navegador %s %s", browser, "em modo headless" if headless else "")
        # Configurações para suprimir logs e aumentar estabilidade do ChromeDriver
        chrome_options = ChromeOptions()
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--disable-logging")
        chrome_options.add_argument("--log-level=3")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-logging"])
        driver = get_browser(browser, headless=headless, proxy=proxy, options=chrome_options, *args, **kwargs)
    else:
        logger.info("Usando agente de navegador definido pelo usuário")
        driver = browser_agent
    if proxy:
        if proxy_is_working(driver, proxy["host"]):
            logger.info("Proxy está funcionando")
        else:
            logger.error("Proxy não está funcionando")
            driver.quit()
            raise Exception("Proxy não está funcionando")
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

            # O vídeo deve ter um horário válido para o agendador do TikTok
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

        if on_complete is callable:
            on_complete(video)

    return failed


def complete_upload_form(driver: WebDriver, path: str, description: str, schedule: datetime.datetime | None, skip_split_window: bool, product_id: str | None = None, num_retries: int = 1, headless: bool = False, *args, **kwargs) -> None:
    """
    Realiza o upload de cada vídeo

    Parâmetros
    ----------
    driver : selenium.webdriver
        O selenium webdriver a ser usado para o upload
    path : str
        O caminho para o vídeo a ser enviado
    """
    logger.info("Navegando para a página de upload")
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
        time.sleep(10)  # Aumentado para 10 segundos para garantir conclusão
        driver.quit()
        closed_successfully = True
        logger.info("Navegador fechado com sucesso após 10 segundos")
    except WebDriverException as e:
        logger.error("Falha ao fechar o navegador: %s", str(e))
    finally:
        if not closed_successfully and driver.service.process:
            try:
                driver.service.stop()
                driver.quit()
                logger.info("Processo do navegador forçado a fechar")
            except WebDriverException as e:
                logger.warning("Falha ao forçar fechamento do navegador, ignorando: %s", str(e))


def _go_to_upload(driver: WebDriver) -> None:
    """
    Navega para a página de upload, alterna para o iframe e aguarda o carregamento

    Parâmetros
    ----------
    driver : selenium.webdriver
    """
    if driver.current_url != config["paths"]["upload"]:
        driver.get(config["paths"]["upload"])
    else:
        _refresh_with_alert(driver)

    root_selector = EC.presence_of_element_located((By.ID, "root"))
    WebDriverWait(driver, config["explicit_wait"]).until(root_selector)

    driver.switch_to.default_content()


def _change_to_upload_iframe(driver: WebDriver) -> None:
    """
    Alterna para o iframe da página de upload

    Parâmetros
    ----------
    driver : selenium.webdriver
    """
    iframe_selector = EC.presence_of_element_located((By.XPATH, config["selectors"]["upload"]["iframe"]))
    iframe = WebDriverWait(driver, config["explicit_wait"]).until(iframe_selector)
    driver.switch_to.frame(iframe)


def _set_description(driver: WebDriver, description: str) -> None:
    """
    Define a descrição do vídeo

    Parâmetros
    ----------
    driver : selenium.webdriver
    description : str
        A descrição a ser definida
    """
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
            if word[0] == "#":
                desc.send_keys(word)
                desc.send_keys(" " + Keys.BACKSPACE)
                WebDriverWait(driver, config["implicit_wait"]).until(
                    EC.presence_of_element_located((By.XPATH, config["selectors"]["upload"]["mention_box"]))
                )
                time.sleep(config["add_hashtag_wait"])
                desc.send_keys(Keys.ENTER)
            elif word[0] == "@":
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
                desc.send_keys(word + " ")

    except Exception as exception:
        logger.error("Falha ao definir descrição: %s", str(exception))
        _clear(desc)
        desc.send_keys(saved_description)


def _clear(element) -> None:
    """
    Limpa o texto do elemento (um problema com o site do TikTok ao automatizar)

    Parâmetros
    ----------
    element
        A caixa de texto a ser limpa
    """
    element.send_keys(2 * len(element.text) * Keys.BACKSPACE)


def _set_video(driver: WebDriver, path: str = "", num_retries: int = 3, **kwargs) -> None:
    """
    Define o vídeo para upload

    Parâmetros
    ----------
    driver : selenium.webdriver
    path : str
        O caminho para o vídeo a ser enviado
    num_retries : número de tentativas (pode ocasionalmente falhar)
    """
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
    """
    Remove a janela de cookies se estiver aberta

    Parâmetros
    ----------
    driver : selenium.webdriver
    """
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
    """
    Define as configurações de interatividade do vídeo

    Parâmetros
    ----------
    driver : selenium.webdriver
    comment : bool
        Se deve permitir comentários ou não
    stitch : bool
        Se deve permitir edição ou não
    duet : bool
        Se deve permitir duetos ou não

    Nota: Essa função tenta configurar opções de interatividade (comentários, duetos, edição) no TikTok.
    Se os elementos não forem encontrados (como indicado pelo erro 'Elementos de interatividade não encontrados'),
    pode ser que os seletores em config['selectors']['upload'] estejam desatualizados ou que essas opções
    não estejam visíveis na interface atual. Esta função foi desativada no script por padrão.
    """
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
    """
    Define o agendamento do vídeo

    Parâmetros
    ----------
    driver : selenium.webdriver
    schedule : datetime.datetime
        O horário a ser definido
    """
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
        if int(day_option.text) == day:
            day_to_click = day_option
            break
    if day_to_click:
        day_to_click.click()
    else:
        raise Exception("Dia não encontrado no calendário")

    __verify_date_picked_is_correct(driver, month, day)


def __verify_date_picked_is_correct(driver: WebDriver, month: int, day: int) -> None:
    date_selected = driver.find_element(By.XPATH, config["selectors"]["schedule"]["date_picker"]).text
    date_selected_month = int(date_selected.split("-")[1])
    date_selected_day = int(date_selected.split("-")[2])

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
    time_selected_hour = int(time_selected.split(":")[0])
    time_selected_minute = int(time_selected.split(":")[1])

    if time_selected_hour == hour and time_selected_minute == minute:
        logger.info("Horário selecionado corretamente")
    else:
        msg = f"Algo deu errado com o seletor de horário, esperado {hour:02d}:{minute:02d} mas recebido {time_selected_hour:02d}:{time_selected_minute:02d}"
        logger.error(msg)
        raise Exception(msg)


def _post_video(driver: WebDriver) -> None:
    """
    Posta o vídeo clicando no botão de postagem

    Parâmetros
    ----------
    driver : selenium.webdriver
    """
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
        logger.info("Tentando clicar no botão novamente")
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
    """
    Retorna se o tipo de arquivo é suportado pelo TikTok
    """
    return exists(path) and path.split(".")[-1] in config["supported_file_types"]


def _get_valid_schedule_minute(schedule: datetime.datetime, valid_multiple) -> datetime.datetime:
    """
    Retorna um datetime.datetime com minuto válido para o TikTok
    """
    if _is_valid_schedule_minute(schedule.minute, valid_multiple):
        return schedule
    return _set_valid_schedule_minute(schedule, valid_multiple)


def _is_valid_schedule_minute(minute: int, valid_multiple) -> bool:
    return minute % valid_multiple == 0


def _set_valid_schedule_minute(schedule: datetime.datetime, valid_multiple: int) -> datetime.datetime:
    minute = schedule.minute
    remainder = minute % valid_multiple
    integers_to_valid_multiple = 5 - remainder
    return schedule + datetime.timedelta(minutes=integers_to_valid_multiple)


def _check_valid_schedule(schedule: datetime.datetime) -> bool:
    """
    Retorna se o agendamento é suportado pelo TikTok
    """
    valid_tiktok_minute_multiple = 5
    margin_to_complete_upload_form = 5

    datetime_utc_now = pytz.UTC.localize(datetime.datetime.utcnow())
    min_datetime_tiktok_valid = datetime_utc_now + datetime.timedelta(minutes=15 + margin_to_complete_upload_form)
    max_datetime_tiktok_valid = datetime_utc_now + datetime.timedelta(days=10)
    return min_datetime_tiktok_valid <= schedule <= max_datetime_tiktok_valid and _is_valid_schedule_minute(schedule.minute, valid_tiktok_minute_multiple)


def _get_splice_index(nearest_mention: int, nearest_hashtag: int, description: str) -> int:
    """
    Retorna o índice para dividir a descrição
    """
    if nearest_mention == -1 and nearest_hashtag == -1:
        return len(description)
    elif nearest_hashtag == -1:
        return nearest_mention
    elif nearest_mention == -1:
        return nearest_hashtag
    return min(nearest_mention, nearest_hashtag)


def _convert_videos_dict(videos_list_of_dictionaries: list[dict[str, Any]]) -> list[VideoDict]:
    """
    Recebe um dicionário de vídeos e o converte.
    """
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
    """
    Retorna o fuso horário do driver
    """
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
    """
    Uma descrição de vídeo mais longa que o máximo permitido pelo uploader do site do TikTok (não app)
    """
    def __init__(self, message: str | None = None):
        super().__init__(message or self.__doc__)


class FailedToUpload(Exception):
    """
    Um vídeo falhou ao fazer upload
    """
    def __init__(self, message=None):
        super().__init__(message or self.__doc__)


def _add_product_link(driver: WebDriver, product_id: str) -> None:
    """
    Adiciona o link do produto ao vídeo usando o ID fornecido.
    """
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