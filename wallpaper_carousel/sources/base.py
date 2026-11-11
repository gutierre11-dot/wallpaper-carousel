"""Contrato das fontes de download."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Protocol

from ..config import Settings
from ..state import State


@dataclass(frozen=True)
class Candidate:
    """Um wallpaper candidato descoberto por uma fonte.

    - ``ident``: chave de dedup gravada em ``state/ids`` (ex.: "zpvdzv",
      "reddit-abc", "bing-20261111", "picsum-1234").
    - ``name``: nome do arquivo master em ``images/`` (ex.: "wallhaven-zpvdzv.jpg").
    """

    source: str  # "wallhaven" | "reddit" | "bing" | "picsum"
    ident: str
    name: str
    url: str
    ext: str  # "jpg" | "png" | "webp"
    hint: float = 0.0  # pista de ordenação (favorites/ups/etc.)


class Source(Protocol):
    """Interface de uma fonte de wallpapers."""

    name: str

    def enabled(self, settings: Settings) -> bool:
        """Fonte habilitada na configuração?"""
        ...

    def candidates(self, settings: Settings, state: State) -> Iterator[Candidate]:
        """Gera candidatos (lazy). Pode pular ids já conhecidos via state."""
        ...
