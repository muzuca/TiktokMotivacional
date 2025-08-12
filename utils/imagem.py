# utils/imagem.py

import os
import requests
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv
from utils.frase import quebrar_em_duas_linhas
import base64
import logging
import json

# === CONFIGURAÇÕES ===
load_dotenv()

# Modo: "pexels", "local", "colab"
IMAGE_MODE = os.getenv("IMAGE_MODE", "local").lower()
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

# Defaults para o COLAB (podem ser sobrescritos por argumentos)
COLAB_WIDTH = int(os.getenv("COLAB_WIDTH", 720))
COLAB_HEIGHT = int(os.getenv("COLAB_HEIGHT", 1280))
COLAB_STEPS = int(os.getenv("COLAB_STEPS", 30))
COLAB_GUIDANCE = float(os.getenv("COLAB_GUIDANCE", 8.5))
COLAB_NEGATIVE = os.getenv("COLAB_NEGATIVE", "")         # ex: "low quality, blurry"
COLAB_SCHEDULER = os.getenv("COLAB_SCHEDULER", "")       # ex: "euler_a", "ddim", "dpmpp_2m"
COLAB_SEED = os.getenv("COLAB_SEED")                     # string; convertemos abaixo se existir

# CONFIG LOCAL (Stable Diffusion) - defaults (podem ser sobrescritos por argumentos)
PIPELINE = None
MODEL_NAME = os.getenv("LOCAL_MODEL_NAME", "runwayml/stable-diffusion-v1-5")
IMAGE_WIDTH, IMAGE_HEIGHT = 576, 1024
INFERENCE_STEPS = 10
GUIDANCE_LOCAL = 6.5

# Configuração do logging com timestamps
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'  # Formato de horário (HH:MM:SS)
)
logger = logging.getLogger(__name__)

# Pasta de cache
CACHE_DIR = "cache"
os.makedirs(CACHE_DIR, exist_ok=True)
IMAGES_CACHE_FILE = os.path.join(CACHE_DIR, "used_images.json")

def load_used_images():
    """Carrega a lista de imagens já geradas do cache."""
    if os.path.exists(IMAGES_CACHE_FILE):
        with open(IMAGES_CACHE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_used_images(used_images):
    """Salva a lista de imagens usadas no cache."""
    with open(IMAGES_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(list(used_images), f)

# === Busca a URL da API no GitHub (via API oficial) ===
def descobrir_url_colab():
    url_api = "https://api.github.com/repos/muzuca/colab-api-endpoint/contents/ngrok_url.txt"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}" if GITHUB_TOKEN else None,
        "Accept": "application/vnd.github.v3.raw"
    }
    # remove chaves None
    headers = {k: v for k, v in headers.items() if v is not None}

    try:
        logger.info("🔍 Buscando URL da API Colab no GitHub...")
        resp = requests.get(url_api, headers=headers, timeout=8)
        if resp.status_code == 200:
            linha = resp.text.strip()
            inicio = linha.find('"') + 1
            fim = linha.find('"', inicio)
            if inicio > 0 and fim > inicio:
                url = linha[inicio:fim]
                logger.info("✅ URL da API Colab encontrada: %s", url)
                return url
            else:
                raise ValueError("Formato da URL inválido na resposta.")
        else:
            raise Exception(f"Erro ao acessar arquivo GitHub: {resp.status_code}")
    except Exception as e:
        logger.error("❌ Erro ao obter URL do Colab: %s", str(e))
        return None

# === Local: pipeline SD (carrega sob demanda) ===
if IMAGE_MODE == "local":
    from diffusers import DiffusionPipeline
    import torch

    def inicializar_pipeline():
        global PIPELINE
        if PIPELINE is not None:
            return
        logger.info("⚙️ Carregando modelo local: %s", MODEL_NAME)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.float32
        PIPELINE = DiffusionPipeline.from_pretrained(
            MODEL_NAME,
            torch_dtype=dtype
        ).to(device)

