# utils/gemini_image.py
import os, time, json, base64
from typing import Optional
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

GEMINI_URL = "https://gemini.google.com/app"

def _env_int(name, default): 
    try: return int(os.getenv(name, str(default)).strip())
    except: return default

def _env_bool(name, default):
    v = os.getenv(name)
    if v is None: return default
    v = v.strip().lower()
    if v in ("1","true","yes","on"): return True
    if v in ("0","false","no","off"): return False
    return default

def _ensure_dir(p): Path(p).mkdir(parents=True, exist_ok=True)

def _mk_driver(profile_dir: str, *, headless: bool, width: int, height: int, zoom: int) -> webdriver.Chrome:
    opts = Options()
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-gpu")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    if headless:
        opts.add_argument("--headless=new")
    driver = webdriver.Chrome(options=opts)
    driver.set_window_size(width, height)
    # Zoom via CDP para “caber” na tela sem malabarismo de Ctrl+-/+
    try:
        device_metrics = {
            "width": width, "height": height, "deviceScaleFactor": 1, "mobile": False,
        }
        driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", device_metrics)
        if zoom and zoom != 100:
            driver.execute_cdp_cmd("Emulation.setPageScaleFactor", {"pageScaleFactor": zoom / 100.0})
    except Exception:
        pass
    return driver

def _save_cookies(driver, path):
    data = driver.get_cookies()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _load_cookies(driver, path):
    with open(path, "r", encoding="utf-8") as f:
        cookies = json.load(f)
    for c in cookies:
        # Precisamos garantir domínio e path válidos
        c.pop("sameSite", None)
        c.setdefault("path", "/")
        try:
            driver.add_cookie(c)
        except Exception:
            # Ignora cookies inválidos ou de outro domínio
            pass

def bootstrap_login(profile_dir: str, cookies_path: str, *, width: int=900, height:int=1100, zoom:int=80):
    """Abra uma vez para logar na sua conta Google e salvar cookies do Gemini."""
    _ensure_dir(profile_dir)
    _ensure_dir(os.path.dirname(cookies_path) or ".")
    drv = _mk_driver(profile_dir, headless=False, width=width, height=height, zoom=zoom)
    try:
        drv.get(GEMINI_URL)
        print("➡ Faça login no Google/Gemini nesta janela. Quando a home do Gemini carregar, aperte ENTER aqui...")
        input()
        _save_cookies(drv, cookies_path)
        print(f"✅ Cookies salvos em: {cookies_path}")
    finally:
        drv.quit()

def _find_prompt_box(driver):
    # Tentativas robustas para pegar a caixa de mensagem do Gemini
    candidates = [
        (By.CSS_SELECTOR, 'textarea[aria-label*="Message"], textarea[aria-label*="mensagem" i]'),
        (By.CSS_SELECTOR, 'div[contenteditable="true"][role="textbox"]'),
        (By.CSS_SELECTOR, 'textarea'),
    ]
    for how, sel in candidates:
        try:
            el = WebDriverWait(driver, 8).until(EC.presence_of_element_located((how, sel)))
            if el: return el
        except Exception:
            pass
    raise RuntimeError("Não achei a caixa de mensagem do Gemini.")

def _wait_for_any_image(driver, timeout=120):
    # Espera alguma <img> nova surgir no fluxo
    last_seen = set()
    t0 = time.time()
    while time.time() - t0 < timeout:
        imgs = driver.find_elements(By.CSS_SELECTOR, "main img, div img, article img")
        # filtra por dimensões razoáveis (evita ícones)
        imgs = [i for i in imgs if (i.size and i.size.get("width",0) >= 200 and i.size.get("height",0) >= 200)]
        for i in imgs:
            src = i.get_attribute("src") or ""
            key = (src, i.size.get("width"), i.size.get("height"))
            if key in last_seen:
                continue
            last_seen.add(key)
            # Achou uma imagem “grande”
            return i
        time.sleep(1.2)
    raise TimeoutError("Tempo esgotado esperando imagem do Gemini.")

