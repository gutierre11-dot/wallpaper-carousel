"""Fonte Lorem Picsum — imagens aleatórias públicas (sem chave).

Port da função ``do_download_picsum`` do wallpaper-carousel.sh.
O ``$RANDOM`` do bash é substituído por ``secrets.randbelow``.
"""

from __future__ import annotations

import logging
import secrets
from typing import Iterator

from ..config import Settings
from ..state import State
from .base import Candidate

log = logging.getLogger("wallpaper.sources.picsum")

URL_TEMPLATE = "https://picsum.photos/2560/1440?random={rid}"
# Mesmo range aproximado do $RANDOM do bash (0..32767), com folga.
_RID_MAX = 1_000_000


class PicsumSource:
    """Gera ``settings.picsum_limit`` candidatos aleatórios do Picsum."""

    name = "picsum"

    def enabled(self, settings: Settings) -> bool:
        return bool(settings.picsum_enabled)

    def candidates(self, settings: Settings, state: State) -> Iterator[Candidate]:
        known_ids = state.load_ids()
        images_dir = settings.images_dir
        seen: set[str] = set()
        # O bash faz no máximo PICSUM_LIMIT tentativas, pulando arquivos
        # já existentes — replicado aqui (cada iteração = uma tentativa).
        for _ in range(settings.picsum_limit):
            rid = secrets.randbelow(_RID_MAX)
            ident = f"picsum-{rid}"
            name = f"{ident}.jpg"
            if ident in seen or ident in known_ids or (images_dir / name).exists():
                continue
            seen.add(ident)
            yield Candidate(
                source="picsum",
                ident=ident,
                name=name,
                url=URL_TEMPLATE.format(rid=rid),
                ext="jpg",
                hint=0.0,
            )
