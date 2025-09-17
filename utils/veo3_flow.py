# utils/veo3_flow.py — versão estável com perfil persistente + login robusto + idioma dinâmico
from __future__ import annotations

import os
import re
import glob
import json
import time
import shutil
import logging
import subprocess
from typing import List, Dict, Tuple, Optional

from selenium import webdriver
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    WebDriverException,
    NoSuchElementException,
)

# 🔑 País/idioma centralizados
from .countries import normalize_lang, cookies_path_for, flow_cookies_file

logger = logging.getLogger(__name__)

CREDIT_COST_PER_SCENE = 20  # só para log

# =========================================================
# ------------------ Cookies Netscape ---------------------
# =========================================================

def _write_netscape_cookies(path: str, cookies: list) -> str:
    """Escreve cookies no formato Netscape (compatível com nosso loader atual)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lines = [
        "# Netscape HTTP Cookie File",
        "# domain\tflag\tpath\tsecure\texpiration\tname\tvalue",
    ]
    for c in cookies or []:
        try:
            domain = (c.get("domain") or "").strip()
            flag = "FALSE" if (domain and not domain.startswith(".")) else "TRUE"
            pathv = c.get("path") or "/"
            secure = "TRUE" if c.get("secure") else "FALSE"
            exp = c.get("expiry") or c.get("expirationDate") or 0
            try:
                exp = int(exp)
            except Exception:
                exp = 0
            name = c.get("name") or ""
            value = c.get("value") or ""
            lines.append(f"{domain}\t{flag}\t{pathv}\t{secure}\t{exp}\t{name}\t{value}")
        except Exception:
            continue
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.replace(tmp, path)
    except Exception:
        shutil.copy2(tmp, path)
    return path


def _read_netscape_cookies(path: str) -> List[Dict]:
    """Lê cookies no formato Netscape; tolera JSON (lista)."""
    if not path or not os.path.isfile(path):
        return []
    txt = open(path, "r", encoding="utf-8").read().strip()
    if txt.startswith("["):
        try:
            arr = json.loads(txt)
            return arr if isinstance(arr, list) else []
        except Exception:
            return []
    items: List[Dict] = []
    for ln in txt.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith(("#", "!")):
            continue
        parts = ln.split("\t")
        if len(parts) != 7:
            continue
        domain, _flag, pathv, secure_s, expiry, name, value = parts
        try:
            exp_i = int(expiry) if str(expiry).isdigit() else None
        except Exception:
            exp_i = None
        items.append({
            "domain": (domain or "").strip(),
            "path": (pathv or "/").strip(),
            "secure": (secure_s.upper() == "TRUE"),
            "expiry": exp_i,
            "name": name.strip(),
            "value": value.strip(),
        })
    return items


def _group_cookie_domains(cookies: List[Dict]) -> List[str]:
    hosts = set()
    for ck in cookies:
        d = (ck.get("domain") or "").strip().lstrip(".")
        if d:
            hosts.add(d)
    return sorted(hosts, key=lambda h: (h.count("."), len(h)))


def _add_cookies_multi_domain(driver: webdriver.Chrome, cookies: List[Dict]) -> None:
    """Injeta cookies visitando cada domínio primeiro (requisito do Chrome)."""
    for host in _group_cookie_domains(cookies):
        base = f"https://{host}/"
        try:
            driver.get(base)
            time.sleep(0.3)
        except Exception:
            continue
        for ck in cookies:
            if (ck.get("domain") or "").lstrip(".") != host:
                continue
            c = {
                "name": ck.get("name"),
                "value": ck.get("value"),
                "path": ck.get("path") or "/",
                "secure": bool(ck.get("secure", False)),
            }
            if ck.get("expiry"):
                try:
                    c["expiry"] = int(ck["expiry"])
                except Exception:
                    pass
            try:
                driver.add_cookie(c)
            except Exception:
                pass

# =========================================================
# --------------- Chrome / Perfil Persistente -------------
# =========================================================

TRUE_SET = {"1", "true", "yes", "on"}

def _flow_profile_dir(mode: str) -> str:
    """
    Retorna o diretório de perfil persistente do Chrome para o Flow.
    - Se FLOW_CHROME_PROFILE_DIR estiver definido, usa-o para ambos os modos.
    - Se FLOW_CHROME_PROFILE_DIR_HEADLESS/FLOW_CHROME_PROFILE_DIR_VISIBLE estiverem definidos,
      usa-os para cada modo.
    - Caso contrário, usa CHROME_PROFILES_DIR / FLOW / {mode}
    """
    fixed_both = os.getenv("FLOW_CHROME_PROFILE_DIR", "").strip()
    if fixed_both:
        os.makedirs(fixed_both, exist_ok=True)
        return fixed_both

    if mode == "headless":
        fixed = os.getenv("FLOW_CHROME_PROFILE_DIR_HEADLESS", "").strip()
    else:
        fixed = os.getenv("FLOW_CHROME_PROFILE_DIR_VISIBLE", "").strip()
    if fixed:
        os.makedirs(fixed, exist_ok=True)
        return fixed

    base = os.path.abspath(os.getenv("CHROME_PROFILES_DIR", "chrome_profiles"))
    name = "headless" if mode == "headless" else "visible"
    d = os.path.join(base, "FLOW", name)
    os.makedirs(d, exist_ok=True)
    return d


def _chrome(download_dir: str, *, headless: bool) -> webdriver.Chrome:
    os.makedirs(download_dir, exist_ok=True)
    opts = Options()

    if headless:
        opts.add_argument("--headless=new")

    # PERFIL PERSISTENTE (evita ter que logar toda hora)
    prof_dir = _flow_profile_dir("headless" if headless else "visible")
    opts.add_argument(f"--user-data-dir={prof_dir}")
    opts.add_argument("--profile-directory=Default")

    opts.add_argument("--window-size=1366,900")
    opts.add_argument("--log-level=3")
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_experimental_option("prefs", {
        "download.default_directory": os.path.abspath(download_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
        "profile.default_content_setting_values.automatic_downloads": 1,
    })

    logger.info("🗂️ Perfil Chrome (Flow): %s | Headless: %s",
                prof_dir, "ON" if headless else "OFF")
    return webdriver.Chrome(options=opts)

# =========================================================
# -------------------- Utilidades gerais ------------------
# =========================================================

_DEBUG_RUN_DIR: Optional[str] = None

def _begin_debug_run() -> str:
    global _DEBUG_RUN_DIR
    if _DEBUG_RUN_DIR:
        return _DEBUG_RUN_DIR
    ts = time.strftime("%Y%m%d_%H%M%S")
    _DEBUG_RUN_DIR = os.path.join("cache", f"flow_run_{ts}")
    os.makedirs(_DEBUG_RUN_DIR, exist_ok=True)
    return _DEBUG_RUN_DIR


def _end_debug_run(success: bool) -> None:
    global _DEBUG_RUN_DIR
    try:
        keep = (os.getenv("FLOW_KEEP_DEBUG_ON_SUCCESS", "0").strip().lower() in TRUE_SET)
        if success and _DEBUG_RUN_DIR and not keep:
            shutil.rmtree(_DEBUG_RUN_DIR, ignore_errors=True)
    finally:
        _DEBUG_RUN_DIR = None


def _debug_dir() -> str:
    return _begin_debug_run()


def _dump_debug(driver: webdriver.Chrome, tag: str):
    try:
        base = _debug_dir()
        ts = int(time.time())
        os.makedirs(base, exist_ok=True)
        driver.save_screenshot(os.path.join(base, f"{tag}_{ts}.png"))
        with open(os.path.join(base, f"{tag}_{ts}.html"), "w", encoding="utf-8") as f:
            f.write(driver.page_source)
    except Exception:
        pass


def _first_visible(driver, candidates: List[Tuple[str, str]], timeout: int = 20):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        for by, sel in candidates:
            try:
                el = WebDriverWait(driver, 3).until(EC.visibility_of_element_located((by, sel)))
                if el and el.is_enabled():
                    return el
            except Exception as e:
                last = e
        time.sleep(0.25)
    if last:
        raise last
    return None


def _wait_overlays_gone(driver: webdriver.Chrome, timeout: int = 12):
    end = time.time() + timeout
    selectors = [
        "div[role='dialog']","div[aria-modal='true']","div[aria-busy='true']",
        "div[class*='overlay']","div[class*='scrim']","div[class*='backdrop']",
        "div[class*='modal']","div[class*='spinner']","div[class*='loading']",
        "div.sc-5ce3bf72-3"
    ]
    while time.time() < end:
        visible = False
        for sel in selectors:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if el.is_displayed():
                        visible = True; break
                except Exception:
                    pass
            if visible:
                break
        if not visible:
            return
        time.sleep(0.25)


def _safe_click(driver: webdriver.Chrome, el):
    driver.execute_script("arguments[0].scrollIntoView({behavior:'instant',block:'center'});", el)
    try:
        WebDriverWait(driver, 6).until(EC.element_to_be_clickable(el))
        el.click(); return
    except Exception:
        pass
    try:
        ActionChains(driver).move_to_element(el).pause(0.05).click().perform(); return
    except Exception:
        pass
    try:
        ActionChains(driver).move_to_element_with_offset(el, 1, 1).pause(0.05).click().perform(); return
    except Exception:
        pass
    driver.execute_script("arguments[0].click();", el)


def _pause_all_videos(driver: webdriver.Chrome):
    try:
        driver.execute_script("""
            const vids = Array.from(document.querySelectorAll('video'));
            for (const v of vids) { try { v.pause(); } catch(e){} }
            return vids.length;
        """)
    except Exception:
        pass


def _hover_latest_video(driver: webdriver.Chrome):
    try:
        vids = driver.find_elements(By.TAG_NAME, "video")
        vids = [v for v in vids if v.is_displayed()]
        if not vids:
            return None
        target = vids[-1]
        driver.execute_script("arguments[0].scrollIntoView({behavior:'instant',block:'center'});", target)
        ActionChains(driver).move_to_element(target).pause(0.12).perform()
        return target
    except Exception:
        return None


def _is_stuck_99(driver: webdriver.Chrome) -> bool:
    try:
        return bool(driver.execute_script("""
