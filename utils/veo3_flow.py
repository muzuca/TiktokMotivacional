# utils/veo3_flow.py
# Automação do Google Labs Flow (Veo3) via Selenium + cookies Netscape.
# Destaques:
# - Cookies Netscape injetados por domínio (evita login).
# - Abre "Novo projeto" com rotina de scroll de destrava (evita misclick).
# - Força "Respostas por comando"=1 (apenas uma vez).
# - LIMPEZA: remove todos os vídeos já existentes no projeto antes de cada cena.
# - Prompt multiline via JS (sem ENTER quebrar texto).
# - Probes periódicos (hover/pausa) até ver "Baixar".
# - Clique robusto no menu Radix: Baixar → "Tamanho original (720p)".
# - Download com retries e grace de chegada do arquivo.
# - Detecta "travou em 99%" e faz refresh automático.
# - Exclui o cartão após cada download.
# - Novo: generate_many_via_flow(prompts[]) mantém UMA sessão para N cenas.
# - NEW: _delete_current_video() e limpeza FINAL garantida antes de fechar o navegador.

from __future__ import annotations
import os, time, glob, shutil, logging
from typing import List, Dict, Tuple

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver import ActionChains
from selenium.common.exceptions import TimeoutException

logger = logging.getLogger(__name__)

