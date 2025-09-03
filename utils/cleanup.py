# utils/cleanup.py
# -*- coding: utf-8 -*-
"""
Limpeza de artefatos entre etapas:
- Após renderizar o vídeo: apaga imagens e áudios (inclui TTS), e limpa "cache" (drawtext_* e qualquer arquivo não-JSON),
  preservando apenas os JSONs e **o último diretório de execução do Flow**.
- Após postar: apaga tudo de "videos" (inclui "prompts"), a menos que KEEP_POSTED_VIDEOS=1 no .env.

Segurança:
- Operações tolerantes a erro (retries) e com logs.
- Não levanta exceção pro chamador (não derruba o job caso algo esteja em uso no Windows).
"""

from __future__ import annotations
import os
import time
import glob
import shutil
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------- helpers

def _retry_unlink(p: Path, tries: int = 3, wait: float = 0.2) -> None:
    for i in range(tries):
        try:
            if p.exists() and p.is_file():
                p.unlink()
            return
        except Exception as e:
            if i == tries - 1:
                logger.debug("unlink falhou (%s): %s", p, e)
            time.sleep(wait)

def _retry_rmtree(p: Path, tries: int = 2, wait: float = 0.3) -> None:
    for i in range(tries):
        try:
            if p.exists() and p.is_dir():
                shutil.rmtree(p, ignore_errors=False)
            return
        except Exception as e:
            if i == tries - 1:
                logger.debug("rmtree falhou (%s): %s", p, e)
            time.sleep(wait)

def _find_latest_flow_dir(cache_dir: Path) -> Path | None:
    """
    Procura pelo **último** diretório relacionado ao Flow dentro de cache,
    considerando nomes de pasta que contenham 'flow' no caminho.
    """
    if not cache_dir.exists():
        return None

    latest_path = None
    latest_mtime = -1.0

    for path in cache_dir.rglob("*"):
        if path.is_dir() and "flow" in str(path).lower():
            try:
                mt = path.stat().st_mtime
                if mt > latest_mtime:
                    latest_mtime = mt
                    latest_path = path
            except Exception:
                pass
    return latest_path

def _delete_globs(globs: list[str]) -> int:
    count = 0
    for pattern in globs:
        for s in glob.glob(pattern, recursive=True):
            p = Path(s)
            if p.is_file():
                _retry_unlink(p)
                count += 1
    return count

# ---------- API

def purge_after_render(
    images_dir: str | Path = "imagens",
    audios_dir: str | Path = "audios",
    cache_dir: str | Path = "cache",
) -> None:
    """
    Chame **logo após** o ffmpeg concluir o .mp4 (render bem-sucedida).
    - Remove **todas** as imagens (png/jpg/webp etc.) de `imagens/`.
    - Remove **todos** os áudios (wav/mp3) de `audios/` e a pasta `audios/tts`.
    - Limpa o `cache/` removendo arquivos temporários (drawtext_* e **qualquer não-JSON**),
      **preservando apenas**: arquivos .json e o **último diretório do Flow**.
    """
    try:
        images_dir = Path(images_dir)
        audios_dir = Path(audios_dir)
        cache_dir = Path(cache_dir)

        # 1) IMAGENS
        if images_dir.exists():
            deleted = _delete_globs([
                str(images_dir / "**/*.png"),
                str(images_dir / "**/*.jpg"),
                str(images_dir / "**/*.jpeg"),
                str(images_dir / "**/*.webp"),
                str(images_dir / "**/*.bmp"),
            ])
            logger.info("🧹 Limpou %d arquivo(s) de imagens em '%s'", deleted, images_dir)

        # 2) ÁUDIOS (inclui TTS)
        if audios_dir.exists():
            # apaga arquivos comuns de áudio
            deleted = _delete_globs([
                str(audios_dir / "**/*.wav"),
                str(audios_dir / "**/*.mp3"),
                str(audios_dir / "**/*.flac"),
                str(audios_dir / "**/*.m4a"),
                str(audios_dir / "**/*.ogg"),
            ])
            logger.info("🧹 Limpou %d arquivo(s) de áudio em '%s'", deleted, audios_dir)
            # apaga pasta TTS inteira (se existir)
            tts_dir = audios_dir / "tts"
            if tts_dir.exists():
                _retry_rmtree(tts_dir)
                logger.info("🧹 Removeu diretório de TTS: %s", tts_dir)

        # 3) CACHE (preserva .json e o último diretório do flow)
        latest_flow = _find_latest_flow_dir(cache_dir)
        keep_root = latest_flow.resolve() if latest_flow else None

        if cache_dir.exists():
            removed_count = 0

            # remove explicitamente drawtext_* (txt, ass, etc.)
            removed_count += _delete_globs([
                str(cache_dir / "drawtext_*"),
                str(cache_dir / "drawtext_*.*"),
                str(cache_dir / "**/drawtext_*"),
                str(cache_dir / "**/drawtext_*.*"),
            ])

            # remove TODOS os arquivos não-JSON fora do último flow dir
            for p in cache_dir.rglob("*"):
                try:
                    if p.is_file():
                        if keep_root and p.resolve().is_relative_to(keep_root):
                            continue  # preserva dentro do último flow
                        if p.suffix.lower() != ".json":
                            _retry_unlink(p)
                            removed_count += 1
                except Exception:
                    # compat Python < 3.9 para is_relative_to
                    try:
                        if keep_root and str(p.resolve()).startswith(str(keep_root)):
                            continue
                        if p.is_file() and p.suffix.lower() != ".json":
                            _retry_unlink(p)
                            removed_count += 1
                    except Exception:
                        pass

            # remove diretórios vazios (exceto o último flow)
            # (varre bottom-up para tentar remover folhas primeiro)
            for d in sorted([x for x in cache_dir.rglob("*") if x.is_dir()], key=lambda x: len(str(x)), reverse=True):
                if keep_root and str(d.resolve()).startswith(str(keep_root)):
                    continue
                try:
                    if not any(d.iterdir()):
                        d.rmdir()
                except Exception:
                    pass

            if latest_flow:
                logger.info("🧹 Cache limpo. Preservado último flow: %s | Itens removidos: %d", latest_flow, removed_count)
            else:
                logger.info("🧹 Cache limpo. (Nenhum diretório de flow encontrado) | Itens removidos: %d", removed_count)

    except Exception as e:
        logger.warning("Falha na limpeza pós-render: %s", e)

def purge_after_posting(
    videos_dir: str | Path = "videos",
    cache_dir: str | Path = "cache",
) -> None:
    """
    Chame **depois** do upload/post ter sido confirmado com sucesso.
    - Apaga **toda** a pasta `videos/` (inclui `videos/prompts`).
    - Mantém `cache/` conforme limpeza anterior (não mexe aqui).
    Controle por ENV:
      - KEEP_POSTED_VIDEOS=1  => não remove `videos/` (útil p/ debug).
    """
    try:
        keep = os.getenv("KEEP_POSTED_VIDEOS", "0").strip() == "1"
        videos_dir = Path(videos_dir)

        if keep:
            logger.info("🛑 KEEP_POSTED_VIDEOS=1 — não apagarei a pasta '%s'", videos_dir)
            return

        if videos_dir.exists():
            _retry_rmtree(videos_dir)
            logger.info("🧹 Pós-postagem: diretório 'videos' removido por completo.")
        else:
            logger.info("Pós-postagem: diretório 'videos' já não existe.")

    except Exception as e:
        logger.warning("Falha na limpeza pós-postagem: %s", e)
