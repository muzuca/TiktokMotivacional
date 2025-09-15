# extrair_html_vpn.py
import os
import time
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager

print("Iniciando teste para extrair o HTML da extensão...")

load_dotenv()

# --- Pega as informações do .env ---
base_profile_dir = os.getenv("CHROME_PROFILES_DIR")
vpn_profile_name = os.getenv("URBANVPN_PROFILE_NAME")
extension_id = os.getenv("URBANVPN_EXTENSION_ID")

if not all([base_profile_dir, vpn_profile_name, extension_id]):
    print("ERRO: Verifique se CHROME_PROFILES_DIR, URBANVPN_PROFILE_NAME e URBANVPN_EXTENSION_ID estão no .env!")
    exit()

# --- Monta o caminho do perfil e a URL da extensão ---
project_root = os.getcwd()
full_profile_path = os.path.join(project_root, base_profile_dir, vpn_profile_name)
extension_url = f"chrome-extension://{extension_id}/popup/index.html"

print(f"Tentando carregar o perfil: {os.path.abspath(full_profile_path)}")
print(f"Navegando para a URL: {extension_url}")

# --- Configura o Chrome (exatamente como no teste que funcionou) ---
chrome_options = ChromeOptions()
chrome_options.add_argument(f"--user-data-dir={os.path.abspath(full_profile_path)}")
chrome_options.add_argument("--profile-directory=Default")
chrome_options.add_argument("--disable-web-security") # Mantemos por segurança

driver = None
try:
    # Garante que o Chrome está fechado
    try:
        os.system("taskkill /f /im chrome.exe >nul 2>&1")
        time.sleep(2)
    except Exception:
        pass

    driver = webdriver.Chrome(
        service=ChromeService(ChromeDriverManager().install()),
        options=chrome_options
    )
    
    # Abre a página da extensão
    driver.get(extension_url)
    
    print("\nAguardando 10 segundos para a página da extensão carregar completamente...")
    time.sleep(10)
    
    print("\n" + "="*20 + " CÓDIGO HTML DA PÁGINA " + "="*20)
    
    # Imprime o código-fonte da página
    print(driver.page_source)
    
    print("="*64)
    print("\nCÓDIGO HTML EXTRAÍDO COM SUCESSO. Por favor, copie e cole todo o bloco de código acima.")

except Exception as e:
    print(f"\nERRO: {e}")

finally:
    if driver:
        driver.quit()
    print("\nTeste finalizado.")