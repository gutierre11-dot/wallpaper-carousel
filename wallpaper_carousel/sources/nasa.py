"""Fonte NASA APOD — Astronomy Picture of the Day.

Endpoint público com ``api_key=DEMO_KEY`` (30 req/h, oficial da NASA).
A API não informa dimensões; o quality gate do pipeline valida depois.
"""

from __future__ import annotations

import logging
from typing import Iterator

import requests

from ..config import Settings
from ..state import State
from ._http import make_session
from .base import Candidate

log = logging.getLogger("wallpaper.sources.nasa")

API_URL = "https://api.nasa.gov/planetary/apod"
API_KEY = "DEMO_KEY"
USER_AGENT = "wallpaper-carousel/7.1-python (wallpaper carousel; NASA APOD)"
TIMEOUT = 30

_EXT_BY_SUFFIX = {"jpg": "jpg", "jpeg": "jpg", "png": "png", "webp": "webp"}


def _ext_from_url(url: str) -> str | None:
    path = url.split("?", 1)[0].split("#", 1)[0]
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return None
    return _EXT_BY_SUFFIX.get(name.rsplit(".", 1)[-1].lower())


class NasaSource:
    """Descobre candidatos via NASA APOD (``nasa_limit`` por execução)."""

    name = "nasa"

    def enabled(self, settings: Settings) -> bool:
        return bool(settings.nasa_enabled)

    def candidates(self, settings: Settings, state: State) -> Iterator[Candidate]:
        count = min(settings.nasa_limit * 4, 30)
        session = make_session()
        try:
            try:
                resp = session.get(
                    API_URL,
                    params={"api_key": API_KEY, "count": count},
                    headers={"User-Agent": USER_AGENT},
                    timeout=TIMEOUT,
                )
            except requests.RequestException as exc:
                log.warning("Rede falhou (NASA APOD): %s", exc)
                return
            if resp.status_code != 200:
                log.warning("NASA APOD HTTP %d", resp.status_code)
                return
            try:
                payload = resp.json()
            except ValueError:
                log.warning("JSON inválido da API do NASA APOD.")
                return
        finally:
            session.close()

        if not isinstance(payload, list) or not payload:
            log.warning("NASA APOD sem lista de resultados.")
            return

        # Rotaciona a janela pelos itens usando source_idx como offset.
        offset = state.source_idx % len(payload)
        state.source_idx += 1
        items = payload[offset:] + payload[:offset]

        known_ids = state.load_ids()
        images_dir = settings.images_dir
        seen: set[str] = set()
        yielded = 0
        for item in items:
            if yielded >= settings.nasa_limit:
                break
            if not isinstance(item, dict):
                continue
            if str(item.get("media_type") or "") != "image":
                continue
            url = str(item.get("hdurl") or item.get("url") or "")
            ext = _ext_from_url(url)
            if not url or not ext:
                continue
            date = str(item.get("date") or "").strip()
            if not date:
                continue
            ident = f"nasa-{date}"
            if ident in seen or ident in known_ids:
                continue
            name = f"{ident}.{ext}"
            if (images_dir / name).exists():
                continue
            seen.add(ident)
            yielded += 1
            yield Candidate(
                source="nasa",
                ident=ident,
                name=name,
                url=url,
                ext=ext,
                hint=0.0,
            )
        log.info("NASA APOD: %d candidatos.", yielded)
