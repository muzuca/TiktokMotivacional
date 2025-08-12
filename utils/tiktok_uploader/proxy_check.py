import base64
import os
import requests
from dotenv import load_dotenv

def quick_proxy_test(timeout=10):
    """
    Testa o proxy do .env via requests (httpbin.org/ip).
    Lança exceção se falhar.
    """
    load_dotenv()
    host = os.getenv("PROXY_HOST")
    port = os.getenv("PROXY_PORT")
    user = os.getenv("PROXY_USER")
    pw = os.getenv("PROXY_PASS")

    if not (host and port):
        raise RuntimeError("PROXY_HOST/PROXY_PORT ausentes no .env")

    proxies = {
        "http": f"http://{host}:{port}",
        "https": f"http://{host}:{port}",
    }
    headers = {}
    if user and pw:
        token = base64.b64encode(f"{user}:{pw}".encode()).decode()
        headers["Proxy-Authorization"] = f"Basic {token}"

    r = requests.get("http://httpbin.org/ip", proxies=proxies, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()
