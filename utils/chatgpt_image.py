# utils/chatgpt_image.py
import os
import time
import shutil
import glob
import logging
import json
from typing import Optional

from PIL import Image
import undetected_chromedriver as uc
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.common.exceptions import (
    TimeoutException,
    WebDriverException,
    InvalidCookieDomainException,
    NoSuchWindowException,
)

logger = logging.getLogger(__name__)

# ===================== Config via ENV =====================
HEADLESS = os.getenv("CHATGPT_HEADLESS", "1").strip().lower() in ("1", "true", "yes", "on")
PAGELOAD_TIMEOUT = int(float(os.getenv("CHATGPT_PAGELOAD_TIMEOUT", "60")))
GENERATION_TIMEOUT = int(float(os.getenv("CHATGPT_IMG_MAX_WAIT_SEC", "240")))  # até aparecer o botão de download
FILE_DOWNLOAD_TIMEOUT = int(float(os.getenv("CHATGPT_FILE_DOWNLOAD_TIMEOUT", "180")))
WIN_W = int(float(os.getenv("CHATGPT_WIN_W", "800")))
WIN_H = int(float(os.getenv("CHATGPT_WIN_H", "600")))

# ===================== Utilidades =====================
def _driver_alive(driver) -> bool:
    try:
        return bool(getattr(driver, "session_id", None))
    except Exception:
        return False

def _safe_screenshot(driver, path: str) -> bool:
    try:
        if not _driver_alive(driver):
            return False
        return driver.save_screenshot(path)
    except WebDriverException:
        return False
    except Exception:
        return False

def _wait_for_file_download(download_dir: str, timeout: int = FILE_DOWNLOAD_TIMEOUT) -> str:
    """Aguarda um novo arquivo .png/.webp aparecer e terminar (sem .crdownload/.tmp)."""
    logger.info("Aguardando download da imagem...")
    end_time = time.time() + timeout
    initial_files = set(glob.glob(os.path.join(download_dir, "*")))

    while time.time() < end_time:
        current_files = set(glob.glob(os.path.join(download_dir, "*")))
        new_files = current_files - initial_files

        if new_files:
            valid = [
                f for f in new_files
                if not f.endswith((".tmp", ".crdownload", ".part"))
            ]
            if valid:
                latest = max(valid, key=os.path.getmtime)
                # pequena folga para flush do Chrome
                time.sleep(2.5)
                if os.path.exists(latest) and os.path.getsize(latest) > 1024:
                    logger.info(f"Download concluído: {os.path.basename(latest)}")
                    return latest
        time.sleep(0.75)
    raise TimeoutError("Tempo limite excedido esperando o download da imagem do ChatGPT.")

def _inject_cookies(driver, cookies_path: str) -> int:
    """Carrega cookies (JSON) e injeta na sessão atual do domínio chatgpt.com."""
    if not os.path.exists(cookies_path) or os.path.getsize(cookies_path) < 10:
        raise FileNotFoundError(
            f"Arquivo de cookies '{cookies_path}' não encontrado ou vazio. "
            "Execute 'python setup_cookies.py' para criá-lo."
        )

    try:
        with open(cookies_path, "r", encoding="utf-8") as f:
            cookies = json.load(f)
    except json.JSONDecodeError:
        raise ValueError(
            f"Formato do arquivo '{cookies_path}' inválido (JSON). "
            "Execute 'python setup_cookies.py' para gerar corretamente."
        )

    ok = 0
    skipped = 0
    for cookie in cookies:
        try:
            c = dict(cookie)
            # Alguns campos podem quebrar dependendo da versão do Chrome
            if "sameSite" in c:
                c.pop("sameSite", None)
            # Domain seguro
            domain = c.get("domain", "")
            if "chatgpt.com" not in domain:
                skipped += 1
                continue
            driver.add_cookie(c)
            ok += 1
        except InvalidCookieDomainException:
            skipped += 1
        except Exception:
            skipped += 1
    logger.info(f"Cookies injetados: {ok} (ignorados: {skipped})")
    return ok

