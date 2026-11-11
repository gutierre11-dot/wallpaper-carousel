"""Fontes de download de wallpapers."""

from __future__ import annotations

from ..config import Settings
from .base import Candidate, Source
from .bing import BingSource
from .nasa import NasaSource
from .openverse import OpenverseSource
from .picsum import PicsumSource
from .reddit import RedditSource
from .wallhaven import WallhavenSource
from .wallpaperscom import WallpaperscomSource
from .wikimedia import WikimediaSource

# Ordem = prioridade de download (fontes premium primeiro).
ALL_SOURCES: dict[str, Source] = {
    "wallhaven": WallhavenSource(),
    "wikimedia": WikimediaSource(),
    "nasa": NasaSource(),
    "wallpaperscom": WallpaperscomSource(),
    "picsum": PicsumSource(),
    "bing": BingSource(),
    "openverse": OpenverseSource(),
    "reddit": RedditSource(),  # legado: API anônima morta; RSS pode voltar
}

__all__ = [
    "ALL_SOURCES",
    "Candidate",
    "Source",
    "get_source",
    "all_enabled",
]


def get_source(name: str) -> Source | None:
    """Retorna a fonte pelo nome."""
    return ALL_SOURCES.get(name)


def all_enabled(settings: Settings) -> list[Source]:
    """Fontes habilitadas na configuração, na ordem do registry."""
    return [src for src in ALL_SOURCES.values() if src.enabled(settings)]
