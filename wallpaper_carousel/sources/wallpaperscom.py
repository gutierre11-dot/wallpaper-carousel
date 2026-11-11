"""Fonte Wallpapers.com — API v1 anônima.

Tenta ``GET /random?type=wallpaper&q=<query>&count=<n>``; se falhar
(HTTP != 200 ou JSON sem itens), cai para ``GET /keyword/nature?limit=60``.
Queries rotativas indexadas por ``state.source_idx``.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Iterator

import requests

from ..config import Settings
from ..state import State
from ._http import make_session
from .base import Candidate

log = logging.getLogger("wallpaper.sources.wallpaperscom")

API_BASE = "https://wallpapers.com/api/v1"
USER_AGENT = "wallpaper-carousel/7.1-python (wallpaper carousel; wallpapers.com)"
TIMEOUT = 30

QUERIES: tuple[str, ...] = (
    "nature",
    "landscape",
    "mountain",
    "forest",
    "ocean",
)
_N_QUERIES = len(QUERIES)

_EXT_BY_SUFFIX = {"jpg": "jpg", "jpeg": "jpg", "png": "png", "webp": "webp"}


def _ext_from_url(url: str) -> str | None:
    path = url.split("?", 1)[0].split("#", 1)[0]
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return None
    return _EXT_BY_SUFFIX.get(name.rsplit(".", 1)[-1].lower())


def _extract_items(payload) -> list:
    """Tolerante: lista direta, payload['data'|'results'|'items'|'wallpapers']."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "results", "items", "wallpapers"):
        val = payload.get(key)
        if isinstance(val, list):
            return val
    return []


class WallpaperscomSource:
    """Descobre candidatos via wallpapers.com/api/v1."""

    name = "wallpaperscom"

    def enabled(self, settings: Settings) -> bool:
        return bool(settings.wallpaperscom_enabled)

    def candidates(self, settings: Settings, state: State) -> Iterator[Candidate]:
        q_idx = state.source_idx % _N_QUERIES
        query = QUERIES[q_idx]
        state.source_idx += 1  # monotônico: outras fontes compartilham o contador
        count = max(settings.wallpaperscom_limit * 3, 10)
        log.info("Wallpapers.com: query '%s'.", query)

        session = make_session()
        try:
            items = self._fetch_random(session, query, count)
            if not items:
                log.warning(
                    "Wallpapers.com /random falhou; tentando /keyword/%s.", query
                )
                items = self._fetch_keyword(session, query)
        finally:
            session.close()

        if not items:
            log.warning("Wallpapers.com sem resultados.")
            return

        known_ids = state.load_ids()
        images_dir = settings.images_dir
        seen: set[str] = set()
        yielded = 0
        for item in items:
            if yielded >= settings.wallpaperscom_limit:
                break
            if not isinstance(item, dict):
                continue
            # Prefere 'high'; 'webp' como alternativa; 'thumb' é pequeno demais.
            # /images/high/ serve no máx. ~900px; /images/hd/ serve 2560px.
            url = str(item.get("high") or item.get("webp") or item.get("url") or "")
            url = url.replace("/images/high/", "/images/hd/")
            if not url:
                continue
            ext = _ext_from_url(url)
            if not ext:
                continue
            raw_id = item.get("id")
            if raw_id not in (None, ""):
                ident = f"wallpaperscom-{raw_id}"
            else:
                ident = "wallpaperscom-" + hashlib.md5(url.encode()).hexdigest()[:12]
            if ident in seen or ident in known_ids:
                continue
            name = f"{ident}.{ext}"
            if (images_dir / name).exists():
                continue
            seen.add(ident)
            yielded += 1
            yield Candidate(
                source="wallpaperscom",
                ident=ident,
                name=name,
                url=url,
                ext=ext,
                hint=0.0,
            )
        log.info("Wallpapers.com: %d candidatos.", yielded)

    # ------------------------------------------------------------------
    def _get_json(self, session, url: str, params: dict):
        """GET tolerante; retorna (status_code, payload|None)."""
        try:
            resp = session.get(
                url,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            log.warning("Rede falhou (Wallpapers.com %s): %s", url, exc)
            return 0, None
        if resp.status_code != 200:
            log.warning("Wallpapers.com HTTP %d em %s", resp.status_code, url)
            return resp.status_code, None
        try:
            return resp.status_code, resp.json()
        except ValueError:
            log.warning("JSON inválido de Wallpapers.com (%s).", url)
            return resp.status_code, None

    def _fetch_random(self, session, query: str, count: int) -> list:
        _, payload = self._get_json(
            session,
            f"{API_BASE}/random",
            {"type": "wallpaper", "q": query, "count": count},
        )
        return _extract_items(payload)

    def _fetch_keyword(self, session, query: str) -> list:
        _, payload = self._get_json(
            session,
            f"{API_BASE}/keyword/{query}",
            {"limit": 60},
        )
        return _extract_items(payload)
