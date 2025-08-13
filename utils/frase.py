# utils/frase.py

import os
import re
import unicodedata
import random
import json
import hashlib
import time
from dotenv import load_dotenv
import google.generativeai as genai
import logging
from typing import List, Set, Tuple, Optional

# -----------------------------------------------------------------------------
# Config & logging
# -----------------------------------------------------------------------------
load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# Modelo (mantenha o mesmo que você já usa)
model = genai.GenerativeModel("models/gemini-1.5-pro-latest")

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Cache de frases usadas
# -----------------------------------------------------------------------------
CACHE_DIR = "cache"
os.makedirs(CACHE_DIR, exist_ok=True)
PHRASES_CACHE_FILE = os.path.join(CACHE_DIR, "used_phrases.json")

def load_used_phrases() -> Set[str]:
    """Carrega hashes de frases já usadas (curtas, prompts e longas)."""
    if os.path.exists(PHRASES_CACHE_FILE):
        try:
            with open(PHRASES_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return set(data)
        except Exception:
            pass
    return set()

def save_used_phrases(used_phrases: Set[str]) -> None:
    """Salva hashes de frases já usadas."""
    try:
        with open(PHRASES_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(list(used_phrases)), f, ensure_ascii=False)
    except Exception as e:
        logger.warning("Não foi possível salvar cache de frases: %s", e)

# -----------------------------------------------------------------------------
# Helpers genéricos
# -----------------------------------------------------------------------------
def _lang_tag(idioma: Optional[str]) -> str:
    s = (idioma or "en").strip().lower()
    if s in ("pt", "pt-br", "br", "brasil", "portugues", "português"):
        return "pt"
    return "en"

def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def _hash_key(s: str, prefix: str = "") -> str:
    """Cria uma chave de cache (com prefixo opcional) para evitar colisões entre tipos."""
    return f"{prefix}{_md5(s)}" if prefix else _md5(s)

def _clean_line(s: str) -> str:
    """Normaliza uma linha: remove bullets/numeração e aspas redundantes."""
    if not s:
        return ""
    line = s.strip()
    # remove numeração/bullet no começo: "1. foo", "- bar", "* baz"
    line = re.sub(r'^\s*(?:\d+[\).\s-]+|[-*•]\s+)', '', line).strip()
    # remove aspas duplas simples isoladas
    line = line.strip(' "\'')
    # normaliza espaços
    line = re.sub(r'\s+', ' ', line)
    return line

def _parse_json_list(text: str) -> List[str]:
    """Tenta interpretar o texto como um array JSON de strings."""
    try:
        data = json.loads(text)
        if isinstance(data, list):
            out = []
            for item in data:
                if isinstance(item, str):
                    c = _clean_line(item)
                    if c:
                        out.append(c)
            return out
    except Exception:
        pass
    return []

def _ask_gemini_list(prompt: str, temperature: float = 1.05, tries: int = 2, sleep_base: float = 1.0) -> List[str]:
    """Pede ao Gemini uma lista (idealmente JSON). Usa fallback por linhas e retry com backoff."""
    for attempt in range(tries):
        try:
            # Tentativa 1: exigir JSON
            resp = model.generate_content(
                prompt,
                generation_config={
                    "response_mime_type": "application/json",
                    "temperature": temperature,
                    "top_p": 0.95,
                },
            )
            raw = (getattr(resp, "text", "") or "").strip()
            lst = _parse_json_list(raw)
            if lst:
                return lst

            # Tentativa 2: texto livre por linhas
            resp2 = model.generate_content(
                prompt,
                generation_config={
                    "temperature": temperature,
                    "top_p": 0.95,
                },
            )
            txt = (getattr(resp2, "text", "") or "").strip()
            if txt:
                lines = [_clean_line(l) for l in txt.split("\n") if _clean_line(l)]
                if lines:
                    return lines

        except Exception:
            # backoff leve e segue
            time.sleep(sleep_base * (2 ** attempt))
    return []

def _collect_long_candidates(lang: str, target_total: int = 12, batch_size: int = 4, max_calls: int = 4) -> List[str]:
    """Coleta mini-discursos longos em chamadas menores para aumentar diversidade e confiabilidade."""
    prompts = {
        "en": (
            "Write {n} distinct motivational mini-speeches in English. "
            "Each MUST have 40–55 words (~20 seconds narration). "
            "Vary tone and content (no paraphrases). "
            "Return ONLY a JSON array of strings."
        ),
        "pt": (
            "Escreva {n} mini-discursos motivacionais em português. "
            "Cada um DEVE ter entre 40 e 55 palavras (~20 segundos de narração). "
            "Varie o tom e o conteúdo (sem paráfrases). "
            "Retorne APENAS um array JSON de strings."
        ),
    }
    prompt_tpl = prompts["en" if lang == "en" else "pt"]

    all_items: List[str] = []
    calls = min(max_calls, max(1, (target_total + batch_size - 1) // batch_size))
    for _ in range(calls):
        prompt = prompt_tpl.format(n=batch_size)
        items = _ask_gemini_list(prompt, temperature=1.05, tries=2)
        for it in items:
            itc = _clean_line(it)
            if itc and itc not in all_items:
                all_items.append(itc)
        if len(all_items) >= target_total:
            break
    return all_items

def _pick_new(pool: List[str], used: Set[str], prefix: str, min_words: int, max_words: int) -> Optional[str]:
    """Escolhe aleatoriamente uma frase do pool que ainda não foi usada e está no range de palavras."""
    cand = [p for p in pool if min_words <= len(p.split()) <= max_words]
    random.shuffle(cand)
    for c in cand:
        key = _hash_key(c, prefix=prefix)
        if key not in used:
            return c
    return None

# -----------------------------------------------------------------------------
# Geradores pedidos
# -----------------------------------------------------------------------------
def gerar_prompt_paisagem(idioma="en") -> str:
    """
    Gera uma descrição curta de paisagem (≤7 palavras), evitando repetição via cache.
    """
    if not os.getenv("GEMINI_API_KEY"):
        logger.error("❌ Chave API do Gemini (GEMINI_API_KEY) não configurada no .env.")
        return "Mountains at sunrise" if _lang_tag(idioma) == "en" else "Montanhas ao nascer do sol"

    used_phrases = load_used_phrases()
    try:
        logger.info("Gerando prompt de paisagem com Gemini em %s.", idioma)
        if _lang_tag(idioma) == "en":
            prompt_text = (
                "Create 10 short descriptions of beautiful landscapes. "
                "Each line ≤ 7 words. Return a JSON array of strings."
            )
        else:
            prompt_text = (
                "Crie 10 descrições curtas de paisagens bonitas. "
                "Cada linha ≤ 7 palavras. Retorne um array JSON de strings."
            )

        descricoes = _ask_gemini_list(prompt_text, temperature=0.9, tries=2)
        # Filtro de tamanho
        descricoes = [d for d in descricoes if len(d.split()) <= 7]

        if not descricoes:
            raise ValueError("Nenhuma descrição válida retornada.")

        # Dedup por cache (sem prefixo para manter compat)
        novas_descricoes = [d for d in descricoes if _md5(d) not in used_phrases]
        if not novas_descricoes:
            logger.warning("Nenhum novo prompt disponível. Reutilizando padrão.")
            return "Mountains at sunrise" if _lang_tag(idioma) == "en" else "Montanhas ao nascer do sol"

        descricao_escolhida = random.choice(novas_descricoes)
        used_phrases.add(_md5(descricao_escolhida))
        save_used_phrases(used_phrases)
        logger.info("📷 Prompt gerado para imagem: %s", descricao_escolhida)
        return descricao_escolhida

    except Exception as e:
        logger.error("Erro ao gerar prompt da paisagem com Gemini: %s", str(e))
        return "Mountains at sunrise" if _lang_tag(idioma) == "en" else "Montanhas ao nascer do sol"

def gerar_frase_motivacional(idioma="en") -> str:
    """
    Gera uma frase motivacional curta (≤15 palavras), evitando repetição.
    """
    if not os.getenv("GEMINI_API_KEY"):
        logger.error("❌ Chave API do Gemini (GEMINI_API_KEY) não configurada no .env.")
        return "You are stronger than you think." if _lang_tag(idioma) == "en" else "Você é mais forte do que imagina."

    used_phrases = load_used_phrases()
    try:
        logger.info("Gerando frase motivacional com Gemini em %s.", idioma)
        if _lang_tag(idioma) == "en":
            prompt_text = (
                "Create 10 motivational phrases in English, each ≤ 15 words. "
                "Return a JSON array of strings."
            )
        else:
            prompt_text = (
                "Crie 10 frases motivacionais em português, cada uma com ≤ 15 palavras. "
                "Retorne um array JSON de strings."
            )

        frases = _ask_gemini_list(prompt_text, temperature=0.95, tries=2)
        # valida tamanho
        frases = [f for f in frases if len(f.split()) <= 15]

        if not frases:
            raise ValueError("Nenhuma frase válida retornada.")

        novas_frases = [f for f in frases if _md5(f) not in used_phrases]
        if not novas_frases:
            logger.warning("Nenhuma nova frase disponível. Reutilizando padrão.")
            return "You are stronger than you think." if _lang_tag(idioma) == "en" else "Você é mais forte do que imagina."

        frase_escolhida = random.choice(novas_frases)
        used_phrases.add(_md5(frase_escolhida))
        save_used_phrases(used_phrases)
        logger.info("🧠 Frase motivacional escolhida: %s", frase_escolhida)
        return frase_escolhida

    except Exception as e:
        logger.error("Erro ao gerar frase motivacional com Gemini: %s", str(e))
        return "You are stronger than you think." if _lang_tag(idioma) == "en" else "Você é mais forte do que imagina."

def gerar_frase_motivacional_longa(idioma="en") -> str:
    """
    Gera um mini-discurso motivacional (~40–55 palavras) evitando repetição.
    Estratégia:
      - Coleta em lotes pequenos (4 por chamada), até 16 opções.
      - Filtra por tamanho (preferido 40–55; tolerância 35–65).
      - Escolhe uma ainda não usada (cache prefixado LONG::).
    """
    if not os.getenv("GEMINI_API_KEY"):
        logger.error("❌ Chave API do Gemini (GEMINI_API_KEY) não configurada no .env.")
        return (
            "You are stronger than you think. Take a deep breath and push forward every day, overcoming challenges to achieve your dreams."
            if _lang_tag(idioma) == "en" else
            "Você é mais forte do que imagina. Respire fundo e siga em frente todos os dias, superando desafios para realizar seus sonhos."
        )

    used = load_used_phrases()
    prefix = "LONG::"
    lang = _lang_tag(idioma)

    try:
        logger.info("Gerando frases motivacionais longas (%s).", "EN" if lang == "en" else "PT-BR")
        # 1) Coletar candidatos em múltiplas chamadas curtas
        candidates = _collect_long_candidates(lang=lang, target_total=12, batch_size=4, max_calls=4)

        # 2) Normalizar e filtrar por tamanho
        candidates = [_clean_line(c) for c in candidates if _clean_line(c)]
        ideal = [c for c in candidates if 40 <= len(c.split()) <= 55]
        tolerant = [c for c in candidates if 35 <= len(c.split()) <= 65]

        def pick(pool: List[str]) -> Optional[str]:
            return _pick_new(pool, used, prefix=prefix, min_words=35, max_words=65)

        chosen = pick(ideal) or pick(tolerant)

        # 3) Segunda rodada extra se nada sobrou
        if not chosen:
            logger.info("Todas repetidas/inválidas. Tentando nova chamada ao modelo…")
            extra = _collect_long_candidates(lang=lang, target_total=4, batch_size=4, max_calls=1)
            extra = [_clean_line(c) for c in extra if _clean_line(c)]
            extra_ideal = [c for c in extra if 40 <= len(c.split()) <= 55]
            extra_tol = [c for c in extra if 35 <= len(c.split()) <= 65]
            chosen = pick(extra_ideal) or pick(extra_tol)

        if not chosen:
            raise ValueError("Nenhuma frase longa nova adequada foi gerada.")

        used.add(_hash_key(chosen, prefix=prefix))
        save_used_phrases(used)
        logger.info("🧠 Frase motivacional longa escolhida: %s", chosen[:100] + "..." if len(chosen) > 100 else chosen)
        return chosen

    except Exception as e:
        logger.error("Erro ao gerar frases longas com Gemini: %s", e)
        return (
            "You are stronger than you think. Take a deep breath and push forward every day, overcoming challenges to achieve your dreams."
            if lang == "en" else
            "Você é mais forte do que imagina. Respire fundo e siga em frente todos os dias, superando desafios para realizar seus sonhos."
        )

# -----------------------------------------------------------------------------
# Utilidades existentes
# -----------------------------------------------------------------------------
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

    def comp_len(ws: List[str]) -> int:
        return sum(len(w) for w in ws) + max(0, len(ws) - 1)

    pequenos = {
        "e", "ou", "de", "da", "do", "das", "dos",
        "em", "no", "na", "nos", "nas", "por", "pra", "para",
        "o", "a", "os", "as", "um", "uma", "que", "se", "com"
    }
    pontos = {".", ",", ";", "!", "?", "—", "-", "–", ":"}

    melhor_divisao = None
    menor_diferenca = float('inf')

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

def gerar_slug(texto: str, limite: int = 30) -> str:
    """
    Transforma um texto em slug (nome de arquivo seguro, sem acentos ou símbolos).
    Ex.: "Montanhas ao nascer do sol" -> "montanhas_ao_nascer"
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
