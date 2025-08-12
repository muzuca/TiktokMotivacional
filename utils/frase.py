from dotenv import load_dotenv
import os
import google.generativeai as genai
import random

# Carrega as variáveis de ambiente
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Configura a API do Gemini
genai.configure(api_key=GEMINI_API_KEY)

def gerar_prompt_paisagem(idioma):
    """Gera um prompt de paisagem com base no idioma."""
    if idioma == 'pt-br':
        prompts = [
            "Uma floresta tropical exuberante com rios cristalinos",
            "Uma montanha coberta de neve ao amanhecer",
            "Um campo de flores silvestres sob um céu azul"
        ]
    else:  # idioma 'en' (padrão)
        prompts = [
            "A lush tropical forest with crystal-clear rivers",
            "A snow-covered mountain at dawn",
            "A field of wildflowers under a blue sky"
        ]
    return random.choice(prompts)

def gerar_frase_motivacional(idioma):
    """Gera uma frase motivacional com base no idioma."""
    if idioma == 'pt-br':
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content("Gere uma frase motivacional em português brasileiro, curta e impactante.")
    else:  # idioma 'en' (padrão)
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content("Generate a short and impactful motivational phrase in English.")
    return response.text.strip()

def gerar_slug(texto):
    """Gera um slug a partir de um texto."""
    texto = texto.lower().replace(" ", "_").replace(",", "").replace(".", "")
    return texto[:50]  # Limita a 50 caracteres para evitar nomes muito longos

if __name__ == "__main__":
    # Teste das funções
    idioma = 'en'  # ou 'pt-br' para testar
    print(f"Prompt: {gerar_prompt_paisagem(idioma)}")
    print(f"Frase: {gerar_frase_motivacional(idioma)}")
    print(f"Slug: {gerar_slug(gerar_prompt_paisagem(idioma))}")