def gerar_imagem_com_frase(
    prompt: str,
    arquivo_saida: str,
    *,
    width: int | None = None,
    height: int | None = None,
    steps: int | None = None,
    guidance_scale: float | None = None,
    negative_prompt: str | None = None,
    seed: int | None = None,
    scheduler: str | None = None,
    timeout: int = 120
):
    """
    Gera uma imagem a partir de um prompt e salva em arquivo.
    Parâmetros dinâmicos (apenas quando relevantes):
      - width, height, steps, guidance_scale
    """
    os.makedirs(os.path.dirname(arquivo_saida), exist_ok=True)

    if IMAGE_MODE == "pexels" and not PEXELS_API_KEY:
        logger.error("❌ Chave API da Pexels (PEXELS_API_KEY) não configurada no .env.")
        return

    # Verifica se a imagem já foi gerada
    used_images = load_used_images()
    if arquivo_saida in used_images:
        logger.warning("⚠️ Imagem já gerada anteriormente: %s. Pulando geração.", arquivo_saida)
        return

    if IMAGE_MODE == "pexels":
        try:
            logger.info("🌐 Buscando imagem da Pexels com prompt: %s", prompt)
            headers = {"Authorization": PEXELS_API_KEY}
            params = {
                "query": prompt,
                "orientation": "portrait",
                "size": "large",
                "per_page": 1
            }
            response = requests.get(
                "https://api.pexels.com/v1/search",
                headers=headers,
                params=params,
                timeout=15
            )
            response.raise_for_status()
            data = response.json()

            if not data.get("photos"):
                logger.warning("❌ Nenhuma imagem encontrada na Pexels.")
                return

            image_url = data["photos"][0]["src"]["large2x"]
            img_data = requests.get(image_url, timeout=15).content
            with open(arquivo_saida, "wb") as f:
                f.write(img_data)
            if not os.path.exists(arquivo_saida):
                raise FileNotFoundError(f"Imagem {arquivo_saida} não foi salva.")
            logger.info("✅ Imagem salva de Pexels: %s", arquivo_saida)

            # Registra a imagem como usada
            used_images.add(arquivo_saida)
            save_used_images(used_images)

        except Exception as e:
            logger.error("❌ Erro ao buscar imagem da Pexels: %s", str(e))

    elif IMAGE_MODE == "local":
        if not os.path.exists(MODEL_NAME):
            logger.error("❌ Modelo local %s não encontrado.", MODEL_NAME)
            return

        try:
            inicializar_pipeline()
            # Usa os argumentos se vieram; senão os defaults locais
            w = width or IMAGE_WIDTH
            h = height or IMAGE_HEIGHT
            st = steps or INFERENCE_STEPS
            gs = guidance_scale or GUIDANCE_LOCAL

            logger.info("🖥️ Gerando imagem localmente... %dx%d, steps=%d, guidance=%.1f", w, h, st, gs)
            prompt_final = f"{prompt}. cinematic, high detail, natural lights"

            image = PIPELINE(
                prompt=prompt_final,
                negative_prompt=negative_prompt or "",
                width=w,
                height=h,
                num_inference_steps=st,
                guidance_scale=gs,
                # seed fixo para reproducibilidade (mude se quiser variar)
                # generator=torch.manual_seed(42)  # descomente se quiser seed fixo
            ).images[0]

            image.save(arquivo_saida)
            if not os.path.exists(arquivo_saida):
                raise FileNotFoundError(f"Imagem {arquivo_saida} não foi gerada.")
            logger.info("✅ Imagem local salva em %s", arquivo_saida)

            # Registra a imagem como usada
            used_images.add(arquivo_saida)
            save_used_images(used_images)

        except Exception as e:
            logger.error("❌ Erro ao gerar imagem localmente: %s", str(e))

    elif IMAGE_MODE == "colab":
        if not GITHUB_TOKEN:
            logger.error("❌ Token do GitHub (GITHUB_TOKEN) não configurado no .env.")
            return

        try:
            base_url = descobrir_url_colab()
            if not base_url:
                raise Exception("URL da API Colab não encontrada.")
            url = f"{base_url}/gerar-imagem"

            # Usa argumentos se vieram; senão os defaults do .env
            payload = {
                "prompt": prompt,
                "width": int(width or COLAB_WIDTH),
                "height": int(height or COLAB_HEIGHT),
                "steps": int(steps or COLAB_STEPS),
                "guidance_scale": float(guidance_scale or COLAB_GUIDANCE),
                "negative_prompt": (negative_prompt if negative_prompt is not None else COLAB_NEGATIVE),
                "scheduler": (scheduler if scheduler is not None else COLAB_SCHEDULER),
            }

            # seed opcional; só envia se houver (no arg ou no .env)
            seed_final = seed if seed is not None else (int(COLAB_SEED) if COLAB_SEED else None)
            if seed_final is not None:
                payload["seed"] = int(seed_final)

            logger.info("☁️ Solicitando geração ao Kaggle/Colab: %s | %s", url, payload)
            resp = requests.post(url, json=payload, timeout=timeout)
            if resp.status_code != 200:
                raise Exception(f"Erro do Colab: {resp.text}")

            data = resp.json()
            if "imagem_base64" not in data:
                raise Exception("Resposta inválida: imagem_base64 ausente")

            image_data = base64.b64decode(data["imagem_base64"])
            os.makedirs(os.path.dirname(arquivo_saida), exist_ok=True)
            with open(arquivo_saida, "wb") as f:
                f.write(image_data)
            if not os.path.exists(arquivo_saida):
                raise FileNotFoundError(f"Imagem {arquivo_saida} não foi salva.")
            logger.info("✅ Imagem gerada via Colab salva em %s", arquivo_saida)

            # Registra a imagem como usada
            used_images.add(arquivo_saida)
            save_used_images(used_images)

        except Exception as e:
            logger.error("❌ Erro ao gerar imagem via Colab: %s", str(e))

