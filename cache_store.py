# cache_store.py
from __future__ import annotations
import os, json, re, tempfile, shutil
from typing import Iterable, List, Optional

CACHE_DIR = os.getenv("CACHE_DIR", "cache").strip() or "cache"
os.makedirs(CACHE_DIR, exist_ok=True)

def _norm_lang(lang: Optional[str]) -> str:
    """Normaliza para pt | en | ar (fallback: en). Aceita pt-br, en-US, ar-EG etc."""
    s = (lang or "").strip().lower()
    if s.startswith("pt"): return "pt"
    if s.startswith("ar"): return "ar"
    return "en"

_AR_RE = re.compile(r"[\u0600-\u06FF]")  # caracteres árabes
_PT_DIA = re.compile(r"[áàâãéêíóôõúüç]", re.IGNORECASE)
_PT_HINT = re.compile(r"\b(que|não|você|de|para|com|uma|um|seu|sua|mais|menos|porque|pra|sobre|hoje|amanhã)\b", re.IGNORECASE)

def detect_lang(text: str) -> str:
    """Heurística simples: árabe > PT (diacrítico/palavra) > EN."""
    if _AR_RE.search(text or ""):
        return "ar"
    sample = (text or "")[:512]
    if _PT_DIA.search(sample) or _PT_HINT.search(sample):
        return "pt"
    return "en"

def _atomic_write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=os.path.dirname(path))
    os.close(fd)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    shutil.move(tmp, path)

def _read_list(path: str) -> List:
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if isinstance(obj, list):
            return obj
        return []
    except Exception:
        return []

def _legacy_path(base: str) -> str:
    return os.path.join(CACHE_DIR, f"{base}.json")

def _lang_path(base: str, lang: str) -> str:
    # Ex.: used_phrases_pt.json | used_long_phrases_ar.json
    return os.path.join(CACHE_DIR, f"{base}_{lang}.json")

class LanguageAwareCache:
    """
    Lê/escreve listas JSON por idioma, mantendo compat com arquivo legado sem sufixo.
    Bases previstas: used_phrases, used_long_phrases, used_images, used_pexels_prompts, used_audios (ver abaixo).
    """
    def __init__(self, cache_dir: Optional[str] = None):
        self.cache_dir = cache_dir or CACHE_DIR

    # ---------- API pública ----------
    def list(self, base: str, lang: Optional[str] = None, *, include_legacy=True) -> List:
        if base == "used_audios":
            # Áudios originalmente não segmentados — mantemos o legado
            return _read_list(_legacy_path(base))
        l = _norm_lang(lang) if lang else None
        items = []
        if l:
            items.extend(_read_list(_lang_path(base, l)))
        if include_legacy:
            items.extend(_read_list(_legacy_path(base)))
        # Dedup preservando ordem
        seen = set()
        out: List = []
        for it in items:
            key = json.dumps(it, ensure_ascii=False, sort_keys=True) if isinstance(it, (dict, list)) else str(it)
            if key not in seen:
                seen.add(key); out.append(it)
        return out

    def contains(self, base: str, item, lang: Optional[str] = None) -> bool:
        l = _norm_lang(lang) if lang else None
        hay = self.list(base, l, include_legacy=True)
        return item in hay

    def add(self, base: str, item, lang: Optional[str] = None) -> None:
        if base == "used_audios":
            path = _legacy_path(base)
            data = _read_list(path)
            if item not in data:
                data.append(item)
                _atomic_write_json(path, data)
            return

        l = _norm_lang(lang) if lang else self._infer_lang_from_item(item)
        path = _lang_path(base, l)
        data = _read_list(path)
        if item not in data:
            data.append(item)
            _atomic_write_json(path, data)

    def add_many(self, base: str, items: Iterable, lang: Optional[str] = None) -> None:
        for it in items:
            self.add(base, it, lang=lang)

    # ---------- auxiliares ----------
    def _infer_lang_from_item(self, item) -> str:
        try:
            if isinstance(item, str):
                return detect_lang(item)
            if isinstance(item, dict):
                for k in ("text", "phrase", "caption", "title", "name"):
                    if k in item and isinstance(item[k], str):
                        return detect_lang(item[k])
            if isinstance(item, list) and item and isinstance(item[0], str):
                return detect_lang(" ".join(item[:4]))
        except Exception:
            pass
        return "en"

# Instância global de conveniência
cache = LanguageAwareCache()