def _find_prompt_input(driver, max_wait: int = 45):
    """
    Tenta localizar o campo de prompt (textarea ou contenteditable div).
    Retorna o WebElement ou lança TimeoutException.
    """
    wait = WebDriverWait(driver, max_wait)

    locators = [
        (By.CSS_SELECTOR, "textarea#prompt-textarea"),  # padrão atual
        (By.CSS_SELECTOR, "div[contenteditable='true'][data-testid='prompt-textarea']"),
        (By.CSS_SELECTOR, "div[contenteditable='true'][role='textbox']"),
        (By.CSS_SELECTOR, "div[contenteditable='true']"),
    ]

    last_err = None
    for by, sel in locators:
        try:
            el = wait.until(EC.presence_of_element_located((by, sel)))
            return el
        except Exception as e:
            last_err = e

    raise TimeoutException(f"Não encontrei o campo de prompt em {max_wait}s. Último erro: {last_err}")

def _type_prompt(driver, element, text: str) -> None:
    """Tenta enviar o prompt via send_keys; se falhar, usa JS como fallback."""
    try:
        element.click()
    except Exception:
        pass

    sent = False
    try:
        element.clear()  # funciona em textarea
        element.send_keys(text)
        sent = True
    except Exception:
        # Pode ser um DIV contenteditable — tenta apenas send_keys
        try:
            element.send_keys(text)
            sent = True
        except Exception:
            sent = False

    if not sent:
        # fallback via JS — insere o texto de forma direta
        try:
            driver.execute_script(
                "if(arguments[0].tagName==='TEXTAREA'){arguments[0].value=arguments[1];}"
                " else {arguments[0].innerText=arguments[1];}",
                element, text
            )
        except Exception:
            # último fallback: força o foco e emite teclas
            try:
                driver.execute_script("arguments[0].focus();", element)
                element.send_keys(text)
            except Exception as e:
                raise WebDriverException(f"Falha ao inserir o prompt no campo: {e}")

def _click_send(driver, max_wait: int = 30) -> None:
    wait = WebDriverWait(driver, max_wait)
    # Variações de seletor do botão de enviar
    locators = [
        (By.CSS_SELECTOR, "button[data-testid='send-button']"),
        (By.CSS_SELECTOR, "button[aria-label='Send']"),
        (By.CSS_SELECTOR, "button[aria-label*='Enviar']"),
    ]
    last_err = None
    for by, sel in locators:
        try:
            btn = wait.until(EC.element_to_be_clickable((by, sel)))
            btn.click()
            return
        except Exception as e:
            last_err = e
    raise TimeoutException(f"Não consegui clicar no botão de enviar: {last_err}")

def _wait_download_button(driver, timeout: int = GENERATION_TIMEOUT):
    """
    Espera pelo botão de download de imagem em PT/EN.
    """
    wait = WebDriverWait(driver, timeout)
    selectors = [
        "button[aria-label='Download this image']",
        "button[aria-label*='Download']",
        "button[aria-label='Baixar essa imagem']",
        "button[aria-label*='Baixar']",
    ]
    last_err = None
    for sel in selectors:
        try:
            el = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
            return el
        except Exception as e:
            last_err = e
    raise TimeoutException(f"Não apareceu botão de download em {timeout}s. Último erro: {last_err}")

