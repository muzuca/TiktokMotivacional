# utils/countries.py
import os
from dataclasses import dataclass
from typing import Dict, Optional

@dataclass(frozen=True)
class CountryCfg:
    key: str                 # ex: "US", "BR", "EG", "RU"
    label: str               # como aparece no menu
    idioma: str              # "en", "pt-br", "ar", "ru"
    lang_tag: str            # ex: "en-US", "pt-BR", "ar-EG", "ru-RU"
    region: str              # ex: "US", "BR", "EG", "RU" (para TikTok / profile)
    timezone: str            # tz default; pode ser sobrescrito por proxy
    cookies_env: str         # nome da ENV com o arquivo de cookies (ex: COOKIES_US_FILENAME)
    cookies_default: str     # fallback do arquivo de cookies
    proxy_env: str           # nome da ENV do proxy (ex: PROXY_US)
    persona: str             # persona default para Veo3 (ex.: "luisa", "yasmina", "alina")
    headless_defaults: dict  # {"flow": True, "tiktok": True, "chatgpt": False}

REGISTRY: Dict[str, CountryCfg] = {
    "US": CountryCfg(
        key="US",
        label="EUA (Inglês)",
        idioma="en",
        lang_tag="en-US",
        region="US",
        timezone=os.getenv("TZ_US", "America/New_York"),
        cookies_env="COOKIES_US_FILENAME",
        cookies_default="cookies_us.txt",
        proxy_env="PROXY_US",
        persona=os.getenv("PERSONA_US", "sophia"),
        headless_defaults={"flow": True, "tiktok": True, "chatgpt": False},
    ),
    "BR": CountryCfg(
        key="BR",
        label="Brasil (pt-br)",
        idioma="pt-br",
        lang_tag="pt-BR",
        region="BR",
        timezone=os.getenv("TZ_BR", "America/Sao_Paulo"),
        cookies_env="COOKIES_BR_FILENAME",
        cookies_default="cookies_br.txt",
        proxy_env="PROXY_BR",
        persona=os.getenv("PERSONA_BR", "luisa"),
        headless_defaults={"flow": True, "tiktok": True, "chatgpt": False},
    ),
    "EG": CountryCfg(
        key="EG",
        label="Árabe (egípcio)",
        idioma="ar",
        lang_tag="ar-EG",
        region="EG",
        timezone=os.getenv("TZ_EG", "Africa/Cairo"),
        cookies_env="COOKIES_EG_FILENAME",
        cookies_default="cookies_eg.txt",
        proxy_env="PROXY_EG",
        persona=os.getenv("PERSONA_EG", "yasmina"),
        headless_defaults={"flow": True, "tiktok": True, "chatgpt": False},
    ),
    "RU": CountryCfg(
        key="RU",
        label="Rússia (Russo)",
        idioma="ru",
        lang_tag="ru-RU",
        region="RU",
        timezone=os.getenv("TZ_RU", "Europe/Moscow"),
        cookies_env="COOKIES_RU_FILENAME",
        cookies_default="cookies_ru.txt",
        proxy_env="PROXY_RU",
        persona=os.getenv("PERSONA_RU", "alina"),
        headless_defaults={"flow": True, "tiktok": True, "chatgpt": False},
    ),
}

# Helpers

def list_menu_items():
    # Mantém a ordem padrão: US, BR, EG, RU
    order = ["US", "BR", "EG", "RU"]
    return [(i+1, REGISTRY[k]) for i, k in enumerate(order)]

def get_by_menu_choice(choice: str) -> Optional[CountryCfg]:
    mapping = {str(i): cfg for i, cfg in list_menu_items()}
    return mapping.get(choice)

def get_by_id(key_or_idioma: str) -> Optional[CountryCfg]:
    s = (key_or_idioma or "").strip().lower()
    for cfg in REGISTRY.values():
        if s in (cfg.key.lower(), cfg.idioma.lower(), cfg.region.lower()):
            return cfg
    return None
