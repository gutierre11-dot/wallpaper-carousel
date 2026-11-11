"""Fontes de download de wallpapers."""

from __future__ import annotations

from ..config import Settings
from .base import Candidate, Source
from .bing import BingSource
from .picsum import PicsumSource
from .reddit import RedditSource
from .wallhaven import WallhavenSource

ALL_SOURCES: dict[str, Source] = {
    "wallhaven": WallhavenSource(),
    "reddit": RedditSource(),
    "bing": BingSource(),
    "picsum": PicsumSource(),
}

__all__ = [
    "ALL_SOURCES",
    "Candidate",
    "Source",
    "get_source",
    "all_enabled",
]


def get_source(name: str) -> Source | None:
    """Retorna a fonte pelo nome ("wallhaven"|"reddit"|"bing"|"picsum")."""
    return ALL_SOURCES.get(name)


def all_enabled(settings: Settings) -> list[Source]:
    """Fontes habilitadas na configuração, na ordem do registry."""
    return [src for src in ALL_SOURCES.values() if src.enabled(settings)]
