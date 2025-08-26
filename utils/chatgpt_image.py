# utils/chatgpt_image.py
import os
import time
import shutil
import glob
import logging
import json
from PIL import Image
import undetected_chromedriver as uc
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException, InvalidCookieDomainException

logger = logging.getLogger(__name__)

def _wait_for_file_download(download_dir: str, timeout: int = 180) -> str:
    """Aguarda um novo arquivo .png ou .webp aparecer na pasta e estar completo."""
    logger.info("Aguardando download da imagem...")
    end_time = time.time() + timeout
    initial_files = set(glob.glob(os.path.join(download_dir, "*")))

    while time.time() < end_time:
        current_files = set(glob.glob(os.path.join(download_dir, "*")))
        new_files = current_files - initial_files
        
        if new_files:
            valid_new_files = [f for f in new_files if not f.endswith(('.tmp', '.crdownload'))]
            if valid_new_files:
                latest_file = max(valid_new_files, key=os.path.getmtime)
                time.sleep(2.5)
                if os.path.exists(latest_file) and os.path.getsize(latest_file) > 1024:
                    logger.info(f"Download concluído: {os.path.basename(latest_file)}")
                    return latest_file
        time.sleep(1)
    raise TimeoutError("Tempo limite excedido esperando o download da imagem do ChatGPT.")


def gerar_imagem_chatgpt(prompt: str, cookies_path: str, arquivo_saida: str) -> bool:
    """
    Usa um navegador limpo e injeta cookies para autenticação.
    """
    headless = os.getenv("CHATGPT_HEADLESS", "1").strip().lower() in ("1", "true", "yes", "on")
    temp_download_dir = os.path.abspath(os.path.join("cache", "chatgpt_downloads"))
    os.makedirs(temp_download_dir, exist_ok=True)

    opts = uc.ChromeOptions()
    opts.add_argument("--log-level=3")
    # A linha --window-size foi removida daqui, pois não é confiável.
    opts.add_experimental_option("prefs", {
        "download.default_directory": temp_download_dir
    })

    driver = None
    try:
        logger.info(f"Iniciando automação com cookies (Headless: {headless})...")
        driver = uc.Chrome(options=opts, headless=headless)
        
        # --- CORREÇÃO DEFINITIVA PARA O TAMANHO DA JANELA ---
        # Este comando é executado após o navegador iniciar, sendo mais confiável.
        logger.info("Redimensionando a janela para 800x600...")
        driver.set_window_size(800, 600)
        # --- FIM DA CORREÇÃO ---

        driver.set_page_load_timeout(60)
        
        logger.info("Navegando para o ChatGPT...")
        driver.get("https://chatgpt.com/")
        time.sleep(2)

        if not os.path.exists(cookies_path) or os.path.getsize(cookies_path) < 10:
             raise FileNotFoundError(f"Arquivo de cookies '{cookies_path}' não encontrado ou vazio. Execute 'python setup_cookies.py' para criá-lo.")
        
        try:
            with open(cookies_path, 'r', encoding='utf-8') as f:
                cookies = json.load(f)
        except json.JSONDecodeError:
            logger.error(f"ERRO CRÍTICO: O arquivo '{cookies_path}' não está no formato JSON válido.")
            raise ValueError(f"Formato de cookie inválido. Execute 'python setup_cookies.py' para gerar o arquivo correto.")

        logger.info(f"Carregando {len(cookies)} cookies do arquivo '{cookies_path}'...")
        for cookie in cookies:
            if 'sameSite' in cookie:
                del cookie['sameSite']
            if 'domain' in cookie and 'chatgpt.com' in cookie['domain']:
                try:
                    driver.add_cookie(cookie)
                except Exception:
                    pass # Ignora cookies que não podem ser adicionados
        
        logger.info("Cookies injetados. Atualizando a página para aplicar a sessão...")
        driver.get("https://chatgpt.com/")
        
        wait = WebDriverWait(driver, 45)
        
        logger.info("Aguardando o campo de texto visível (div)...")
        prompt_input_area = wait.until(
            EC.presence_of_element_located((uc.By.CSS_SELECTOR, "div#prompt-textarea[contenteditable='true']"))
        )
        logger.info("Campo de texto visível encontrado.")
        
        full_prompt = (
            f"Generate a single photorealistic image with an aspect ratio of 9:16, based on the following theme: '{prompt}'. "
            "Do not add any text or logos to the image. Focus on a cinematic and high-quality visual."
        )
        
        driver.execute_script(
            "arguments[0].innerHTML = arguments[1];", 
            prompt_input_area, 
            f'<p>{full_prompt}</p>'
        )
        time.sleep(1)

        submit_button = wait.until(
            EC.element_to_be_clickable((uc.By.CSS_SELECTOR, "button[data-testid='send-button']"))
        )
        submit_button.click()
        logger.info("Prompt enviado. Aguardando geração da imagem...")

        download_button_selector = "button[aria-label='Download this image'], button[aria-label='Baixar essa imagem']"
        download_button = WebDriverWait(driver, 240).until(
            EC.presence_of_element_located((uc.By.CSS_SELECTOR, download_button_selector))
        )
        logger.info("Imagem gerada. Clicando em download...")
        
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", download_button)
        time.sleep(0.5)
        download_button.click()
        
        downloaded_file_path = _wait_for_file_download(temp_download_dir)
        
        img = Image.open(downloaded_file_path).convert("RGB")
        img.save(arquivo_saida, "png")
        logger.info(f"Imagem processada e salva em: {arquivo_saida}")

        return True

    except Exception as e:
        logger.error(f"❌ Erro durante a automação do ChatGPT: {e}")
        if driver:
            error_screenshot_path = os.path.join("cache", f"chatgpt_error_{int(time.time())}.png")
            driver.save_screenshot(error_screenshot_path)
            logger.error(f"Screenshot de erro salvo em: {error_screenshot_path}")
        return False
    finally:
        if driver:
            driver.quit()
        if os.path.exists(temp_download_dir):
            try:
                shutil.rmtree(temp_download_dir)
            except OSError as e:
                logger.warning(f"Não foi possível limpar a pasta de download temporária: {e}")