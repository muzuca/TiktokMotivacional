# utils/cache_store.py
from __future__ import annotations
import os, re, json, threading
from typing import List, Optional

# --- normalização de idioma usada pelo projeto -------------------------------
def _norm_lang(x: Optional[str]) -> str:
    s = (x or "").strip().lower()
    if s.startswith("pt"): return "pt"
    if s.startswith("ar"): return "ar"
    if s.startswith("ru"): return "ru"
    return "en"

# --- cache simples por idioma com persistência em JSON -----------------------
class FileLangCache:
    def __init__(self, cache_dir: Optional[str] = None, max_items: Optional[int] = None):
        self.cache_dir = os.getenv("CACHE_DIR", "cache") if cache_dir is None else cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)
        self.max_items = int(os.getenv("CACHE_MAX_ITEMS", str(max_items or 5000)))
        self._lock = threading.RLock()

    # nomes como used_phrases_en.json, used_pexels_prompts_pt.json etc.
    def _file(self, key: str, lang: Optional[str]) -> str:
        key_s = re.sub(r"[^\w\-]+", "_", key.strip().lower())
        lang_s = _norm_lang(lang)
        return os.path.join(self.cache_dir, f"{key_s}_{lang_s}.json")

    def _read(self, path: str) -> List[str]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [str(x) for x in data]
        except Exception:
            pass
        return []

    def _write(self, path: str, items: List[str]) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items[-self.max_items :], f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    # normaliza conteúdo para deduplicação
    def _norm_value(self, v: str) -> str:
        s = re.sub(r"\s+", " ", (v or "").strip())
        return s

    # API usada no projeto -----------------------------------------------------
    def add(self, key: str, value: str, *, lang: Optional[str] = None) -> bool:
        """Adiciona ao cache; retorna True se for novo."""
        path = self._file(key, lang)
        norm = self._norm_value(value)
        with self._lock:
            items = self._read(path)
            # checagem case-insensitive
            lowset = {i.lower() for i in items}
            if norm.lower() in lowset:
                return False
            items.append(norm)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._write(path, items)
            return True

    def seen(self, key: str, value: str, *, lang: Optional[str] = None) -> bool:
        path = self._file(key, lang)
        norm = self._norm_value(value)
        with self._lock:
            items = self._read(path)
            return norm.lower() in {i.lower() for i in items}

    def get_all(self, key: str, *, lang: Optional[str] = None) -> List[str]:
        path = self._file(key, lang)
        with self._lock:
            return self._read(path)

# instância global importada como `cache`
cache = FileLangCache()