def escrever_frase_na_imagem(imagem_path, frase, saida_path):
    """
    Escreve uma frase quebrada em duas linhas na imagem e salva em um novo arquivo.
    """
    try:
        if not os.path.exists(imagem_path):
            raise FileNotFoundError(f"Imagem de entrada {imagem_path} não encontrada.")

        frase_quebrada = quebrar_em_duas_linhas(frase).upper()
        img = Image.open(imagem_path).convert("RGB")
        draw = ImageDraw.Draw(img)

        largura_img, altura_img = img.size
        tamanho_base = int(altura_img * 0.065)
        font = ImageFont.truetype("fonts/BebasNeue-Regular.ttf", tamanho_base)

        bbox = draw.multiline_textbbox((0, 0), frase_quebrada, font=font, align="center")
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        pos_x = (largura_img - text_w) // 2
        pos_y = (altura_img - text_h) // 2

        draw.multiline_text(
            (pos_x, pos_y),
            frase_quebrada,
            fill="white",
            font=font,
            align="center",
            stroke_width=4,
            stroke_fill="black"
        )

        os.makedirs(os.path.dirname(saida_path), exist_ok=True)
        img.save(saida_path)
        if not os.path.exists(saida_path):
            raise FileNotFoundError(f"Imagem de saída {saida_path} não foi gerada.")
        logger.info("📝 Frase adicionada na imagem: %s", saida_path)

    except Exception as e:
        logger.error("❌ Erro ao escrever a frase na imagem: %s", str(e))

def gerar_imagem(prompt, slug_paisagem, frase, slug_frase):
    """
    Gera uma imagem com base no modo configurado e adiciona a frase.
    """
    os.makedirs("imagens", exist_ok=True)
    imagem_path = os.path.join("imagens", f"{slug_paisagem}.jpg")
    saida_path = os.path.join("imagens", f"{slug_frase}.jpg")

    # Gera a imagem com base no modo
    if IMAGE_MODE == "pexels" or IMAGE_MODE == "local" or IMAGE_MODE == "colab":
        gerar_imagem_com_frase(prompt, imagem_path)
    else:
        raise ValueError(f"Modo de imagem inválido: {IMAGE_MODE}")

    # Adiciona a frase à imagem
    escrever_frase_na_imagem(imagem_path, frase, saida_path)

    return saida_path