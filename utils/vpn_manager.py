# utils/vpn_manager.py
import os
import time
import logging
from datetime import datetime
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException, NoSuchElementException

try:
    # Este import é crucial para a função de setup
    from .tiktok_uploader.browsers import get_browser
except (ImportError, ModuleNotFoundError):
    def get_browser(*args, **kwargs):
        # Fallback para evitar que o import quebre o script principal
        raise NotImplementedError("get_browser não pôde ser importado de tiktok_uploader.browsers")

logger = logging.getLogger(__name__)

class VpnConnectionError(Exception):
    """Erro customizado para falhas de conexão da VPN."""
    pass

# ==============================================================================
# FUNÇÃO ORQUESTRADORA PRINCIPAL DE CONEXÃO
# ==============================================================================

def connect_vpn(driver: WebDriver) -> bool:
    """
    Verifica a flag no .env e chama a função de conexão da VPN apropriada.
    """
    provider = os.getenv("VPN_PROVIDER", "none").lower()
    
    if provider == "urban":
        logger.info("VPN Provider selecionado: Urban VPN")
        return connect_urban_vpn(driver)
    elif provider == "zoog":
        logger.info("VPN Provider selecionado: ZoogVPN")
        return connect_zoog_vpn(driver)
    elif provider == "none":
        logger.info("Nenhuma VPN selecionada (VPN_PROVIDER=none).")
        return True # Retorna sucesso pois nenhuma conexão era necessária
    else:
        raise VpnConnectionError(f"Provedor de VPN desconhecido: '{provider}'. Verifique a variável VPN_PROVIDER no .env.")

# ==============================================================================
# LÓGICA DE CONEXÃO DA ZOOGVPN
# ==============================================================================

def connect_zoog_vpn(driver: WebDriver) -> bool:
    """
    Automatiza a conexão da ZoogVPN.
    """
    try:
        ext_id = os.getenv("ZOOGVPN_EXTENSION_ID")
        country_name = os.getenv("ZOOGVPN_CONNECT_COUNTRY", "EG - Cairo")
        connect_timeout = int(os.getenv("ZOOGVPN_CONNECT_TIMEOUT_SEC", "40"))

        if not ext_id:
            raise VpnConnectionError("ZOOGVPN_EXTENSION_ID não definido no .env")

        logger.info(f"🔌 Conectando ZoogVPN no país: {country_name}...")
        
        original_window = driver.current_window_handle
        driver.switch_to.new_window('tab')
        driver.get(f'chrome-extension://{ext_id}/popup.html')
        
        logger.info("Aguardando a interface da extensão carregar...")
        wait = WebDriverWait(driver, 20)
        time.sleep(4)

        # PASSO 1: Clicar no botão para abrir a lista de países
        logger.info("Procurando o seletor de localizações...")
        location_button = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button.location-btn")
        ))
        location_button.click(); time.sleep(1)

        # PASSO 2: Digitar o nome do país no campo de busca
        logger.info(f"Procurando o campo de busca e digitando '{country_name}'...")
        search_input = wait.until(EC.visibility_of_element_located(
            (By.CSS_SELECTOR, "div.searchbar input[type='text']")
        ))
        search_input.clear(); search_input.send_keys(country_name); time.sleep(2)

        # PASSO 3: Clicar no país que apareceu nos resultados da busca
        logger.info(f"Procurando por '{country_name}' na lista e clicando para conectar...")
        country_element = wait.until(EC.element_to_be_clickable(
            (By.XPATH, f"//div[contains(@class, 'server-name')]//p[normalize-space()='{country_name}']")
        ))
        country_element.click()
        logger.info(f"País '{country_name}' selecionado. A conexão deve iniciar automaticamente.")
        
        # PASSO 4: Espera o status "connected" aparecer
        logger.info("Aguardando confirmação da conexão (status 'connected')...")
        WebDriverWait(driver, connect_timeout).until(
            EC.presence_of_element_located((
                By.XPATH,
                "//div[contains(@class, 'connection-status')]//p[normalize-space()='connected']"
            ))
        )
        
        logger.info("✅ ZoogVPN Conectada com sucesso!")
        return True

    except Exception as e:
        logger.error(f"❌ Falha ao conectar a ZoogVPN. Erro: {type(e).__name__} - {e}")
        raise VpnConnectionError("Não foi possível estabelecer conexão com a ZoogVPN.") from e
    
    finally:
        if driver and len(driver.window_handles) > 1:
            try:
                driver.switch_to.window(driver.window_handles[1]); driver.close()
                driver.switch_to.window(original_window)
            except Exception: pass

