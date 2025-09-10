# countries.py
# Centraliza normalização de idioma, metadados por país e paths de cookies.

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Optional

TRUE_SET = {"1", "true", "yes", "on"}

# Diretórios padrão (podem ser sobrescritos no .env)
COOKIES_DIR = os.path.abspath(os.getenv("COOKIES_DIR", "cache/cookies"))
CHROME_PROFILES_DIR = os.path.abspath(os.getenv("CHROME_PROFILES_DIR", "cache/chrome_profiles"))
CHROME_DISK_CACHE_DIR = os.path.abspath(os.getenv("CHROME_DISK_CACHE_DIR", "cache/chrome_cache"))

os.makedirs(COOKIES_DIR, exist_ok=True)
os.makedirs(CHROME_PROFILES_DIR, exist_ok=True)
os.makedirs(CHROME_DISK_CACHE_DIR, exist_ok=True)

@dataclass(frozen=True)
class CountryConfig:
    code: str          # "US", "BR", "EG", "RU", "ID", etc.
    lang_key: str      # "en", "pt-br", "ar", "ru", "id"
    lang_tag: str      # "en-US", "pt-BR", "ar-EG", "ru-RU", "id-ID"
    region: str        # "US", "BR", "EG", "RU", "ID"
    cookies_env: str   # nome da env p/ nome do arquivo de cookies
    cookies_default: str  # default do filename
    proxy_env_key: Optional[str] = None  # se usar um proxy específico por país

# Tabela de países suportados
_COUNTRIES: Dict[str, CountryConfig] = {
    "en": CountryConfig(
        code="US", lang_key="en", lang_tag="en-US", region="US",
        cookies_env="COOKIES_US_FILENAME", cookies_default="tiktok/us.txt",
        proxy_env_key="UPSTREAM_PROXY_US"
    ),
    "pt-br": CountryConfig(
        code="BR", lang_key="pt-br", lang_tag="pt-BR", region="BR",
        cookies_env="COOKIES_BR_FILENAME", cookies_default="tiktok/br.txt",
        proxy_env_key="UPSTREAM_PROXY_BR"
    ),
    "ar": CountryConfig(
        code="EG", lang_key="ar", lang_tag="ar-EG", region="EG",
        cookies_env="COOKIES_EG_FILENAME", cookies_default="tiktok/eg.txt",
        proxy_env_key="UPSTREAM_PROXY_EG"
    ),
    "ru": CountryConfig(
        code="RU", lang_key="ru", lang_tag="ru-RU", region="RU",
        cookies_env="COOKIES_RU_FILENAME", cookies_default="tiktok/ru.txt",
        proxy_env_key="UPSTREAM_PROXY_RU"
    ),
    "id": CountryConfig(  # <<<<<--- Indonésia ADICIONADO
        code="ID", lang_key="id", lang_tag="id-ID", region="ID",
        cookies_env="COOKIES_ID_FILENAME", cookies_default="tiktok/id.txt",
        proxy_env_key="UPSTREAM_PROXY_ID"
    ),
}

def normalize_lang(value: Optional[str]) -> str:
    """Normaliza a entrada para 'en', 'pt-br', 'ar', 'ru' ou 'id'."""
    s = (value or "").strip().lower()
    if s in ("1", "en", "en-us", "us", "usa", "eua", "ingles", "inglês", "english"):
        return "en"
    if s in ("2", "pt", "pt-br", "br", "brasil", "portugues", "português"):
        return "pt-br"
    if s in ("3", "ar", "ar-eg", "egito", "eg", "árabe", "arabe"):
        return "ar"
    if s in ("4", "ru", "ru-ru", "russia", "rússia", "russo"):
        return "ru"
    if s in ("5", "id", "id-id", "indonesia", "indonésia", "indo", "bahasa"):
        return "id"
    # fallback: en
    return "en"

def get_config(lang_key: str) -> CountryConfig:
    """Retorna a configuração do país para o lang_key normalizado."""
    key = normalize_lang(lang_key)
    return _COUNTRIES.get(key, _COUNTRIES["en"])

def cookies_filename_for(lang_key: str) -> str:
    """Nome do arquivo de cookies (apenas o filename)."""
    cfg = get_config(lang_key)
    return os.getenv(cfg.cookies_env, cfg.cookies_default)

def cookies_path_for(lang_key: str) -> str:
    """Caminho absoluto do arquivo de cookies, dentro de COOKIES_DIR."""
    filename = cookies_filename_for(lang_key)
    if os.path.isabs(filename):
        return filename
    return os.path.join(COOKIES_DIR, filename)

def flow_cookies_file() -> str:
    """Caminho absoluto dos cookies do Flow (veo3) em COOKIES_DIR."""
    name = os.getenv("COOKIES_VEO3_FILENAME", "cookies_veo3.txt")
    if os.path.isabs(name):
        return name
    return os.path.join(COOKIES_DIR, name)

def tiktok_headless_default() -> bool:
    """Headless do TikTok (default ON)."""
    v = (os.getenv("TIKTOK_HEADLESS", "1").strip().lower() or "1")
    return v in TRUE_SET