const hasText = !!Array.from(document.querySelectorAll('div,span,p'))
 .some(el => /(^|\\s)99%($|\\s)/.test((el.innerText||el.textContent||'').trim()));
const bars = Array.from(document.querySelectorAll('[role="progressbar"],progress'));
const hasAria = bars.some(b => {
  const v = (b.getAttribute('aria-valuenow')||b.value||'').toString();
  const n = parseInt(v,10);
  return !isNaN(n) && n >= 99;
});
return hasText || hasAria;
"""))
    except Exception:
        return False


def _has_generation_error(driver: webdriver.Chrome) -> bool:
    try:
        txts = [
            "Falha na geração", "Falha na gerac", "Falha na criação", "Falha ao gerar",
            "Generation failed", "Failed to generate", "Generation error"
        ]
        nodes = driver.find_elements(By.XPATH, "//div|//span|//button")
        for n in nodes:
            try:
                t = (n.text or "").strip()
                if t and any(x.lower() in t.lower() for x in txts):
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def _refresh(driver: webdriver.Chrome):
    try:
        driver.refresh()
        _first_visible(driver, [(By.CSS_SELECTOR, "body")], timeout=15)
        time.sleep(0.4)
    except Exception:
        pass

# =========================================================
# -------------------- Navegação no Flow ------------------
# =========================================================

def _open_flow_home(driver: webdriver.Chrome, entry_url: str):
    driver.get(entry_url)
    _first_visible(driver, [(By.CSS_SELECTOR, "body")], timeout=15)


def _unlock_home_scroll(driver: webdriver.Chrome):
    time.sleep(0.8)
    try:
        driver.execute_script("window.scrollBy(0, Math.floor(window.innerHeight*0.7));")
        time.sleep(0.2)
        driver.execute_script("window.scrollTo({top:0, behavior:'instant'});")
        time.sleep(0.1)
    except Exception:
        pass


def _click_novo_projeto(driver: webdriver.Chrome):
    _unlock_home_scroll(driver)
    try:
        btn = _first_visible(driver, [
            (By.XPATH, "//button[contains(normalize-space(.), 'Novo projeto')]"),
            (By.XPATH, "//button[contains(normalize-space(.), 'New project')]"),
        ], timeout=10)
        _safe_click(driver, btn); return
    except Exception:
        pass
    try:
        el = driver.execute_script(r"""