# ==============================================================================
# LÓGICA DE CONEXÃO DA URBAN VPN
# ==============================================================================

def connect_urban_vpn(driver: WebDriver) -> bool:
    """
    Automatiza a conexão da Urban VPN. Clicar no país já inicia a conexão.
    """
    try:
        ext_id = os.getenv("URBANVPN_EXTENSION_ID")
        country_name = os.getenv("URBANVPN_CONNECT_COUNTRY", "Egypt")
        connect_timeout = int(os.getenv("URBANVPN_CONNECT_TIMEOUT_SEC", "40"))

        if not ext_id:
            raise VpnConnectionError("URBANVPN_EXTENSION_ID não definido no .env")

        logger.info(f"🔌 Conectando Urban VPN no país: {country_name}...")
        
        original_window = driver.current_window_handle
        if len(driver.window_handles) > 1:
            for handle in driver.window_handles[1:]:
                driver.switch_to.window(handle); driver.close()
            driver.switch_to.window(original_window)

        driver.switch_to.new_window('tab')
        driver.get(f'chrome-extension://{ext_id}/popup/index.html')
        
        logger.info("Aguardando a interface da extensão carregar...")
        time.sleep(5) 

        try:
            agree_button_wait = WebDriverWait(driver, 7)
            agree_button1 = agree_button_wait.until(EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='Agree']")))
            agree_button1.click(); time.sleep(2)
            agree_button2 = agree_button_wait.until(EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='Agree']")))
            agree_button2.click(); time.sleep(3)
        except TimeoutException:
            logger.info("Nenhuma tela de consentimento nova encontrada.")
        
        wait = WebDriverWait(driver, 20)
        
        location_selector = wait.until(EC.element_to_be_clickable((By.XPATH, "//div[contains(@class, 'location-view__content')]")))
        location_selector.click(); time.sleep(1)

        search_input = wait.until(EC.visibility_of_element_located((By.XPATH, "//input[@placeholder='Search']")))
        search_input.clear(); search_input.send_keys(country_name); time.sleep(2)

        country_element = wait.until(EC.element_to_be_clickable((By.XPATH, f"//div[contains(@class, 'selector-option')]//p[contains(., '{country_name}')]")))
        country_element.click(); 
        logger.info(f"País '{country_name}' selecionado. A conexão deve iniciar automaticamente.")
        time.sleep(3)
        
        try:
            rate_us_wait = WebDriverWait(driver, 5)
            driver.find_element(By.XPATH, "//div[contains(@class, 'simple-layout__header')]//*[contains(@class, 'close')]").click()
            logger.info("Popup 'Rate Us' fechado."); time.sleep(2)
        except Exception:
            logger.info("Nenhum popup de avaliação encontrado.")

        logger.info("Aguardando confirmação da conexão (status 'Connected')...")
        WebDriverWait(driver, connect_timeout).until(
            EC.presence_of_element_located((
                By.XPATH, 
                "//p[contains(@class, 'connection-state__status-text') and normalize-space()='Connected']"
            ))
        )
        
        logger.info("✅ Urban VPN Conectada com sucesso!")
        return True

    except Exception as e:
        logger.error(f"❌ Falha ao conectar a Urban VPN. Erro: {type(e).__name__} - {e}")
        raise VpnConnectionError("Não foi possível estabelecer conexão com a Urban VPN.") from e
    
    finally:
        if driver and len(driver.window_handles) > 1:
            try:
                driver.switch_to.window(driver.window_handles[1]); driver.close()
                driver.switch_to.window(original_window)
            except Exception: pass

