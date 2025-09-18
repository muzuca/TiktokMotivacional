# utils/tiktok_uploader/browsers.py (VERSÃO "MODO TESTE" - IDÊNTICA AO TESTE_PERFIL.PY)
import logging
import os
import uuid
import time
from typing import Optional, Tuple
from dotenv import load_dotenv

from selenium import webdriver as std_webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service as ChromeService

load_dotenv()
logger = logging.getLogger(__name__)

# --- Funções auxiliares (mantidas para o modo sem vpn) ---
def _idioma_norm(idioma: Optional[str]) -> str:
    s = (idioma or "").strip().lower()
    if s.startswith("ar"): return "ar"
    if s.startswith("pt"): return "pt"
    if s.startswith("ru"): return "ru"
    if s.startswith("id"): return "id"
    return "en"

def _use_proxy_from_idioma(idioma: Optional[str]) -> bool:
    return _idioma_norm(idioma) in ("en", "ar", "ru", "id")

def _compute_region_from_idioma(idioma: Optional[str]) -> Optional[str]:
    lang = _idioma_norm(idioma)
    if lang == "ar": return "EG"
    if lang == "en": return "US"
    if lang == "pt": return "BR"
    if lang == "ru": return "RU"
    if lang == "id": return "ID"
    return None

def _lang_tag_from_idioma(idioma: Optional[str]) -> str:
    lang_map = {"pt": "pt-BR", "ar": "ar-EG", "ru": "ru-RU", "id": "id-ID"}
    return lang_map.get(_idioma_norm(idioma), "en-US")

def _env_first(*keys: str, default: str = "") -> str:
    for k in keys:
        v = os.getenv(k)
        if v: return v
    return default

def _resolve_proxy_env(region: Optional[str]) -> Tuple[str, str, Optional[str], Optional[str]]:
    reg = (region or "").upper()
    prefix = f"PROXY_{reg}" if reg else "PROXY"
    host = _env_first(f"{prefix}_HOST", "PROXY_HOST")
    port = _env_first(f"{prefix}_PORT", "PROXY_PORT")
    user = _env_first(f"{prefix}_USER", "PROXY_USER") or None
    pw = _env_first(f"{prefix}_PASS", "PROXY_PASS") or None
    return host, port, user, pw

def _mk_seleniumwire_options(use_proxy: bool, region: Optional[str]):
    opts = {'request_storage': 'none', 'verify_ssl': False, 'scopes': [r".*\.tiktok\.com.*"], 'port': 0, 'addr': '127.0.0.1'}
    if not use_proxy: return opts
    host, port, user, pw = _resolve_proxy_env(region)
    if host and port:
        # ==================================================================
        # LOG ADICIONADO NO LOCAL CORRETO, CONFORME SUA SUGESTÃO
        # ==================================================================
        logger.info(f"🔌 Carregando proxy para região '{region or 'PADRÃO'}': Host={host}, Porta={port}")

        proxy_uri = f"http://{user}:{pw}@{host}:{port}" if user and pw else f"http://{host}:{port}"
        opts['proxy'] = {'http': proxy_uri, 'https': proxy_uri, 'no_proxy': 'localhost,127.0.0.1'}
    return opts

def _profile_roots(region: Optional[str], vpn_profile_name: Optional[str] = None) -> Tuple[str, str]:
    base_profile = os.getenv("CHROME_PROFILES_DIR", os.path.join("cache", "chrome_profiles"))
    base_cache = os.getenv("CHROME_DISK_CACHE_DIR", os.path.join("cache", "chrome_cache"))
    if vpn_profile_name:
        profile_dir = os.path.join(base_profile, vpn_profile_name)
        cache_dir = os.path.join(base_cache, vpn_profile_name)
    else:
        reg = _idioma_norm(region)
        tag = uuid.uuid4().hex[:8]
        profile_dir = os.path.join(base_profile, reg, tag)
        cache_dir = os.path.join(base_cache, reg, tag)
    os.makedirs(profile_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)
    return profile_dir, cache_dir

def _unlock_profile(user_data_dir: str):
    lockfile = os.path.join(user_data_dir, "SingletonLock")
    if os.path.exists(lockfile):
        try: os.remove(lockfile)
        except Exception: pass

def get_browser(name: str = "chrome", options=None, proxy=None, idioma: str = "auto", headless: bool = False, *, want_proxy: Optional[bool] = None, region: Optional[str] = None, lang_tag: Optional[str] = None, vpn_profile_name: Optional[str] = None, **kwargs):
    if name != "chrome":
        raise ValueError("Este modo suporta apenas Chrome.")

    logger.info("get_browser: idioma=%s | want_proxy=%s | region=%s | lang_tag=%s | vpn_profile=%s", idioma, want_proxy, region or "-", lang_tag, vpn_profile_name or "N/A")

    if vpn_profile_name:
        # ######################################################################
        # LÓGICA "À PROVA DE FALHAS" - IMITANDO O TESTE_PERFIL.PY
        # ######################################################################
        logger.info("MODO VPN DETECTADO: Usando configuração mínima e estável, idêntica ao script de teste.")
        
        try:
            #os.system("taskkill /f /im chrome.exe >nul 2>&1")
            time.sleep(2)
        except Exception:
            pass

        options = ChromeOptions()
        user_data_dir, _ = _profile_roots(None, vpn_profile_name=vpn_profile_name)
        _unlock_profile(user_data_dir)
        
        # Argumentos EXATOS do teste que funcionou
        options.add_argument(f"--user-data-dir={os.path.abspath(user_data_dir)}")
        options.add_argument("--profile-directory=Default")
        
        # Mantemos apenas este, pois é necessário para a automação da extensão
        options.add_argument("--disable-web-security")

        driver = std_webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=options)
        
        logger.info("🗂️ Perfil Chrome (VPN - modo de teste): %s", os.path.abspath(user_data_dir))
        return driver
    
    else:
        # ######################################################################
        # LÓGICA NORMAL PARA CONEXÕES COM PROXY OU DIRETAS (sem VPN)
        # ######################################################################
        logger.info("Modo padrão (sem VPN) detectado.")
        
        if options is None: options = ChromeOptions()

        if want_proxy is None: want_proxy = _use_proxy_from_idioma(idioma)
        if region is None: region = _compute_region_from_idioma(idioma)
        if lang_tag is None: lang_tag = _lang_tag_from_idioma(idioma)
        
        options.add_argument(f"--lang={lang_tag}")
        options.add_argument("--disable-extensions")
        if headless:
            options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
        
        user_data_dir, _ = _profile_roots(region, vpn_profile_name=None)
        _unlock_profile(user_data_dir)
        options.add_argument(f"--user-data-dir={os.path.abspath(user_data_dir)}")

        try:
            from seleniumwire import webdriver as wire_webdriver
            sw_opts = _mk_seleniumwire_options(want_proxy, region)
            driver = wire_webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=options, seleniumwire_options=sw_opts)
        except (ImportError, RuntimeError):
            logger.warning("Selenium-Wire não encontrado/necessário. Usando Selenium padrão.")
            driver = std_webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=options)
        
        logger.info("🗂️ Perfil Chrome (Padrão): %s", os.path.abspath(user_data_dir))
        return driver