# ====================== Cookies (Netscape) ======================
def _read_netscape_cookies(path: str) -> List[Dict]:
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(f"Cookie file não encontrado: {path}")
    items: List[Dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith(("#", "!")):
                continue
            parts = ln.split("\t")
            if len(parts) != 7:
                continue
            domain, _flag, pathv, secure_s, expiry, name, value = parts
            items.append({
                "domain": (domain or "").strip(),
                "path": (pathv or "/").strip(),
                "secure": (secure_s.upper() == "TRUE"),
                "expiry": int(expiry) if str(expiry).isdigit() else None,
                "name": name.strip(),
                "value": value.strip(),
            })
    if not items:
        raise RuntimeError(f"Nenhum cookie válido lido de: {path}")
    return items

def _group_cookie_domains(cookies: List[Dict]) -> List[str]:
    hosts = set()
    for ck in cookies:
        d = (ck.get("domain") or "").strip().lstrip(".")
        if d:
            hosts.add(d)
    return sorted(hosts, key=lambda h: (h.count("."), len(h)))

def _add_cookies_multi_domain(driver: webdriver.Chrome, cookies: List[Dict]) -> None:
    for host in _group_cookie_domains(cookies):
        base = f"https://{host}/"
        try:
            driver.get(base)
            time.sleep(0.6)
        except Exception:
            continue
        for ck in cookies:
            if (ck.get("domain") or "").lstrip(".") != host:
                continue
            c = {
                "name": ck["name"],
                "value": ck["value"],
                "path": (ck.get("path") or "/"),
                "secure": bool(ck.get("secure", False)),
            }
            if ck.get("expiry"):
                c["expiry"] = int(ck["expiry"])
            try:
                driver.add_cookie(c)
            except Exception as e:
                logger.debug("Cookie ignorado em %s (%s): %s", host, ck.get("name"), e)

# ====================== Chrome ======================
def _chrome(download_dir: str, headless: bool = True) -> webdriver.Chrome:
    os.makedirs(download_dir, exist_ok=True)
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--log-level=3")
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1366,900")
    opts.add_experimental_option("prefs", {
        "download.default_directory": os.path.abspath(download_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
        "profile.default_content_setting_values.automatic_downloads": 1,
    })
    return webdriver.Chrome(options=opts)

# ====================== Util / Esperas ======================
def _first_visible(driver, candidates: List[Tuple[str,str]], timeout: int = 20):
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
            s1 = os.path.getsize(p); time.sleep(1.0); s2 = os.path.getsize(p)
            if s1 == s2 and s1 > 0:
                return p
        time.sleep(0.7)
    raise TimeoutException("Timeout aguardando download do .mp4.")

def _dump_debug(driver: webdriver.Chrome, tag: str):
    try:
        os.makedirs("cache", exist_ok=True)
        driver.save_screenshot(os.path.join("cache", f"flow_{tag}.png"))
        with open(os.path.join("cache", f"flow_{tag}.html"), "w", encoding="utf-8") as f:
            f.write(driver.page_source)
    except Exception:
        pass

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

def _wait_overlays_gone(driver: webdriver.Chrome, timeout: int = 12):
    end = time.time() + timeout
    selectors = [
        "div[role='dialog']","div[aria-modal='true']","div[aria-busy='true']",
        "div[class*='overlay']","div[class*='scrim']","div[class*='backdrop']",
        "div[class*='modal']","div[class*='spinner']","div[class*='loading']",
        "div.sc-5ce3bf72-3"
    ]
    while time.time() < end:
        any_vis = False
        for sel in selectors:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if el.is_displayed():
                        any_vis = True; break
                except Exception:
                    pass
            if any_vis: break
        if not any_vis: return
        time.sleep(0.25)

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
        if not vids: return None
        target = vids[-1]
        driver.execute_script("arguments[0].scrollIntoView({behavior:'instant',block:'center'});", target)
        ActionChains(driver).move_to_element(target).pause(0.12).perform()
        return target
    except Exception:
        return None

# ====== Stall (99%) handling ======
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

def _refresh(driver: webdriver.Chrome):
    try:
        driver.refresh()
        _first_visible(driver, [(By.CSS_SELECTOR, "body")], timeout=15)
        time.sleep(0.5)
    except Exception:
        pass

# ====================== Home helpers ======================
def _open_flow_home(driver: webdriver.Chrome, entry_url: str):
    driver.get(entry_url)
    _first_visible(driver, [(By.CSS_SELECTOR, "body")], timeout=15)

def _unlock_home_scroll(driver: webdriver.Chrome):
    """Mitiga layout primeiro paint: espera e faz scroll down/up para liberar o botão 'Novo projeto'."""
    time.sleep(1.2)
    try:
        driver.execute_script("window.scrollBy(0, Math.floor(window.innerHeight*0.7));")
        time.sleep(0.25)
        driver.execute_script("window.scrollTo({top:0, behavior:'instant'});")
        time.sleep(0.2)
    except Exception:
        pass

def _click_novo_projeto(driver: webdriver.Chrome):
    """
    Clica especificamente em botões/elementos de texto 'Novo projeto'.
    Evita clicar em <a href="/project/..."> de cartões já existentes.
    """
    _unlock_home_scroll(driver)
    try:
        btn = _first_visible(driver, [
            (By.XPATH, "//button[contains(normalize-space(.), 'Novo projeto')]"),
            (By.XPATH, "//button[contains(normalize-space(.), 'Novo Projeto')]"),
            (By.XPATH, "//button[contains(normalize-space(.), 'New project') or contains(normalize-space(.), 'Create project')]"),
        ], timeout=10)
        _safe_click(driver, btn); return
    except Exception:
        pass
    # alternativa: buscar qualquer botão com label 'Novo projeto'
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
        if el: _safe_click(driver, el); return
    except Exception:
        pass

    _dump_debug(driver, "novo_projeto_not_found")
    raise TimeoutException("Não consegui clicar em 'Novo projeto'.")

def _force_respostas_por_comando_1(driver: webdriver.Chrome):
    ajustes_btn = _first_visible(driver, [
        (By.XPATH, "//button[.//i[contains(., 'tune')]]"),
        (By.XPATH, "//button[contains(@aria-label, 'config') or contains(@aria-label, 'ajustes') or contains(@aria-label, 'settings')]"),
    ], timeout=20)
    _safe_click(driver, ajustes_btn)
    select_btn = _first_visible(driver, [
        (By.XPATH, "//*[contains(normalize-space(.), 'Respostas por comando')]/following::button[1]"),
        (By.XPATH, "//div[contains(., 'Respostas por comando')]//button"),
    ], timeout=10)
    _safe_click(driver, select_btn)
    opt_1 = _first_visible(driver, [
        (By.XPATH, "//div[@role='option' or @role='menuitem'][normalize-space()='1']"),
        (By.XPATH, "//*[normalize-space()='1']"),
    ], timeout=10)
    _safe_click(driver, opt_1)
    try: driver.find_element(By.TAG_NAME, "body").click()
    except Exception: pass

# ----------- Injeção segura do prompt -----------
def _set_text_multiline_js(driver: webdriver.Chrome, element, text: str) -> None:
    script = r"""
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
    driver.execute_script(script, element, text)

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

def _click_download_720p_once(driver: webdriver.Chrome) -> bool:
    _wait_overlays_gone(driver, timeout=5)
    _pause_all_videos(driver)
    _hover_latest_video(driver)

    btn = _find_download_button(driver)
    if not btn:
        return False
    _safe_click(driver, btn)
    time.sleep(0.15)

    # menu Radix aberto (role="menu", data-state="open")
    try:
        menu = WebDriverWait(driver, 6).until(EC.visibility_of_element_located((
            By.XPATH, "//div[@role='menu' and (@data-state='open' or @data-radix-menu-content)]"
        )))
    except Exception:
        return False

    candidates_xp = [
        ".//div[@role='menuitem'][.//i[normalize-space()='capture']]",  # ícone do 720p
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

# ----------- Limpeza de vídeos existentes no projeto -----------
def _delete_card_menuitem_if_open(driver: webdriver.Chrome) -> bool:
    """Se o menu 'Excluir' estiver visível, clica e retorna True."""
    try:
        item = _first_visible(driver, [
            (By.XPATH, "//div[@role='menuitem' and .//i[normalize-space()='delete'] and contains(normalize-space(.), 'Excluir')]"),
        ], timeout=3)
        _safe_click(driver, item)
        return True
    except Exception:
        return False

def _clear_existing_videos(driver: webdriver.Chrome, max_loops: int = 8) -> int:
    """
    Remove todos os cartões de vídeo visíveis no projeto, antes de gerar um novo.
    Itera pelos botões 'more_vert' (mais opções) e aciona 'Excluir'.
    """
    removed = 0
    for _ in range(max_loops):
        _wait_overlays_gone(driver, timeout=4)
        _pause_all_videos(driver)
        _hover_latest_video(driver)

        # tenta direto clicar em Excluir se um menu já estiver aberto
        if _delete_card_menuitem_if_open(driver):
            removed += 1
            time.sleep(0.5)
            continue

        # localiza botões de "mais opções" dos cartões (ícone more_vert)
        btns = driver.find_elements(By.XPATH, "//button[.//i[normalize-space()='more_vert']]")
        btns = [b for b in btns if b.is_displayed() and b.is_enabled()]
        if not btns:
            break

        # clique do último para o primeiro (cartão mais recente costuma ser o último)
        clicked_any = False
        for btn in reversed(btns):
            try:
                _safe_click(driver, btn)
                time.sleep(0.15)
                if _delete_card_menuitem_if_open(driver):
                    removed += 1
                    clicked_any = True
                    time.sleep(0.6)
                    break  # volta ao loop principal para recalcular lista
            except Exception:
                continue

        if not clicked_any:
            break

    if removed:
        logger.info("🧹 Limpeza: %d vídeo(s) removido(s) antes de gerar.", removed)
    else:
        logger.info("🧹 Limpeza: nenhum vídeo anterior encontrado.")
    return removed

# ----------- Apagar o vídeo ATUAL pelo mesmo menu do download (mais resiliente) -----------
def _delete_current_video(driver: webdriver.Chrome, tries: int = 3, timeout: int = 20) -> bool:
    """
    Abre o menu do vídeo atual (via botão 'Baixar' ou 'more_vert') e clica em 'Excluir'.
    Retorna True se conseguir excluir ou se não houver vídeo na tela.
    """
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

    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    import time

    for attempt in range(tries):
        if not _videos_visible():
            return True

        _wait_overlays_gone(driver, timeout=5)
        _pause_all_videos(driver)
        _hover_latest_video(driver)

        # Tenta abrir o mesmo menu usado para "Baixar"
        btn = _find_download_button(driver)
        if btn:
            _safe_click(driver, btn)
            time.sleep(0.15)
        else:
            # fallback: "mais opções"
            more = driver.find_elements(By.XPATH, "//button[.//i[normalize-space()='more_vert']]")
            more = [b for b in more if b.is_displayed() and b.is_enabled()]
            if more:
                _safe_click(driver, more[-1])
                time.sleep(0.15)

        # tenta achar "Excluir"
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
            # tentativa global
            items = driver.find_elements(By.XPATH, "//div[@role='menuitem'][contains(normalize-space(.), 'Excluir') or .//i[normalize-space()='delete']]")
            items = [it for it in items if it.is_displayed() and it.is_enabled()]
            if items:
                item = items[-1]

        if item and _click(item):
            # Aguarda sumir o vídeo
            try:
                WebDriverWait(driver, timeout).until(EC.invisibility_of_element_located((By.TAG_NAME, "video")))
            except Exception:
                pass
            return True

        time.sleep(1.0)

    # Se falhou em excluir, considera ok se já não houver vídeo
    return not _videos_visible()

# ----------- Download 720p (probes + retries + refresh em 99%) -----------
def _wait_download_720p(driver: webdriver.Chrome, download_dir: str, before: List[str]) -> str:
    total_wait = int(float(os.getenv("FLOW_DOWNLOAD_WAIT_SEC", "600")))
    probe = int(float(os.getenv("FLOW_PROBE_SEC", "10")))
    dl_attempts = int(float(os.getenv("FLOW_DL_ATTEMPTS", "3")))
    dl_grace = int(float(os.getenv("FLOW_DL_GRACE_SEC", "10")))
    stall_sec = int(float(os.getenv("FLOW_STALL_REFRESH_SEC", "35")))
    stall_max = int(float(os.getenv("FLOW_STALL_REFRESH_MAX", "2")))

    deadline = time.time() + total_wait
    first_seen_ts = None
    seen_once_logged = False
    refreshes = 0
    last_stall_mark = None

    # Primeiro: aguardar que um <video> apareça OU fazer refresh se travar em 99%
    logger.info("⏳ Aguardando o player aparecer (com anti-stall 99%%)…")
    while time.time() < deadline:
        vids = driver.find_elements(By.TAG_NAME, "video")
        vids = [v for v in vids if v.is_displayed()]
        if vids:
            break

        if _is_stuck_99(driver):
            if last_stall_mark is None:
                last_stall_mark = time.time()
            if (time.time() - last_stall_mark) >= stall_sec and refreshes < stall_max:
                logger.info("🔄 Detected 99%% por ≥%ds — dando refresh (%d/%d)…", stall_sec, refreshes+1, stall_max)
                _refresh(driver)
                last_stall_mark = None
                refreshes += 1
                continue
        else:
            last_stall_mark = None

        time.sleep(1.0)

    if not driver.find_elements(By.TAG_NAME, "video"):
        _dump_debug(driver, "no_video_timeout")
        raise TimeoutException("Não apareceu nenhum <video> na página.")

    logger.info("⏳ Monitorando o player; probes a cada %ds (timeout total %ds)…", probe, total_wait)

    # Loop de probes + clique em 720p com retries
    while time.time() < deadline:
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
                            _hover_latest_video(driver); time.sleep(0.6)
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

        # Anti-stall durante probes também
        if _is_stuck_99(driver) and refreshes < stall_max:
            if last_stall_mark is None:
                last_stall_mark = time.time()
            elif (time.time() - last_stall_mark) >= stall_sec:
                logger.info("🔄 99%% detectado durante probes — refresh (%d/%d)…", refreshes+1, stall_max)
                _refresh(driver)
                last_stall_mark = None
                refreshes += 1
                continue
        else:
            last_stall_mark = None

        time.sleep(probe)

    _dump_debug(driver, "download_timeout")
    raise TimeoutException("Timeout geral tentando efetuar o download 720p.")

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

# ====================== APIs públicas ======================
def generate_single_via_flow(
    prompt_text: str,
    out_path: str,
    project_url: str,
    cookies_file: str,
    headless: bool = True,
    timeout_sec: int = 480,
) -> str:
    """
    Mantido para retrocompatibilidade: abre sessão, gera 1 cena e fecha.
    """
    download_dir = os.path.abspath(os.path.dirname(out_path) or ".")
    os.makedirs(download_dir, exist_ok=True)
    before = glob.glob(os.path.join(download_dir, "*.mp4"))

    driver = _chrome(download_dir=download_dir, headless=headless)
    try:
        cookies = _read_netscape_cookies(cookies_file)
        _add_cookies_multi_domain(driver, cookies)

        if "/tools/flow" in project_url and "/project/" not in project_url:
            logger.info("Abrindo Flow e criando 'Novo projeto'…")
            _open_flow_home(driver, project_url)
            _click_novo_projeto(driver)
            _force_respostas_por_comando_1(driver)
        else:
            driver.get(project_url)

        _clear_existing_videos(driver)

        logger.info("Enviando prompt…")
        _submit_prompt(driver, prompt_text)
        logger.info("Prompt enviado; aguardando opção de download…")

        mp4_tmp = _wait_download_720p(driver, download_dir, before)
        logger.info("Vídeo baixado: %s", mp4_tmp)

        _delete_card(driver)  # tentativa primária de exclusão

        # LIMPEZA FINAL extra (garantir que nada restou)
        try:
            if _delete_current_video(driver):
                logger.info("🧹 Limpeza final: vídeo removido do projeto.")
            else:
                logger.warning("⚠️ Limpeza final: não consegui excluir o vídeo ativo.")
        except Exception as e:
            logger.debug("Limpeza final falhou: %s", e)

        if os.path.abspath(mp4_tmp) != os.path.abspath(out_path):
            if os.path.exists(out_path):
                try: os.remove(out_path)
                except Exception: pass
            shutil.move(mp4_tmp, out_path)
        return out_path

    except Exception:
        _dump_debug(driver, "exception")
        raise
    finally:
        try:
            driver.quit()
        except Exception:
            pass

def generate_many_via_flow(
    prompts: List[str],
    out_paths: List[str],
    project_url: str,
    cookies_file: str,
    headless: bool = True,
    timeout_sec: int = 480,
) -> List[str]:
    """
    NOVO: Gera N cenas em uma ÚNICA sessão e no MESMO projeto.
    Para cada cena:
      - limpa cartões existentes,
      - envia prompt,
      - baixa 720p (com retries, hover, anti-99%),
      - exclui o cartão baixado,
      - move o arquivo para o out_path correspondente.
    Fecha o navegador somente ao final (com limpeza final garantida).
    """
    if len(prompts) != len(out_paths):
        raise ValueError("prompts e out_paths precisam ter o mesmo tamanho.")

    # todos os downloads cairão na pasta do primeiro out_path (mesma pasta já usada no seu pipeline)
    download_dir = os.path.abspath(os.path.dirname(out_paths[0]) or ".")
    os.makedirs(download_dir, exist_ok=True)

    driver = _chrome(download_dir=download_dir, headless=headless)
    try:
        cookies = _read_netscape_cookies(cookies_file)
        _add_cookies_multi_domain(driver, cookies)

        # entry: home → novo projeto → respostas por comando = 1
        if "/tools/flow" in project_url and "/project/" not in project_url:
            logger.info("Abrindo Flow e criando 'Novo projeto'…")
            _open_flow_home(driver, project_url)
            _click_novo_projeto(driver)
            _force_respostas_por_comando_1(driver)
        else:
            driver.get(project_url)

        results: List[str] = []
        for idx, (prompt_text, out_path) in enumerate(zip(prompts, out_paths), start=1):
            logger.info("──────── Cena %d/%d ────────", idx, len(prompts))

            # limpar cartões existentes ANTES de enviar o prompt da cena
            _clear_existing_videos(driver)

            # coletar baseline de arquivos para detectar o novo .mp4
            before = glob.glob(os.path.join(download_dir, "*.mp4"))

            # enviar prompt
            logger.info("Enviando prompt…")
            _submit_prompt(driver, prompt_text)
            logger.info("Prompt enviado; aguardando opção de download…")

            # aguarda e tenta os cliques de download 720p (com anti-99%)
            mp4_tmp = _wait_download_720p(driver, download_dir, before)
            logger.info("Vídeo baixado: %s", mp4_tmp)

            # excluir o cartão do vídeo que acabou de baixar (mantém projeto limpo)
            _delete_card(driver)

            # mover para o destino final desta cena
            if os.path.abspath(mp4_tmp) != os.path.abspath(out_path):
                if os.path.exists(out_path):
                    try: os.remove(out_path)
                    except Exception: pass
                shutil.move(mp4_tmp, out_path)

            results.append(out_path)

        # LIMPEZA FINAL: garantir que não ficou vídeo no projeto
        try:
            _clear_existing_videos(driver)
            if _delete_current_video(driver):
                logger.info("🧹 Limpeza final: projeto ficou sem vídeos antes de fechar.")
            else:
                logger.warning("⚠️ Limpeza final: não consegui confirmar a remoção do vídeo ativo.")
        except Exception as e:
            logger.debug("Limpeza final falhou: %s", e)

        logger.info("✅ Todas as %d cena(s) foram geradas e baixadas.", len(results))
        return results

    except Exception:
        _dump_debug(driver, "exception_many")
        raise
    finally:
        try:
            driver.quit()
        except Exception:
            pass
