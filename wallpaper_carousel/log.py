"""Logging estruturado: stderr (journal) + arquivo com rotação por tamanho.

Invariante importante: NADA de log vai para stdout. stdout é reservado para
dados (ex.: nome do wallpaper escolhido, JSON). Isso elimina a classe de bug
em que mensagens de log contaminavam capturas de comando.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FMT = "%(asctime)s [%(levelname)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup(log_file: Path, max_kb: int = 1024, verbose: bool = False) -> None:
    """Configura o logger raiz uma única vez por processo."""
    global _configured
    if _configured:
        return
    _configured = True

    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)  # DEBUG de libs (PIL etc.) fica de fora

    fmt = logging.Formatter(_FMT, datefmt=_DATEFMT)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_kb * 1024,
        backupCount=1,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    err_handler = logging.StreamHandler(sys.stderr)
    err_handler.setFormatter(fmt)
    err_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(err_handler)


def get_logger(name: str = "wallpaper") -> logging.Logger:
    return logging.getLogger(name)


def rotate_if_needed(log_file: Path, max_kb: int) -> None:
    """Rotação manual compatível com o formato legado (carousel.log.1)."""
    try:
        if log_file.stat().st_size > max_kb * 1024:
            backup = log_file.with_suffix(log_file.suffix + ".1")
            if backup.exists():
                backup.unlink()
            log_file.replace(backup)
            log_file.touch()
            os.chmod(log_file, 0o644)
    except OSError:
        pass