const matches = (el, txts) => {
  const norm = s => (s||'').replace(/\s+/g,' ').trim().toLowerCase();
  const t = norm(el.innerText || el.textContent || '');
  return txts.some(x => t.includes(norm(x)));
};
const labels = ['Novo projeto','Novo Projeto','New project','Create project'];
const btns = Array.from(document.querySelectorAll('button'));
for (const b of btns) { if (matches(b, labels)) return b; }
return null;
""")
        if el:
            _safe_click(driver, el); return
    except Exception:
        pass
    _dump_debug(driver, "novo_projeto_not_found")
    raise TimeoutException("Não consegui clicar em 'Novo projeto'.")

# ----------- Forçadores de UI (opcional) -----------

def _force_respostas_por_comando_1(driver: webdriver.Chrome):
    try:
        logger.info("🔧 Forçando 'Respostas por comando' para 1...")
        ajustes_btn = _first_visible(driver, [
            (By.XPATH, "//button[.//i[contains(., 'tune')]]"),
            (By.XPATH, "//button[contains(@aria-label, 'config') or contains(@aria-label, 'ajustes') or contains(@aria-label, 'settings')]")
        ], timeout=20)
        _safe_click(driver, ajustes_btn)
        select_btn = _first_visible(driver, [
            (By.XPATH, "//*[contains(normalize-space(.), 'Respostas por comando')]/ancestor::button"),
            (By.XPATH, "//button[contains(., 'Respostas por comando')]")
        ], timeout=10)
        _safe_click(driver, select_btn)
        opt_1 = _first_visible(driver, [
            (By.XPATH, "//div[@role='option' or @role='menuitem'][normalize-space()='1']"),
        ], timeout=10)
        _safe_click(driver, opt_1)
        WebDriverWait(driver, 10).until(
            EC.invisibility_of_element_located((By.XPATH, "//div[@role='option' or @role='menuitem']"))
        )
        time.sleep(0.4)
        logger.info("✅ 'Respostas por comando' definido como 1.")
    except Exception as e:
        logger.error("⚠️ Não consegui forçar 'Respostas por comando'=1 (%s).", e)
        _dump_debug(driver, "respostas_comando_fail")


def _force_model_veo3_fast(driver: webdriver.Chrome):
    logger.info("🔧 Selecionando modelo 'Veo 3 - Fast' (se disponível)…")
    try:
        model_menu_button = _first_visible(driver, [
            (By.XPATH, "//button[.//span[normalize-space()='Modelo']]"),
            (By.XPATH, "//span[normalize-space()='Modelo']/ancestor::button[1]")
        ], timeout=20)
        _safe_click(driver, model_menu_button)
        time.sleep(1.0)
        veo3_fast_option_container = _first_visible(driver, [
            (By.XPATH, "//div[@role='option'][.//span[normalize-space()='Veo 3 - Fast']]"),
            (By.XPATH, "//div[@role='option'][contains(., 'Veo 3 - Fast')]"),
        ], timeout=10)
        _safe_click(driver, veo3_fast_option_container)
        WebDriverWait(driver, 10).until(
            EC.text_to_be_present_in_element((By.XPATH, "//button[contains(., 'Modelo')]"), "Veo 3 - Fast")
        )
        logger.info("✅ Modelo 'Veo 3 - Fast' selecionado.")
        time.sleep(0.2)
    except Exception as e:
        logger.warning("⚠️ Não consegui confirmar 'Veo 3 - Fast': %s", e)
        _dump_debug(driver, "model_option_fail")

# ----------- Injeção de prompt -----------

_DEF_JS = r"""
const el = arguments[0]; const val = arguments[1] ?? "";
function setReactValue(input, v) {
  const desc = (input.tagName === 'TEXTAREA')
    ? Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')
    : Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
  if (desc && desc.set) { desc.set.call(input, v); } else { input.value = v; }
  input.dispatchEvent(new Event('input', {bubbles:true}));
  input.dispatchEvent(new Event('change', {bubbles:true}));
}
if (el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && (!el.type || el.type==='text'))) {
  el.focus(); setReactValue(el, val);
} else if (el.isContentEditable || el.getAttribute('contenteditable') === 'true') {
  el.focus(); el.innerText = val;
  el.dispatchEvent(new Event('input', {bubbles:true}));
  el.dispatchEvent(new Event('change', {bubbles:true}));
} else {
  el.focus(); try { el.value = val; } catch(e) {}
  el.dispatchEvent(new Event('input', {bubbles:true}));
  el.dispatchEvent(new Event('change', {bubbles:true}));
}
return true;
"""

def _set_text_multiline_js(driver: webdriver.Chrome, element, text: str) -> None:
    driver.execute_script(_DEF_JS, element, text)

def _read_current_text(driver: webdriver.Chrome, element) -> str:
    script = """
