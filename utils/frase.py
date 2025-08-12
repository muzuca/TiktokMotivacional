# utils/frase.py

import os
import re
import unicodedata
import random
import json
import hashlib
from dotenv import load_dotenv
import google.generativeai as genai
import logging

# Carrega variáveis do .env
load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# Inicializa o modelo do Gemini
model = genai.GenerativeModel("models/gemini-1.5-pro-latest")

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
PHRASES_CACHE_FILE = os.path.join(CACHE_DIR, "used_phrases.json")

def load_used_phrases():
    """Carrega a lista de frases já usadas do cache."""
    if os.path.exists(PHRASES_CACHE_FILE):
        with open(PHRASES_CACHE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_used_phrases(used_phrases):
    """Salva a lista de frases usadas no cache."""
    with open(PHRASES_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(list(used_phrases), f)

def gerar_prompt_paisagem():
    """
    Gera uma descrição curta e aleatória de uma paisagem bonita.
    Escolhe uma entre 10 opções geradas pelo Gemini, evitando repetição.
    """
    if not os.getenv("GEMINI_API_KEY"):
        logger.error("❌ Chave API do Gemini (GEMINI_API_KEY) não configurada no .env.")
        return "Montanhas ao nascer do sol"

    used_phrases = load_used_phrases()
    try:
        logger.info("Gerando prompt de paisagem com Gemini.")
        prompt = (
            "Crie 10 descrições curtas de paisagens bonitas, com até 7 palavras cada. "
            "Liste cada uma em uma nova linha, sem numeração."
        )
        response = model.generate_content(prompt)
        texto = response.text.strip()

        descricoes = [linha.strip() for linha in texto.split("\n") if linha.strip() and len(linha.split()) <= 7]
        
        if not descricoes:
            raise ValueError("Nenhuma descrição válida retornada.")

        # Filtra descrições já usadas
        novas_descricoes = [d for d in descricoes if hashlib.md5(d.encode()).hexdigest() not in used_phrases]
        if not novas_descricoes:
            logger.warning("Nenhum novo prompt disponível. Reutilizando 'Montanhas ao nascer do sol'.")
            return "Montanhas ao nascer do sol"

        descricao_escolhida = random.choice(novas_descricoes)
        used_phrases.add(hashlib.md5(descricao_escolhida.encode()).hexdigest())
        save_used_phrases(used_phrases)
        logger.info("📷 Prompt gerado para imagem: %s", descricao_escolhida)
        return descricao_escolhida

    except Exception as e:
        logger.error("Erro ao gerar prompt da paisagem com Gemini: %s", str(e))
        return "Montanhas ao nascer do sol"

def gerar_frase_motivacional():
    """
    Gera uma frase motivacional curta em português, escolhida aleatoriamente entre 10 opções, evitando repetição.
    """
    if not os.getenv("GEMINI_API_KEY"):
        logger.error("❌ Chave API do Gemini (GEMINI_API_KEY) não configurada no .env.")
        return "Você é mais forte do que imagina."

    used_phrases = load_used_phrases()
    try:
        logger.info("Gerando frase motivacional com Gemini.")
        prompt = (
            "Crie 10 frases motivacionais em português, com no máximo 15 palavras cada. "
            "Liste cada frase em uma nova linha, sem numeração."
        )
        response = model.generate_content(prompt)
        texto = response.text.strip()

        # Divide as frases por linha e remove vazios, validando comprimento
        frases = [linha.strip() for linha in texto.split("\n") if linha.strip() and len(linha.split()) <= 15]
        
        if not frases:
            raise ValueError("Nenhuma frase válida retornada.")

        # Filtra frases já usadas
        novas_frases = [f for f in frases if hashlib.md5(f.encode()).hexdigest() not in used_phrases]
        if not novas_frases:
            logger.warning("Nenhuma nova frase disponível. Reutilizando 'Você é mais forte do que imagina.'")
            return "Você é mais forte do que imagina."

        frase_escolhida = random.choice(novas_frases)
        used_phrases.add(hashlib.md5(frase_escolhida.encode()).hexdigest())
        save_used_phrases(used_phrases)
        logger.info("🧠 Frase motivacional escolhida: %s", frase_escolhida)
        return frase_escolhida

    except Exception as e:
        logger.error("Erro ao gerar frase motivacional com Gemini: %s", str(e))
        return "Você é mais forte do que imagina."
    
def quebrar_em_duas_linhas(frase: str) -> str:
    """
    Quebra a frase em duas linhas balanceando por caracteres,
    evitando linhas que terminem/começam com palavras curtinhas
    e preferindo quebras após pontuação.
    """
    palavras = frase.split()
    n = len(palavras)
    if n <= 4:
        return frase  # curto demais: não quebra

    candidatos = range(2, n - 1)  # Evita linhas com menos de 2 palavras

    def comp_len(ws):
        return sum(len(w) for w in ws) + max(0, len(ws) - 1)

    pequenos = {
        "e", "ou", "de", "da", "do", "das", "dos",
        "em", "no", "na", "nos", "nas", "por", "pra", "para",
        "o", "a", "os", "as", "um", "uma", "que", "se", "com"
    }
    pontos = {".", ",", ";", "!", "?", "—", "-", "–", ":"}

    melhor_divisao = None
    menor_diferenca = float('inf')

    total_caracteres = comp_len(palavras)
    for i in candidatos:
        linha1 = palavras[:i]
        linha2 = palavras[i:]

        len1 = comp_len(linha1)
        len2 = comp_len(linha2)
        diferenca = abs(len1 - len2)

        # Penaliza terminar/começar com palavras curtas
        if linha1[-1].lower().strip("".join(pontos)) in pequenos:
            diferenca += 8
        if linha2[0].lower().strip("".join(pontos)) in pequenos:
            diferenca += 10

        # Recompensa quebra após pontuação
        if any(linha1[-1].endswith(p) for p in pontos):
            diferenca -= 5

        if diferenca < menor_diferenca:
            menor_diferenca = diferenca
            melhor_divisao = i

    if melhor_divisao is None:
        return frase

    return f'{" ".join(palavras[:melhor_divisao])}\n{" ".join(palavras[melhor_divisao:])}'

def gerar_slug(texto, limite=30):
    """
    Transforma um texto em slug (nome de arquivo seguro, sem acentos ou símbolos).
    Exemplo: "Montanhas ao nascer do sol" -> "montanhas_ao_nascer"
    """
    try:
        texto = unicodedata.normalize('NFD', texto).encode('ascii', 'ignore').decode('utf-8')
        texto = re.sub(r'[^a-zA-Z0-9\s]', '', texto)
        texto = texto.strip().lower().replace(" ", "_")
        slug = texto[:limite]
        logger.info("🔗 Slug gerado: %s", slug)
        return slug
    except Exception as e:
        logger.error("Erro ao gerar slug: %s", str(e))
        return "default_slug"