# ===================== Função principal =====================
def gerar_imagem_chatgpt(prompt: str, cookies_path: str, arquivo_saida: str) -> bool:
    """
    Abre Chrome "limpo", injeta cookies e pede ao ChatGPT uma imagem 9:16.
    Retorna True/False. NUNCA usa proxy (forçado via flags do Chrome).
    """
    temp_download_dir = os.path.abspath(os.path.join("cache", "chatgpt_downloads"))
    os.makedirs(temp_download_dir, exist_ok=True)

    # Chrome Options
    opts = uc.ChromeOptions()
    opts.add_argument("--log-level=3")
    # Força NÃO usar proxy (mesmo se houver variáveis de ambiente)
    opts.add_argument("--proxy-server=direct://")
    opts.add_argument("--proxy-bypass-list=*")
    # Reduz páginas esperando assets pesados
    try:
        opts.page_load_strategy = "eager"
    except Exception:
        pass
    # Diretório de download
    opts.add_experimental_option("prefs", {
        "download.default_directory": temp_download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
    })

    driver = None
    try:
        logger.info(f"Iniciando automação com cookies (Headless: {HEADLESS})...")
        driver = uc.Chrome(options=opts, headless=HEADLESS)
        try:
            driver.set_window_size(WIN_W, WIN_H)
        except Exception:
            pass

        try:
            driver.set_page_load_timeout(PAGELOAD_TIMEOUT)
        except Exception:
            pass

        # 1) Ir ao domínio para aceitar cookies
        logger.info("Navegando para o ChatGPT...")
        driver.get("https://chatgpt.com/")
        time.sleep(2)

        # 2) Cookies
        logger.info("Carregando cookies e injetando sessão…")
        injected = _inject_cookies(driver, cookies_path)
        if injected <= 0:
            logger.warning("Nenhum cookie válido foi injetado; a sessão pode não autenticar.")

        # 3) Recarregar autenticado
        logger.info("Atualizando a página para aplicar a sessão...")
        driver.get("https://chatgpt.com/")
        # Opcional: aguardar algum indicador de sessão (avatar, etc.) — por enquanto, segue direto

        # 4) Campo de prompt
        logger.info("Aguardando o campo de prompt…")
        prompt_el = _find_prompt_input(driver, max_wait=45)
        logger.info("Campo localizado.")

        full_prompt = (
            f"Create a single **photorealistic** image in 9:16 aspect ratio, high resolution. "
            f"Theme: {prompt}. "
            "Avoid any text, watermarks, or logos. Cinematic lighting, high detail."
        )

        # 5) Inserir prompt
        _type_prompt(driver, prompt_el, full_prompt)
        time.sleep(0.5)

        # 6) Enviar
        _click_send(driver, max_wait=30)
        logger.info("Prompt enviado. Aguardando geração da imagem...")

        # 7) Esperar botão de download
        download_btn = _wait_download_button(driver, timeout=GENERATION_TIMEOUT)
        logger.info("Imagem gerada. Clicando em download...")
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", download_btn)
        except Exception:
            pass
        time.sleep(0.5)
        download_btn.click()

        # 8) Esperar arquivo
        downloaded_path = _wait_for_file_download(temp_download_dir, timeout=FILE_DOWNLOAD_TIMEOUT)

        # 9) Converter/Salvar PNG final
        img = Image.open(downloaded_path).convert("RGB")
        os.makedirs(os.path.dirname(arquivo_saida) or ".", exist_ok=True)
        img.save(arquivo_saida, "PNG")
        logger.info(f"Imagem processada e salva em: {arquivo_saida}")

        return True

    except TimeoutException as e:
        logger.error(f"❌ Timeout no fluxo do ChatGPT: {e}")
        if driver:
            _safe_screenshot(driver, os.path.join("cache", f"chatgpt_error_{int(time.time())}.png"))
        return False

    except (NoSuchWindowException, WebDriverException) as e:
        # Erros típicos quando o driver foi morto externamente (ex.: watchdog do main)
        logger.error(f"❌ Driver/Sessão indisponível durante a automação: {e}")
        # Evita novas chamadas no driver após morte externa
        return False

    except Exception as e:
        logger.error(f"❌ Erro durante a automação do ChatGPT: {e}")
        if driver:
            _safe_screenshot(driver, os.path.join("cache", f"chatgpt_error_{int(time.time())}.png"))
        return False

    finally:
        try:
            if driver:
                driver.quit()
        except Exception:
            pass
        if os.path.exists(temp_download_dir):
            try:
                shutil.rmtree(temp_download_dir)
            except OSError as e:
                logger.warning(f"Não foi possível limpar a pasta de download temporária: {e}")
