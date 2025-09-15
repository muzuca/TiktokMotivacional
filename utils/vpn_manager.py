# utils/vpn_manager.py (VERSÃO FINAL SEM CLIQUE REDUNDANTE NO PLAY)
import os
import time
import logging
from datetime import datetime
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

try:
    from .tiktok_uploader.browsers import get_browser
except (ImportError, ModuleNotFoundError):
    def get_browser(*args, **kwargs):
        raise NotImplementedError("get_browser não disponível fora do contexto principal.")

logger = logging.getLogger(__name__)

class VpnConnectionError(Exception):
    """Erro customizado para falhas de conexão da VPN."""
    pass

def _get_setup_flag_path(profile_name: str) -> str:
    base_profile_dir = os.getenv("CHROME_PROFILES_DIR", os.path.join("cache", "chrome_profiles"))
    profile_dir = os.path.join(base_profile_dir, profile_name)
    return os.path.join(profile_dir, ".vpn_setup_complete")

def is_vpn_setup_complete(profile_name: str) -> bool:
    return os.path.exists(_get_setup_flag_path(profile_name))

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
        
        if len(driver.window_handles) > 1:
            original_window = driver.window_handles[0]
            for handle in driver.window_handles[1:]:
                driver.switch_to.window(handle); driver.close()
            driver.switch_to.window(original_window)

        driver.switch_to.new_window('tab')
        driver.get(f'chrome-extension://{ext_id}/popup/index.html')
        
        logger.info("Aguardando a interface da extensão carregar...")
        time.sleep(5) 

        # PASSO 1: Lidar com telas de consentimento (se aparecerem)
        try:
            agree_button_wait = WebDriverWait(driver, 7)
            agree_button1 = agree_button_wait.until(EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='Agree']")))
            logger.info("Tela de consentimento 1 detectada. Clicando em 'Agree'...")
            agree_button1.click(); time.sleep(2)
            agree_button2 = agree_button_wait.until(EC.element_to_be_clickable((By.XPATH, "//button[normalize-space()='Agree']")))
            logger.info("Tela de consentimento 2 detectada. Clicando em 'Agree'...")
            agree_button2.click(); time.sleep(3)
        except TimeoutException:
            logger.info("Nenhuma tela de consentimento nova encontrada.")
        except Exception as e:
            logger.warning(f"Erro não crítico ao aceitar os termos: {e}")

        wait = WebDriverWait(driver, 20)
        
        # PASSO 2: Clicar para abrir a lista de países
        logger.info("Procurando o seletor de localizações...")
        location_selector = wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//div[contains(@class, 'location-view__content')]")
        ))
        location_selector.click(); time.sleep(1)

        # PASSO 3: Digitar o nome do país no campo de busca
        logger.info(f"Procurando o campo de busca e digitando '{country_name}'...")
        search_input = wait.until(EC.visibility_of_element_located(
            (By.XPATH, "//input[@placeholder='Search']")
        ))
        search_input.clear(); search_input.send_keys(country_name); time.sleep(2)

        # PASSO 4: Clicar no país que apareceu nos resultados da busca
        logger.info(f"Procurando por '{country_name}' e clicando para conectar...")
        country_element = wait.until(EC.element_to_be_clickable(
            (By.XPATH, f"//div[contains(@class, 'selector-option')]//p[contains(., '{country_name}')]")
        ))
        country_element.click(); 
        logger.info(f"País '{country_name}' selecionado. A conexão deve iniciar automaticamente.")
        time.sleep(3)
        
        # ######################################################################
        # REMOVIDO: O clique no botão de play era redundante e desconectava a VPN.
        # ######################################################################

        # PASSO 5: Lidar com o popup "Rate Us" (se aparecer)
        try:
            rate_us_wait = WebDriverWait(driver, 5)
            rate_us_button = rate_us_wait.until(EC.presence_of_element_located((By.XPATH, "//button[contains(@class, 'rate-us-page__action')]")))
            if rate_us_button:
                logger.info("Popup 'Rate Us' detectado. Tentando fechá-lo...")
                close_button = driver.find_element(By.XPATH, "//div[contains(@class, 'simple-layout__header')]//*[contains(@class, 'close')]")
                close_button.click(); logger.info("Popup 'Rate Us' fechado."); time.sleep(2)
        except Exception:
            logger.info("Nenhum popup de avaliação encontrado.")

        # PASSO 6: Espera o status "Connected" aparecer
        logger.info("Aguardando confirmação da conexão (status 'Connected')...")
        WebDriverWait(driver, connect_timeout).until(
            EC.presence_of_element_located((
                By.XPATH, 
                "//p[contains(@class, 'connection-state__status-text') and normalize-space()='Connected']"
            ))
        )
        
        logger.info("✅ VPN Conectada com sucesso!")
        return True

    except Exception as e:
        # Log de erro simplificado, como solicitado
        logger.error(f"❌ Falha ao conectar a Urban VPN. Erro: {type(e).__name__} - {e}")
        raise VpnConnectionError("Não foi possível estabelecer conexão com a VPN.") from e
    
    finally:
        if driver and len(driver.window_handles) > 1:
            try:
                driver.switch_to.window(driver.window_handles[1])
                driver.close()
                driver.switch_to.window(driver.window_handles[0])
            except Exception: pass

def setup_urban_vpn():
    profile_name = os.getenv("URBANVPN_PROFILE_NAME")
    install_url = os.getenv("URBANVPN_FIRST_RUN_URL")
    if not profile_name or not install_url:
        raise VpnConnectionError("URBANVPN_PROFILE_NAME e URBANVPN_FIRST_RUN_URL devem estar definidos no .env para o setup.")

    logger.info(f"Abrindo navegador com perfil '{profile_name}' para configuração manual...")
    driver = None
    try:
        driver = get_browser(name="chrome", headless=False, vpn_profile_name=profile_name, want_proxy=False)
        driver.get(install_url)
        
        logger.info("+"*60)
        logger.info("POR FAVOR, FAÇA A CONFIGURAÇÃO MANUAL DA URBAN VPN:")
        logger.info("1. Instale a extensão Urban VPN.")
        logger.info("2. Clique no ícone da extensão, aceite os termos e feche a aba de 'Bem-vindo'.")
        logger.info("3. Deixe a extensão pronta para uso.")
        logger.info("4. Após terminar, feche o navegador manualmente.")
        logger.info("+"*60)

        while True:
            try:
                _ = driver.window_handles
                time.sleep(2)
            except (WebDriverException, ConnectionRefusedError):
                logger.info("Navegador fechado pelo usuário.")
                break
        
        flag_path = _get_setup_flag_path(profile_name)
        with open(flag_path, "w") as f:
            f.write(datetime.now().isoformat())
        logger.info(f"Arquivo de flag '{flag_path}' criado.")
    except Exception as e:
        logger.error(f"Ocorreu um erro durante o setup da VPN: {e}")
    finally:
        if driver:
            try: driver.quit()
            except Exception: pass