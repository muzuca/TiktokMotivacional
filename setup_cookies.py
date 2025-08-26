# setup_cookies.py
import os
import time
import json
import undetected_chromedriver as uc
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import logging

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

COOKIES_FILE = "cookies_chatgpt.txt"

print("\n--- Script para Gerar o Arquivo de Cookies do ChatGPT ---")

opts = uc.ChromeOptions()
opts.add_argument("--log-level=3")
opts.add_argument("--window-size=1200,800")

driver = None
try:
    logger.info("1. Abrindo o navegador...")
    driver = uc.Chrome(options=opts, headless=False)
    
    logger.info("2. Navegando para o ChatGPT...")
    driver.get("https://chatgpt.com/")

    print("\n" + "="*60)
    print("--- AÇÃO NECESSÁRIA ---")
    print("Uma janela do Chrome foi aberta. Por favor, faça o seguinte:")
    print("  1. Faça o login completo na sua conta do ChatGPT.")
    print("  2. Resolva qualquer CAPTCHA ou verificação que aparecer.")
    print("  3. Após chegar na tela principal de chat, espere 5 segundos.")
    print("\nEste processo SÓ precisa ser feito uma vez (ou quando o login expirar).")
    print("="*60)

    input("\nPressione ENTER neste terminal APÓS ter feito o login para continuar...")

    logger.info("Salvando cookies de sessão...")
    cookies = driver.get_cookies()

    with open(COOKIES_FILE, 'w', encoding='utf-8') as f:
        json.dump(cookies, f, ensure_ascii=False, indent=4)
    
    logger.info(f"✅ Cookies salvos com sucesso no arquivo: {COOKIES_FILE}")
    logger.info("O arquivo está pronto para ser usado pelo script principal.")

except Exception as e:
    logger.error(f"\nOcorreu um erro durante a configuração: {e}")
finally:
    if driver:
        driver.quit()