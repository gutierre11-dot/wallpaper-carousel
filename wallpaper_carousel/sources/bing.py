"""Fonte Bing — Imagem do Dia (HPImageArchive, UHD).

Port da função ``do_download_bing`` do wallpaper-carousel.sh.
"""

from __future__ import annotations

import logging
from typing import Iterator

import requests

from ..config import Settings
from ..state import State
from ._http import make_session
from .base import Candidate

log = logging.getLogger("wallpaper.sources.bing")

API_URL = "https://www.bing.com/HPImageArchive.aspx"
USER_AGENT = "wallpaper-carousel/7.0-python (wallpaper carousel)"
TIMEOUT = 30
BASE_URL = "https://www.bing.com"


class BingSource:
    """Descobre candidatos via Bing Image of the Day (``bing_days`` dias)."""

    name = "bing"

    def enabled(self, settings: Settings) -> bool:
        return bool(settings.bing_enabled)

    def candidates(self, settings: Settings, state: State) -> Iterator[Candidate]:
        params = {
            "format": "js",
            "idx": 0,
            "n": settings.bing_days,
            "mkt": "en-US",
            "uhd": 1,
            "uhdwidth": 3840,
            "uhdheight": 2160,
        }
        session = make_session()
        try:
            try:
                resp = session.get(
                    API_URL,
                    params=params,
                    headers={"User-Agent": USER_AGENT},
                    timeout=TIMEOUT,
                )
            except requests.RequestException as exc:
                log.warning("Rede falhou (Bing): %s", exc)
                return
            if resp.status_code != 200:
                log.warning("Bing HTTP %d", resp.status_code)
                return
            try:
                payload = resp.json()
            except ValueError:
                log.warning("JSON inválido do Bing Image Archive.")
                return
            if not isinstance(payload, dict):
                log.warning("Resposta inesperada do Bing Image Archive.")
                return
        finally:
            session.close()

        known_ids = state.load_ids()
        images_dir = settings.images_dir
        seen: set[str] = set()
        yielded = 0
        for item in payload.get("images") or []:
            if yielded >= settings.bing_limit:
                break
            startdate = str(item.get("startdate") or "").strip()
            base = str(item.get("url") or "").strip()
            if not startdate or not base:
                continue
            ident = f"bing-{startdate}"
            name = f"{ident}.{'png' if base.split('?', 1)[0].lower().endswith('.png') else 'jpg'}"
            if ident in seen or ident in known_ids or (images_dir / name).exists():
                continue
            seen.add(ident)
            yielded += 1
            yield Candidate(
                source="bing",
                ident=ident,
                name=name,
                url=f"{BASE_URL}{base}",
                ext=name.rsplit(".", 1)[-1],
                hint=0.0,
            )
