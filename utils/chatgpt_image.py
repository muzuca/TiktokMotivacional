# utils/chatgpt_image.py
import os
import time
import shutil
import glob
import logging
from PIL import Image
import undetected_chromedriver as uc
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

logger = logging.getLogger(__name__)

# Lê as variáveis do .env no topo
CHROME_PROFILE_PATH = os.getenv("CHROME_USER_PROFILE_PATH", "").strip()
CHATGPT_HEADLESS = os.getenv("CHATGPT_HEADLESS", "1").strip().lower() in ("1", "true", "yes", "on")


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
                time.sleep(2.5) # Espera para garantir que a escrita terminou
                size = os.path.getsize(latest_file)
                if size > 1024:  # > 1KB
                    logger.info(f"Download concluído: {os.path.basename(latest_file)}")
                    return latest_file
        time.sleep(1)
    raise TimeoutError("Tempo limite excedido esperando o download da imagem do ChatGPT.")


def gerar_imagem_chatgpt(prompt: str, cookies_path: str, arquivo_saida: str) -> bool:
    """
    Usa undetected-chromedriver e um perfil de usuário para gerar uma imagem no ChatGPT.
    """
    if not CHROME_PROFILE_PATH:
        raise ValueError("CHROME_USER_PROFILE_PATH não está definido no arquivo .env. Este caminho é necessário.")

    logger.info(f"Usando perfil do Chrome em: {CHROME_PROFILE_PATH}")

    temp_download_dir = os.path.abspath(os.path.join("cache", "chatgpt_downloads"))
    os.makedirs(temp_download_dir, exist_ok=True)

    opts = uc.ChromeOptions()
    opts.add_argument("--log-level=3")
    # --- NOVA LINHA PARA DEFINIR O TAMANHO DA JANELA ---
    opts.add_argument("--window-size=800,900") # Largura x Altura
    opts.add_experimental_option("prefs", {
        "download.default_directory": temp_download_dir
    })

    driver = None
    try:
        logger.info(f"Iniciando automação do ChatGPT (Headless: {CHATGPT_HEADLESS})...")
        driver = uc.Chrome(options=opts, user_data_dir=CHROME_PROFILE_PATH, headless=CHATGPT_HEADLESS, use_subprocess=True)
        
        driver.get("https://chatgpt.com/")
        logger.info("Página do ChatGPT carregada com sucesso.")

        logger.info("Aguardando o campo de texto (div contenteditable)...")
        prompt_textarea = WebDriverWait(driver, 45).until(
            EC.presence_of_element_located((uc.By.CSS_SELECTOR, "div#prompt-textarea[contenteditable='true']"))
        )
        logger.info("Campo de texto encontrado.")
        
        prompt_textarea.click()
        time.sleep(0.5)

        # --- PROMPT MELHORADO ---
        full_prompt = (
            f"Generate a single photorealistic image with an aspect ratio of 9:16, based on the following theme: '{prompt}'. "
            "Do not add any text or logos to the image. Focus on a cinematic and high-quality visual."
        )
        
        driver.execute_script(
            "arguments[0].innerText = arguments[1]; arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", 
            prompt_textarea, 
            full_prompt
        )
        time.sleep(1)

        submit_button = WebDriverWait(driver, 10).until(
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
            error_screenshot_path = os.path.join("cache", "chatgpt_error.png")
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