# ==============================================================================
# LÓGICA DE SETUP DE PRIMEIRA EXECUÇÃO
# ==============================================================================
def setup_vpn():
    """Lê o .env e chama a função de setup para a VPN selecionada."""
    provider = os.getenv("VPN_PROVIDER", "none").lower()
    if provider == 'urban':
        setup_urban_vpn()
    elif provider == 'zoog':
        setup_zoog_vpn()
    else:
        logger.info("Nenhuma VPN selecionada para setup (VPN_PROVIDER=none).")

def _get_setup_flag_path(profile_name: str) -> str:
    base_profile_dir = os.getenv("CHROME_PROFILES_DIR", os.path.join("cache", "chrome_profiles"))
    profile_dir = os.path.join(base_profile_dir, profile_name)
    os.makedirs(profile_dir, exist_ok=True)
    return os.path.join(profile_dir, ".vpn_setup_complete")

def is_vpn_setup_complete(profile_name: str) -> bool:
    return os.path.exists(_get_setup_flag_path(profile_name))

def _run_manual_setup_flow(profile_name: str, install_url: str, vpn_name: str):
    """Função genérica para guiar a instalação manual de uma extensão."""
    logger.info(f"Abrindo navegador com perfil '{profile_name}' para configuração manual da {vpn_name}...")
    driver = None
    try:
        driver = get_browser(name="chrome", headless=False, vpn_profile_name=profile_name, want_proxy=False)
        driver.get(install_url)
        
        logger.info("+"*60)
        logger.info(f"POR FAVOR, FAÇA A CONFIGURAÇÃO MANUAL DA {vpn_name}:")
        logger.info(f"1. Instale a extensão {vpn_name}.")
        logger.info("2. Clique no ícone da extensão, aceite os termos e feche abas de 'Bem-vindo'.")
        logger.info("3. Se necessário, faça login na extensão.")
        logger.info("4. Deixe a extensão pronta para uso.")
        logger.info("5. Após terminar, feche esta janela do navegador manualmente.")
        logger.info("+"*60)

        while True:
            try:
                _ = driver.window_handles; time.sleep(2)
            except (WebDriverException, ConnectionRefusedError):
                logger.info("Navegador fechado pelo usuário."); break
        
        flag_path = _get_setup_flag_path(profile_name)
        with open(flag_path, "w") as f: f.write(datetime.now().isoformat())
        logger.info(f"Arquivo de flag de setup '{flag_path}' criado.")
    except Exception as e:
        logger.error(f"Ocorreu um erro durante o setup da VPN: {e}")
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass

def setup_urban_vpn():
    """Prepara o ambiente para a primeira execução da Urban VPN."""
    profile_name = os.getenv("URBANVPN_PROFILE_NAME")
    ext_id = os.getenv("URBANVPN_EXTENSION_ID")
    if not profile_name or not ext_id:
        raise VpnConnectionError("URBANVPN_PROFILE_NAME e URBANVPN_EXTENSION_ID devem estar definidos.")
    install_url = os.getenv("URBANVPN_FIRST_RUN_URL", f"https://chromewebstore.google.com/detail/{ext_id}")
    _run_manual_setup_flow(profile_name, install_url, "Urban VPN")

def setup_zoog_vpn():
    """Prepara o ambiente para a primeira execução da ZoogVPN."""
    profile_name = os.getenv("ZOOGVPN_PROFILE_NAME")
    ext_id = os.getenv("ZOOGVPN_EXTENSION_ID")
    if not profile_name or not ext_id:
        raise VpnConnectionError("ZOOGVPN_PROFILE_NAME e ZOOGVPN_EXTENSION_ID devem estar definidos.")
    install_url = f"https://chromewebstore.google.com/detail/{ext_id}"
    _run_manual_setup_flow(profile_name, install_url, "ZoogVPN")