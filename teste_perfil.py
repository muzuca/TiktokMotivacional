# teste_perfil.py
import os
import time
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager

print("Iniciando teste de carregamento de perfil...")

# Carrega as variáveis do arquivo .env
load_dotenv()

# Constrói o caminho para o perfil da VPN
base_profile_dir = os.getenv("CHROME_PROFILES_DIR")
vpn_profile_name = os.getenv("URBANVPN_PROFILE_NAME")

if not base_profile_dir or not vpn_profile_name:
    print("ERRO: As variáveis CHROME_PROFILES_DIR ou URBANVPN_PROFILE_NAME não estão no seu .env!")
    exit()

# Monta o caminho completo e absoluto
project_root = os.getcwd()
full_profile_path = os.path.join(project_root, base_profile_dir, vpn_profile_name)

print(f"Tentando carregar o perfil do Chrome do seguinte caminho:")
print(f"==> {os.path.abspath(full_profile_path)}")

# Configura as opções do Chrome para usar esse perfil
chrome_options = ChromeOptions()
chrome_options.add_argument(f"--user-data-dir={os.path.abspath(full_profile_path)}")
chrome_options.add_argument("--profile-directory=Default") 

# Inicializa o driver
try:
    # Garante que todos os processos do Chrome estejam fechados antes de começar
    # (Isso é uma tentativa, mas o ideal é fechar manualmente)
    #os.system("taskkill /f /im chrome.exe")
    print("\nTentando fechar processos existentes do Chrome...")
    time.sleep(2)

    driver = webdriver.Chrome(
        service=ChromeService(ChromeDriverManager().install()),
        options=chrome_options
    )
    print("\nNavegador iniciado com sucesso! Verifique a janela que abriu.")
    print("A janela ficará aberta por 45 segundos para inspeção...")
    time.sleep(45)
    driver.quit()
    print("Teste concluído.")

except Exception as e:
    print("\n" + "="*20 + " ERRO AO INICIAR O NAVEGADOR " + "="*20)
    print("Não foi possível iniciar o Chrome com o perfil especificado.")
    print(f"MENSAGEM DE ERRO: {e}")
    print("="*66)