def _grab_image_as_png_bytes(driver, img_el) -> bytes:
    # 1) Tenta pegar o src; se for blob:, converte para dataURL via JS assíncrono
    src = img_el.get_attribute("src") or ""
    if src.startswith("data:image"):
        # já é dataURL
        header, b64 = src.split(",", 1)
        return base64.b64decode(b64)

    if src.startswith("blob:") or src.startswith("https://"):
        try:
            # Executa JS assíncrono para ler o blob como dataURL
            js = """
            const el = arguments[0];
            const cb = arguments[arguments.length - 1];
            (async () => {
              try {
                const src = el.getAttribute('src') || '';
                const blob = await fetch(src).then(r => r.blob());
                const fr = new FileReader();
                fr.onload = () => cb({ok:true, data: fr.result});
                fr.onerror = () => cb({ok:false, err:'FileReader error'});
                fr.readAsDataURL(blob);
              } catch(e){ cb({ok:false, err: String(e)}); }
            })();
            """
            res = driver.execute_async_script(js, img_el)
            if res and res.get("ok") and isinstance(res.get("data"), str) and res["data"].startswith("data:image"):
                b64 = res["data"].split(",", 1)[1]
                return base64.b64decode(b64)
        except Exception:
            pass

    # 2) Fallback: screenshot do elemento
    return img_el.screenshot_as_png

def gerar_imagem_gemini(prompt: str, arquivo_saida: str, *, idioma: Optional[str] = None):
    """
    Abre a página do Gemini, envia o prompt para gerar UMA imagem 9:16
    e salva como 1080x1920 (ou mantém proporção mais próxima).
    """
    # Pastas/flags
    profile_dir = os.getenv("GEMINI_PROFILE_DIR", ".chrome-profile-gemini")
    cookies_path = os.getenv("GEMINI_COOKIES_PATH", os.path.join("secrets", "gemini_cookies.json"))
    headless = _env_bool("GEMINI_HEADLESS", False)
    width   = _env_int("BROWSER_WIDTH", 500)
    height  = _env_int("BROWSER_HEIGHT", 820)
    zoom    = _env_int("BROWSER_ZOOM", 70)  # 70% é um bom começo pro app do Gemini

    _ensure_dir(profile_dir)
    _ensure_dir(os.path.dirname(cookies_path) or ".")
    _ensure_dir(os.path.dirname(arquivo_saida) or ".")

    driver = _mk_driver(profile_dir, headless=headless, width=width, height=height, zoom=zoom)
    try:
        # Carrega domínio para poder injetar cookies
        driver.get(GEMINI_URL)
        time.sleep(1.0)
        # Injeta cookies (se existirem) e recarrega
        if os.path.isfile(cookies_path):
            try:
                _load_cookies(driver, cookies_path)
                driver.get(GEMINI_URL)
            except Exception:
                pass

        # Caixa de prompt
        box = _find_prompt_box(driver)

        # Força uma formulação que tenda a gerar IMAGEM (não só texto)
        # Ajuste o texto conforme seu gosto/idioma
        pedido = (
            "Create a SINGLE AI image. Aspect ratio 9:16, portrait. "
            "High detail, cinematic lighting. Reply with the image only.\n\n"
            f"Subject: {prompt}"
        )
        try:
            box.click()
            box.clear()
        except Exception:
            pass
        box.send_keys(pedido)
        box.send_keys(Keys.ENTER)

        # Espera a imagem aparecer
        img_el = _wait_for_any_image(driver, timeout=180)

        # Extrai bytes
        png_bytes = _grab_image_as_png_bytes(driver, img_el)

        # Salva e garante 1080x1920
        from PIL import Image, ImageOps
        import io
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        # Ajusta para 1080x1920 sem distorcer (como seu pipeline já faz)
        tw, th = 1080, 1920
        iw, ih = img.size
        scale = max(tw/iw, th/ih)
        new = img.resize((int(iw*scale), int(ih*scale)), Image.LANCZOS)
        nx, ny = new.size
        left = (nx - tw)//2
        top  = (ny - th)//2
        new = new.crop((left, top, left+tw, top+th))
        new.save(arquivo_saida, "PNG", optimize=True)
    finally:
        driver.quit()

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--bootstrap", action="store_true", help="Abre o Gemini para fazer login e salvar cookies.")
    ap.add_argument("--profile", default=os.getenv("GEMINI_PROFILE_DIR", ".chrome-profile-gemini"))
    ap.add_argument("--cookies", default=os.getenv("GEMINI_COOKIES_PATH", os.path.join("secrets","gemini_cookies.json")))
    args = ap.parse_args()
    if args.bootstrap:
        bootstrap_login(args.profile, args.cookies)
        raise SystemExit(0)
    print("Use gerar_imagem_gemini() a partir do pipeline.")