const el = arguments[0];
if (el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && (!el.type || el.type==='text'))) return el.value || "";
if (el.isContentEditable || el.getAttribute('contenteditable') === 'true') return el.innerText || el.textContent || "";
return el.value || el.textContent || "";
"""
    return (driver.execute_script(script, element) or "").strip()

def _submit_prompt(driver: webdriver.Chrome, prompt_text: str):
    composer = _first_visible(driver, [
        (By.CSS_SELECTOR, "textarea"),
        (By.XPATH, "//textarea[contains(@placeholder,'Crie um vídeo')]"),
        (By.CSS_SELECTOR, "[contenteditable='true']"),
    ], timeout=25)
    _set_text_multiline_js(driver, composer, prompt_text)
    cur = _read_current_text(driver, composer)
    if (cur or "").strip()[:1000] != (prompt_text or "").strip()[:1000]:
        logger.warning("⚠️ Prompt no campo difere do esperado (seguindo com o inserido).")
    submit_btn = _first_visible(driver, [
        (By.XPATH, "//button[@type='submit']"),
        (By.XPATH, "//button[.//i[contains(., 'arrow_forward')]]"),
        (By.XPATH, "//button[normalize-space()='Criar']"),
        (By.XPATH, "//button[normalize-space()='Create']"),
    ], timeout=10)
    _safe_click(driver, submit_btn)

# ----------- Download helpers -----------

def _find_download_button(driver: webdriver.Chrome):
    exact = ("//button[@aria-haspopup='menu' and .//i[normalize-space()='download'] "
             "and .//span[normalize-space()='Baixar']]")
    btns = driver.find_elements(By.XPATH, exact)
    btns = [b for b in btns if b.is_displayed() and b.is_enabled()]
    if btns:
        return btns[-1]
    icon_names = ["download","file_download","download_2","get_app","save_alt","arrow_downward"]
    xpats = [f"//button[.//i[normalize-space()='{n}']]" for n in icon_names] + [
        "//button[contains(@aria-label,'Baixar') or contains(@aria-label,'Download')]",
        "//a[contains(@aria-label,'Baixar') or contains(@aria-label,'Download')]",
    ]
    for xp in xpats:
        cand = driver.find_elements(By.XPATH, xp)
        cand = [b for b in cand if b.is_displayed() and b.is_enabled()]
        if cand:
            return cand[-1]
    return None

def _wait_download(download_dir: str, before: List[str], timeout: int = 300) -> str:
    end = time.time() + timeout
    before_set = set(map(os.path.abspath, before))
    while time.time() < end:
        files = [os.path.abspath(p) for p in glob.glob(os.path.join(download_dir, "*.mp4"))
                 if not p.endswith(".crdownload")]
        newf = [p for p in files if p not in before_set]
        if newf:
            newf.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            p = newf[0]
            s1 = os.path.getsize(p)
            time.sleep(1.0)
            s2 = os.path.getsize(p)
            if s1 == s2 and s1 > 0:
                return p
        time.sleep(0.7)
    raise TimeoutException("Timeout aguardando download do .mp4.")

def _click_download_720p_once(driver: webdriver.Chrome) -> bool:
    _wait_overlays_gone(driver, timeout=5)
    _pause_all_videos(driver)
    _hover_latest_video(driver)

    btn = _find_download_button(driver)
    if not btn:
        return False
    _safe_click(driver, btn)
    time.sleep(0.12)

    try:
        menu = WebDriverWait(driver, 6).until(EC.visibility_of_element_located((
            By.XPATH, "//div[@role='menu' and (@data-state='open' or @data-radix-menu-content)]"
        )))
    except Exception:
        return False

    candidates_xp = [
        ".//div[@role='menuitem'][.//i[normalize-space()='capture']]",
        ".//div[@role='menuitem'][contains(normalize-space(.), 'Tamanho original (720p)')]",
        ".//div[@role='menuitem'][contains(normalize-space(.), 'Original') and contains(normalize-space(.), '720p')]",
        ".//div[@role='menuitem'][contains(normalize-space(.), '720p')]",
    ]
    target = None
    for xp in candidates_xp:
        items = menu.find_elements(By.XPATH, xp)
        items = [it for it in items if it.is_displayed() and it.is_enabled()]
        if items:
            target = items[0]; break
    if target is None:
        for xp in [s.replace(".//", "//") for s in candidates_xp]:
            items = driver.find_elements(By.XPATH, xp)
            items = [it for it in items if it.is_displayed() and it.is_enabled()]
            if items:
                target = items[0]; break
    if not target:
        return False

    _safe_click(driver, target)
    return True

# ----------- Limpeza / Exclusão ----------------------

def _delete_card_menuitem_if_open(driver: webdriver.Chrome) -> bool:
    try:
        item = _first_visible(driver, [
            (By.XPATH, "//div[@role='menuitem' and .//i[normalize-space()='delete'] and contains(normalize-space(.), 'Excluir')]")
        ], timeout=3)
        _safe_click(driver, item)
        return True
    except Exception:
        return False

def _clear_existing_videos(driver: webdriver.Chrome, max_loops: int = 8) -> int:
    removed = 0
    for _ in range(max_loops):
        _wait_overlays_gone(driver, timeout=4)
        _pause_all_videos(driver)
        _hover_latest_video(driver)

        if _delete_card_menuitem_if_open(driver):
            removed += 1
            time.sleep(0.4)
            continue

        btns = driver.find_elements(By.XPATH, "//button[.//i[normalize-space()='more_vert']]")
        btns = [b for b in btns if b.is_displayed() and b.is_enabled()]
        if not btns:
            break
        clicked = False
        for btn in reversed(btns):
            try:
                _safe_click(driver, btn)
                time.sleep(0.12)
                if _delete_card_menuitem_if_open(driver):
                    removed += 1
                    clicked = True
                    time.sleep(0.5)
                    break
            except Exception:
                continue
        if not clicked:
            break

    logger.info("🧹 Limpeza: %s", f"{removed} vídeo(s) removido(s)" if removed else "nenhum vídeo anterior encontrado.")
    return removed

def _delete_current_video(driver: webdriver.Chrome, tries: int = 3, timeout: int = 20) -> bool:
    def _videos_visible() -> bool:
        vids = driver.find_elements(By.TAG_NAME, "video")
        return any(v.is_displayed() for v in vids)

    def _click(el) -> bool:
        try:
            el.click(); return True
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", el); return True
            except Exception:
                return False

    for _ in range(tries):
        if not _videos_visible():
            return True

        _wait_overlays_gone(driver, timeout=5)
        _pause_all_videos(driver)
        _hover_latest_video(driver)

        btn = _find_download_button(driver)
        if btn:
            _safe_click(driver, btn)
            time.sleep(0.12)
        else:
            more = driver.find_elements(By.XPATH, "//button[.//i[normalize-space()='more_vert']]")
            more = [b for b in more if b.is_displayed() and b.is_enabled()]
            if more:
                _safe_click(driver, more[-1])
                time.sleep(0.12)

        item = None
        try:
            menu = WebDriverWait(driver, 4).until(EC.visibility_of_element_located((
                By.XPATH, "//div[@role='menu' and (@data-state='open' or @data-radix-menu-content)]"
            )))
            items = menu.find_elements(By.XPATH, ".//div[@role='menuitem'][contains(normalize-space(.), 'Excluir') or .//i[normalize-space()='delete']]")
            items = [it for it in items if it.is_displayed() and it.is_enabled()]
            if items:
                item = items[-1]
        except Exception:
            items = driver.find_elements(By.XPATH, "//div[@role='menuitem'][contains(normalize-space(.), 'Excluir') or .//i[normalize-space()='delete']]")
            items = [it for it in items if it.is_displayed() and it.is_enabled()]
            if items:
                item = items[-1]

        if item and _click(item):
            try:
                WebDriverWait(driver, timeout).until(EC.invisibility_of_element_located((By.TAG_NAME, "video")))
            except Exception:
                pass
            return True
        time.sleep(0.9)

    vids = driver.find_elements(By.TAG_NAME, "video")
    return not any(v.is_displayed() for v in vids)

# ----------- Download com anti-stall -------------------

def _wait_download_720p(driver: webdriver.Chrome, download_dir: str, before: List[str], *, tag: str = "") -> str:
    total_wait = int(float(os.getenv("FLOW_DOWNLOAD_WAIT_SEC", "600")))
    probe = int(float(os.getenv("FLOW_PROBE_SEC", "10")))
    dl_attempts = int(float(os.getenv("FLOW_DL_ATTEMPTS", "3")))
    dl_grace = int(float(os.getenv("FLOW_DL_GRACE_SEC", "10")))
    stall_sec = int(float(os.getenv("FLOW_STALL_REFRESH_SEC", "35")))
    stall_max = int(float(os.getenv("FLOW_STALL_REFRESH_MAX", "2")))

    marks_env = (os.getenv("FLOW_STUCK_SHOTS", "120,240,360").strip() or "")
    marks: List[int] = []
    for p in marks_env.split(","):
        try:
            v = int(p); 
            if v > 0: marks.append(v)
        except Exception:
            pass
    marks = sorted(set(marks))
    taken: set[int] = set()

    deadline = time.time() + total_wait
    first_seen_ts = None
    seen_once_logged = False
    refreshes = 0
    last_stall_mark = None

    logger.info("⏳ Aguardando o player aparecer (com anti-stall 99%%)…")
    while time.time() < deadline:
        if _has_generation_error(driver):
            _dump_debug(driver, f"{tag or 'scene'}_generation_failed")
            raise RuntimeError(f"Falha na geração detectada [{tag or 'scene'}].")

        vids = [v for v in driver.find_elements(By.TAG_NAME, "video") if v.is_displayed()]
        if vids:
            break

        if _is_stuck_99(driver):
            if last_stall_mark is None:
                last_stall_mark = time.time()
            if (time.time() - last_stall_mark) >= stall_sec and refreshes < stall_max:
                logger.info("🔄 99%% ≥%ds — refresh (%d/%d)…", stall_sec, refreshes+1, stall_max)
                _dump_debug(driver, f"{tag or 'scene'}_stuck99_refresh{refreshes+1}")
                _refresh(driver)
                last_stall_mark = None
                refreshes += 1
                continue
        else:
            last_stall_mark = None

        elapsed_total = int((time.time() - (deadline - total_wait)))
        for m in marks:
            if elapsed_total >= m and m not in taken:
                _dump_debug(driver, f"{tag or 'scene'}_waiting_player_{m}s"); taken.add(m)
        time.sleep(1.0)

    if not driver.find_elements(By.TAG_NAME, "video"):
        _dump_debug(driver, f"{tag or 'scene'}_no_video_timeout")
        raise TimeoutException(f"Não apareceu nenhum <video> na página. [{tag or 'scene'}]")

    logger.info("⏳ Monitorando o player; probes a cada %ds (timeout total %ds)…", probe, total_wait)

    while time.time() < deadline:
        if _has_generation_error(driver):
            _dump_debug(driver, f"{tag or 'scene'}_generation_failed_during_probes")
            raise RuntimeError(f"Falha na geração detectada durante probes [{tag or 'scene'}].")

        _wait_overlays_gone(driver, timeout=5)
        _pause_all_videos(driver)
        _hover_latest_video(driver)

        btn = _find_download_button(driver)
        if btn:
            if first_seen_ts is None:
                first_seen_ts = time.time()
                seen_once_logged = False
                logger.info("👁️ 'Baixar' detectado. Vou aguardar %ds antes de tentar o clique…", probe)
            else:
                elapsed = time.time() - first_seen_ts
                if not seen_once_logged and elapsed < probe:
                    logger.info("… botão visível há %.1fs (aguardando %ds).", elapsed, probe)
                    seen_once_logged = True
                if elapsed >= probe:
                    for att in range(1, dl_attempts + 1):
                        logger.info("⚡ Tentativa de download %d/%d…", att, dl_attempts)
                        ok = _click_download_720p_once(driver)
                        if not ok:
                            logger.info("   • Não consegui abrir menu/clicar 720p; re-hover e tento de novo.")
                            _hover_latest_video(driver); time.sleep(0.5)
                            continue
                        logger.info("   • 720p clicado, aguardando arquivo por %ds…", dl_grace)
                        try:
                            return _wait_download(download_dir, before, timeout=dl_grace)
                        except TimeoutException:
                            logger.info("   • Arquivo não chegou em %ds — reinicio sequência.", dl_grace)
                            first_seen_ts = None
                            seen_once_logged = False
                            break
        else:
            first_seen_ts = None
            seen_once_logged = False

        if _is_stuck_99(driver) and refreshes < stall_max:
            if last_stall_mark is None:
                last_stall_mark = time.time()
            elif (time.time() - last_stall_mark) >= stall_sec:
                logger.info("🔄 99%% detectado durante probes — refresh (%d/%d)…", refreshes+1, stall_max)
                _dump_debug(driver, f"{tag or 'scene'}_stuck99_probe_refresh{refreshes+1}")
                _refresh(driver)
                last_stall_mark = None
                refreshes += 1
                continue
        else:
            last_stall_mark = None

        elapsed_total = int((time.time() - (deadline - total_wait)))
        for m in marks:
            if elapsed_total >= m and m not in taken:
                _dump_debug(driver, f"{tag or 'scene'}_waiting_dl_{m}s"); taken.add(m)
        time.sleep(probe)

    _dump_debug(driver, f"{tag or 'scene'}_download_timeout")
    raise TimeoutException(f"Timeout geral tentando efetuar o download 720p. [{tag or 'scene'}]")

# ----------- Apagar cartão atual (menu) ----------------

def _delete_card(driver: webdriver.Chrome):
    def _find_delete_item():
        xp = ("//div[@role='menuitem' and .//i[normalize-space()='delete'] "
              "and contains(normalize-space(.), 'Excluir')]")
        items = driver.find_elements(By.XPATH, xp)
        items = [it for it in items if it.is_displayed() and it.is_enabled()]
        return items[-1] if items else None
    try:
        item = _find_delete_item()
        if not item:
            more_btn = _first_visible(driver, [
                (By.XPATH, "//button[.//i[contains(., 'more_vert')]]"),
                (By.XPATH, "//button[@aria-label='mais opções']"),
                (By.XPATH, "//button[@aria-label='More options']"),
            ], timeout=8)
            _safe_click(driver, more_btn)
            item = _find_delete_item()
        if item:
            _safe_click(driver, item)
            logger.info("🗑️ Vídeo excluído no Flow (pós-download).")
    except Exception as e:
        logger.debug("Não foi possível excluir no Flow (seguindo): %s", e)

# =========================================================
# --------------------- ffprobe (áudio) -------------------
# =========================================================

def _ffprobe_path() -> Optional[str]:
    p = shutil.which("ffprobe")
    if not p:
        logger.warning("ffprobe não encontrado no PATH — pulando checagem de áudio (FLOW_CHECK_AUDIO=0 para ocultar).")
    return p

def _has_audio_ffprobe(video_path: str) -> Optional[bool]:
    ffprobe = _ffprobe_path()
    if not ffprobe or not os.path.isfile(video_path):
        return None
    try:
        cmd = [ffprobe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", video_path]
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=10)
        return bool(out.decode("utf-8", "ignore").strip())
    except Exception as e:
        logger.debug("ffprobe falhou ao checar áudio (%s): %s", video_path, e)
        return None

def _should_check_audio() -> bool:
    v = (os.getenv("FLOW_CHECK_AUDIO", "1").strip() or "1").lower()
    return v in TRUE_SET

def _noaudio_retries() -> int:
    try:
        return max(0, int(float(os.getenv("FLOW_NOAUDIO_RETRIES", "2"))))
    except Exception:
        return 2

# =========================================================
# ------------------- Créditos (opcional) -----------------
# =========================================================

def _check_and_log_credits(driver: webdriver.Chrome, stage: str) -> Optional[int]:
    try:
        logger.info("💰 Verificando créditos de IA (%s)...", stage)
        profile_icon = _first_visible(driver, [
            (By.XPATH, "//button[.//img[contains(@alt, 'perfil do usuário')]]"),
            (By.XPATH, "//button[.//div[normalize-space()='PRO']]")
        ], timeout=15)
        _safe_click(driver, profile_icon)
        credit_element = _first_visible(driver, [
            (By.XPATH, "//a[contains(., 'Créditos de IA') or contains(., 'AI Credits')]")
        ], timeout=10)
        text = credit_element.text or ""
        match = re.search(r"(\d+)", text)
        credits = int(match.group(1)) if match else None
        if credits is not None:
            logger.info("💰 Créditos de IA (%s): %d", stage, credits)
        else:
            logger.warning("Não consegui extrair o número de créditos: '%s'", text)
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        time.sleep(0.3)
        return credits
    except Exception as e:
        logger.warning("Não foi possível verificar os créditos de IA: %s", e)
        try:
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        except Exception:
            pass
        return None

# =========================================================
# ----------------- Autenticação (robusto) ----------------
# =========================================================

def _get_success_xpaths_from_env() -> List[str]:
    raw = os.getenv("FLOW_LOGIN_SUCCESS_SELECTORS", "")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if parts:
        return parts
    return [
        "//button[contains(., 'Novo projeto')]",
        "//button[contains(., 'New project')]",
        "//*[@data-testid='new-project-button']",
        "//a[contains(@href, 'new-project')]",
        "//button[contains(., 'Create') and contains(., 'project')]",
        "//*[contains(@class, 'new-project')]",
    ]

def _auth_is_login_gate(driver) -> bool:
    try:
        url = (driver.current_url or "").lower()
        if any(k in url for k in ["accounts.google.", "/signin", "sign-in", "login", "oauth"]):
            return True
        html = (driver.page_source or "").lower()
        return any(k in html for k in [
            "sign in with google", "continuar com o google", "entrar com o google",
            "try signing in with a different account"
        ])
    except Exception:
        return False

def _auth_has_session_cookie(driver) -> bool:
    try:
        for ck in driver.get_cookies() or []:
            name = (ck.get("name") or "").lower()
            if "next" in name and "auth" in name:
                return True
            if "session" in name and ck.get("value"):
                return True
        return False
    except WebDriverException:
        return False

def _auth_is_logged_in(driver) -> bool:
    if _auth_has_session_cookie(driver):
        return True
    try:
        url = (driver.current_url or "").lower()
    except Exception:
        url = ""
    if any(k in url for k in ["/projects", "/studio", "/home", "/dashboard"]):
        return True
    for xp in _get_success_xpaths_from_env():
        try:
            el = driver.find_element("xpath", xp)
            if el:
                return True
        except NoSuchElementException:
            continue
        except WebDriverException:
            break
    return False

def _auth_dump(driver, tag="signin"):
    try:
        debug_dir = _debug_dir()
        os.makedirs(debug_dir, exist_ok=True)
        with open(os.path.join(debug_dir, f"{tag}_{int(time.time())}.html"), "w", encoding="utf-8") as f:
            f.write(driver.page_source or "")
        driver.save_screenshot(os.path.join(debug_dir, f"{tag}_{int(time.time())}.png"))
    except Exception:
        pass

def _auth_export_cookies(driver, cookie_path: str) -> None:
    try:
        cookies = driver.get_cookies() or []
        _write_netscape_cookies(cookie_path, cookies)
        logger.info("🍪 Cookies exportados para: %s (%d itens)", cookie_path, len(cookies))
    except Exception as e:
        logger.warning("Falha ao exportar cookies para %s: %s", cookie_path, e)

def _env_interactive_allowed() -> bool:
    flow = os.getenv("FLOW_INTERACTIVE_LOGIN", "").strip().lower()
    veo  = os.getenv("VEO3_ALLOW_INTERACTIVE_LOGIN", "").strip().lower()
    def truthy(v: str) -> Optional[bool]:
        if v == "": return None
        return v in TRUE_SET
    v = truthy(flow)
    if v is None:
        v = truthy(veo)
        if v is None:
            return True  # default: permitido
    return bool(v)

def _env_login_timeout_minutes() -> int:
    try:
        mins = int(float(os.getenv("FLOW_LOGIN_MAX_MINUTES", "").strip()))
        if mins > 0: return mins
    except Exception:
        pass
    try:
        secs = int(float(os.getenv("VEO3_INTERACTIVE_LOGIN_TIMEOUT_SEC", "").strip()))
        if secs > 0: return max(1, secs // 60)
    except Exception:
        pass
    return 10

def auth_wait_login_and_export(driver, cookie_path: str, max_minutes: int = 10) -> bool:
    deadline = time.time() + max(1, int(max_minutes)) * 60
    last_log = 0.0
    while time.time() < deadline:
        try:
            if _auth_is_logged_in(driver):
                logger.info("✅ Login reconhecido (cookies/URL/seletor). Exportando cookies…")
                _auth_export_cookies(driver, cookie_path)
                time.sleep(1.0)
                return True
            if _auth_is_login_gate(driver):
                now = time.time()
                if now - last_log > 10:
                    logger.info("⏳ Aguardando sua autenticação… (restante ~%ds)", int(deadline - now))
                    last_log = now
            else:
                # Não é claramente login nem logado — empurra para área interna
                try:
                    driver.get("https://www.tiktok.com/tiktokstudio/upload")
                except Exception:
                    pass
            time.sleep(1.0)
        except WebDriverException:
            break
    logger.error("[AUTH] Tempo esgotado aguardando autenticação manual.")
    return False

def _auth_ensure(driver, project_url: str, cookies_file: str, *, headless: bool) -> webdriver.Chrome:
    """
    Garante autenticação:
    - Se já estiver logado, retorna o próprio driver.
    - Se cair no gate e login interativo for permitido, abre VISÍVEL para logar,
      exporta cookies e:
        * por padrão, CONTINUA visível nesta execução (evita crash do headless com mesmo perfil).
        * se FLOW_REOPEN_HEADLESS_AFTER_AUTH=1, reabre headless com OUTRO perfil e injeta cookies.
    """
    try:
        # Primeiro, cheque se já está logado (evita falso-positivo de gate)
        if _auth_is_logged_in(driver):
            logger.info("🔓 Sessão já autenticada detectada. Prosseguindo sem fluxo de login.")
            return driver

        if not _auth_is_login_gate(driver):
            return driver

        _auth_dump(driver, "signin_detected")
        logger.error("❌ Gate de login do Flow detectado — é necessário renovar os cookies.")

        if not _env_interactive_allowed():
            raise RuntimeError("Gate detectado e login interativo desabilitado. "
                               "Habilite FLOW_INTERACTIVE_LOGIN=1 ou atualize os cookies manualmente.")

        timeout_min = _env_login_timeout_minutes()

        try:
            driver.quit()
        except Exception:
            pass

        logger.warning("🔐 Abrindo Chrome VISÍVEL para autenticação manual (até %d min)…", timeout_min)
        drv2 = _chrome(download_dir=os.path.abspath("videos"), headless=False)
        drv2.get(project_url)

        # (Opcional) clica "Sign in with Google"
        try:
            btn = WebDriverWait(drv2, 10).until(EC.element_to_be_clickable((
                By.XPATH, "//form[contains(@action,'/fx/api/auth/signin/google')]//button"
                          "|//button[contains(., 'Sign in with Google') or contains(., 'Entrar com o Google')]"
            )))
            try:
                btn.click()
            except Exception:
                try:
                    drv2.execute_script("arguments[0].click();", btn)
                except Exception:
                    pass
        except Exception:
            pass

        ok = auth_wait_login_and_export(drv2, cookie_path=cookies_file, max_minutes=timeout_min)
        if not ok:
            _auth_dump(drv2, "signin_timeout")
            try:
                drv2.quit()
            except Exception:
                pass
            raise TimeoutException("LOGIN_TIMEOUT: não consegui confirmar o login no tempo limite.")

        logger.info("🟢 [AUTH] Cookies renovados. Prosseguindo.")

        # Se o usuário pediu headless, podemos:
        # (a) continuar VISÍVEL (padrão, confiável) OU
        # (b) reabrir headless com OUTRO perfil (se habilitado).
        reopen = (os.getenv("FLOW_REOPEN_HEADLESS_AFTER_AUTH", "0").strip().lower() in TRUE_SET)
        if headless and reopen:
            # reabrir headless com perfil 'headless' separado
            try:
                drv2.quit()
            except Exception:
                pass
            drv3 = _chrome(download_dir=os.path.abspath("videos"), headless=True)
            try:
                cookies = _read_netscape_cookies(cookies_file)
                if cookies:
                    _add_cookies_multi_domain(drv3, cookies)
            except Exception:
                pass
            drv3.get(project_url)
            return drv3
        else:
            if headless:
                logger.info("👁️ Continuando com navegador VISÍVEL apenas nesta execução "
                            "(defina FLOW_REOPEN_HEADLESS_AFTER_AUTH=1 para reabrir headless).")
            return drv2
    except Exception as e:
        logger.error("[AUTH] Falha ao garantir autenticação: %s", e)
        return driver

# =========================================================
# ---------------------- API pública ----------------------
# =========================================================

def generate_single_via_flow(
    prompt_text: str,
    out_path: str,
    project_url: str | None = None,
    idioma: str = "en",
    headless: bool = True,
    timeout_sec: int = 480,
    *,
    # ---- compat extras (não obrigatórios) ----
    flow_url: str | None = None,
    cookies_file: str | None = None,   # cookies do FLOW (override)
    download_dir: str | None = None,   # override do diretório de download
    persona: str | None = None,
    **_
) -> str:
    """
    Gera 1 vídeo no Google Flow (Veo3) a partir de um prompt.
    Compatível com chamadas que passam flow_url, cookies_file, download_dir, persona, etc.
    """
    # === resolução de URL do projeto (Flow) ===
    project_url = project_url or flow_url or os.getenv("FLOW_URL") or ""
    if not project_url:
        raise RuntimeError("FLOW_URL (project_url/flow_url) não definido. Configure no .env ou passe como argumento.")

    # === normalização de idioma / logs ===
    lang = normalize_lang(idioma)

    # ATENÇÃO: este é o caminho de cookies do TIKTOK (apenas para log/consistência de saída)
    tiktok_cookies_file = cookies_path_for(lang)

    # Cookies do FLOW: usa o parâmetro se vier; senão, padrão do projeto
    flow_cookie = cookies_file if cookies_file else flow_cookies_file()

    logger.info("🌍 Idioma=%s | TikTok cookies=%s | Flow cookies=%s",
                lang, tiktok_cookies_file, flow_cookie)

    # === diretório de download (padrão: pasta do out_path) ===
    download_dir = os.path.abspath(download_dir or (os.path.dirname(out_path) or "."))
    os.makedirs(download_dir, exist_ok=True)
    _begin_debug_run()

    driver = _chrome(download_dir=download_dir, headless=headless)
    try:
        logger.info("Destino do arquivo: %s", out_path)

        cookies = _read_netscape_cookies(flow_cookie)
        if cookies:
            _add_cookies_multi_domain(driver, cookies)
        driver.get(project_url)
        time.sleep(0.8)

        if _auth_is_logged_in(driver):
            logger.info("🔓 Sessão reconhecida — seguindo.")
        elif _auth_is_login_gate(driver):
            driver = _auth_ensure(driver, project_url, flow_cookie, headless=headless)

        if "/tools/flow" in project_url and "/project/" not in project_url:
            logger.info("Abrindo Flow e criando 'Novo projeto'…")
            _open_flow_home(driver, project_url)
            _click_novo_projeto(driver)

        creditos_iniciais = _check_and_log_credits(driver, "início")
        if creditos_iniciais is not None and creditos_iniciais < CREDIT_COST_PER_SCENE:
            raise RuntimeError("Créditos insuficientes para gerar 1 cena.")

        _force_respostas_por_comando_1(driver)
        _force_model_veo3_fast(driver)

        max_regen = _noaudio_retries()
        scene_tag = os.path.splitext(os.path.basename(out_path))[0] or "scene"
        for attempt in range(1, max_regen + 2):
            _clear_existing_videos(driver)
            before = glob.glob(os.path.join(download_dir, "*.mp4"))

            logger.info("Enviando prompt…")
            _submit_prompt(driver, prompt_text)
            logger.info("Prompt enviado; aguardando opção de download…")

            mp4_tmp = _wait_download_720p(driver, download_dir, before, tag=f"{scene_tag}_attempt{attempt}")
            logger.info("Vídeo baixado: %s", mp4_tmp)

            ok = True
            if _should_check_audio():
                has_aud = _has_audio_ffprobe(mp4_tmp)
                if has_aud is False:
                    ok = False
                    logger.warning("⚠️ Arquivo sem trilha de áudio. Vou excluir e re-gerar (tentativa %d).", attempt)
                elif has_aud is None:
                    logger.debug("Não foi possível confirmar áudio via ffprobe — seguindo.")
                else:
                    logger.info("✅ Áudio presente no arquivo gerado.")

            if ok:
                _delete_card(driver)
                if os.path.abspath(mp4_tmp) != os.path.abspath(out_path):
                    if os.path.exists(out_path):
                        os.remove(out_path)
                    shutil.move(mp4_tmp, out_path)
                break
            else:
                try: _delete_card(driver)
                except Exception: pass
                try: os.remove(mp4_tmp)
                except Exception: pass
                if attempt >= (max_regen + 1):
                    logger.error("❌ Sem áudio após %d tentativa(s). Seguiremos com o último arquivo.", attempt)
                    if os.path.abspath(mp4_tmp) != os.path.abspath(out_path):
                        if os.path.exists(out_path):
                            os.remove(out_path)
                        shutil.move(mp4_tmp, out_path)
                    break

        _check_and_log_credits(driver, "fim")
        try:
            if _delete_current_video(driver):
                logger.info("🧹 Limpeza final: vídeo removido do projeto.")
            else:
                logger.warning("⚠️ Limpeza final: não consegui excluir o vídeo ativo.")
        except Exception as e:
            logger.debug("Limpeza final falhou: %s", e)

        _end_debug_run(success=True)
        return out_path

    except Exception:
        _dump_debug(driver, "exception_single")
        _end_debug_run(success=False)
        raise
    finally:
        try: driver.quit()
        except Exception: pass


def generate_many_via_flow(
    prompts: List[str],
    out_paths: List[str],
    project_url: str | None = None,
    idioma: str = "en",
    headless: bool = True,
    timeout_sec: int = 480,
    *,
    # ---- compat extras (não obrigatórios) ----
    flow_url: str | None = None,
    cookies_file: str | None = None,   # cookies do FLOW (override)
    download_dir: str | None = None,   # override do diretório de download
    persona: str | None = None,
    **_
) -> List[str]:
    # === resolução de URL do projeto (Flow) ===
    project_url = project_url or flow_url or os.getenv("FLOW_URL") or ""
    if not project_url:
        raise RuntimeError("FLOW_URL (project_url/flow_url) não definido. Configure no .env ou passe como argumento.")

    lang = normalize_lang(idioma)
    # Caminho de cookies do TikTok (apenas para log)
    tiktok_cookies_file = cookies_path_for(lang)
    # Cookies do Flow
    flow_cookie = cookies_file if cookies_file else flow_cookies_file()
    logger.info("🌍 Idioma=%s | TikTok cookies=%s | Flow cookies=%s",
                lang, tiktok_cookies_file, flow_cookie)

    if len(prompts) != len(out_paths):
        raise ValueError("prompts e out_paths precisam ter o mesmo tamanho.")

    download_dir = os.path.abspath(download_dir or (os.path.dirname(out_paths[0]) or "."))
    os.makedirs(download_dir, exist_ok=True)
    _begin_debug_run()

    driver = _chrome(download_dir=download_dir, headless=headless)
    try:
        cookies = _read_netscape_cookies(flow_cookie)
        if cookies:
            _add_cookies_multi_domain(driver, cookies)
        driver.get(project_url)
        time.sleep(0.8)

        if _auth_is_logged_in(driver):
            logger.info("🔓 Sessão reconhecida — seguindo.")
        elif _auth_is_login_gate(driver):
            driver = _auth_ensure(driver, project_url, flow_cookie, headless=headless)

        if "/tools/flow" in project_url and "/project/" not in project_url:
            logger.info("Abrindo Flow e criando 'Novo projeto'…")
            _open_flow_home(driver, project_url)
            _click_novo_projeto(driver)

        creditos_iniciais = _check_and_log_credits(driver, "início")
        if creditos_iniciais is not None:
            num = len(prompts)
            custo_total = num * CREDIT_COST_PER_SCENE
            if creditos_iniciais < custo_total:
                raise RuntimeError(f"Créditos insuficientes para gerar {num} cenas.")

        _force_respostas_por_comando_1(driver)
        _force_model_veo3_fast(driver)

        results: List[str] = []
        max_regen = _noaudio_retries()

        for idx, (prompt_text, out_path) in enumerate(zip(prompts, out_paths), start=1):
            base_tag = f"scene{idx}"
            logger.info("Arquivo de destino: %s", out_path)

            success = False
            for attempt in range(1, max_regen + 2):
                _clear_existing_videos(driver)
                before = glob.glob(os.path.join(download_dir, "*.mp4"))

                logger.info("Enviando prompt…")
                _submit_prompt(driver, prompt_text)
                logger.info("Prompt enviado; aguardando opção de download…")

                try:
                    mp4_tmp = _wait_download_720p(driver, download_dir, before, tag=f"{base_tag}_attempt{attempt}")
                except Exception as e:
                    logger.warning("Cena %d: erro ao aguardar download (%s). Re-tentando…", idx, e)
                    _dump_debug(driver, f"{base_tag}_attempt{attempt}_wait_err")
                    continue

                logger.info("Vídeo baixado: %s", mp4_tmp)

                ok = True
                if _should_check_audio():
                    has_aud = _has_audio_ffprobe(mp4_tmp)
                    if has_aud is False:
                        ok = False
                        logger.warning("⚠️ Cena %d: sem áudio. Re-gerando (tentativa %d)…", idx, attempt)
                    elif has_aud is None:
                        logger.debug("Cena %d: não consegui confirmar áudio via ffprobe — seguindo.", idx)
                    else:
                        logger.info("Cena %d: ✅ Áudio presente.", idx)

                if ok:
                    _delete_card(driver)
                    if os.path.abspath(mp4_tmp) != os.path.abspath(out_path):
                        if os.path.exists(out_path):
                            os.remove(out_path)
                        shutil.move(mp4_tmp, out_path)
                    results.append(out_path)
                    success = True
                    break
                else:
                    try: _delete_card(driver)
                    except Exception: pass
                    try: os.remove(mp4_tmp)
                    except Exception: pass
                    if attempt >= (max_regen + 1):
                        logger.error("Cena %d: ❌ Sem áudio após %d tentativas. Usando o último arquivo.", idx, attempt)
                        if os.path.abspath(mp4_tmp) != os.path.abspath(out_path):
                            if os.path.exists(out_path):
                                os.remove(out_path)
                            shutil.move(mp4_tmp, out_path)
                        results.append(out_path)
                        success = True
                        break

            if not success:
                _dump_debug(driver, f"{base_tag}_exception_final")
                raise RuntimeError(f"Falha ao gerar a cena {idx} (erro persistente).")

        try:
            _clear_existing_videos(driver)
            _check_and_log_credits(driver, "fim")
            if _delete_current_video(driver):
                logger.info("🧹 Limpeza final: projeto ficou sem vídeos antes de fechar.")
            else:
                logger.warning("⚠️ Limpeza final: não consegui confirmar a remoção do vídeo ativo.")
        except Exception as e:
            logger.debug("Limpeza final falhou: %s", e)

        logger.info("✅ Todas as %d cena(s) foram geradas e baixadas.", len(results))
        _end_debug_run(success=True)
        return results

    except Exception:
        _dump_debug(driver, "exception_many")
        _end_debug_run(success=False)
        raise
    finally:
        try: driver.quit()
        except Exception: pass
