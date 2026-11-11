"""Fonte Wikimedia Commons — categoria rotativa via API ``action=query``.

- Categorias fixas de paisagem/natureza, rotacionadas por ``state.source_idx``;
- paginação por ``continue.gcmcontinue`` (máx. 2 páginas por execução);
- exige landscape com largura >= 2560px e mime jpeg/png/webp;
- prefere ``thumburl`` (3840px) quando disponível.
"""

from __future__ import annotations

import logging
from typing import Iterator

import requests

from ..config import Settings
from ..state import State
from ._http import make_session
from .base import Candidate

log = logging.getLogger("wallpaper.sources.wikimedia")

API_URL = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = (
    "wallpaper-carousel/7.1-python "
    "(wallpaper carousel; Wikimedia Commons API; contact: local user)"
)
TIMEOUT = 30

CATEGORIES: tuple[str, ...] = (
    "Category:Landscapes",
    "Category:Mountains",
    "Category:Sunsets",
    "Category:Fog",
    "Category:Aurora_(astronomy)",
    "Category:Waterfalls",
    "Category:Beaches",
)
_N_CATEGORIES = len(CATEGORIES)
_MAX_PAGES = 2
_MIN_WIDTH = 2560

_EXT_BY_MIME = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
_EXT_BY_SUFFIX = {"jpg": "jpg", "jpeg": "jpg", "png": "png", "webp": "webp"}


def _to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _ext_from_url(url: str) -> str | None:
    path = url.split("?", 1)[0].split("#", 1)[0]
    if "." not in path.rsplit("/", 1)[-1]:
        return None
    return _EXT_BY_SUFFIX.get(path.rsplit(".", 1)[-1].lower())


class WikimediaSource:
    """Descobre candidatos em categorias do Wikimedia Commons."""

    name = "wikimedia"

    def enabled(self, settings: Settings) -> bool:
        return bool(settings.wikimedia_enabled)

    def candidates(self, settings: Settings, state: State) -> Iterator[Candidate]:
        cat_idx = state.source_idx % _N_CATEGORIES
        category = CATEGORIES[cat_idx]
        # Avança a rotação já na entrada — monotônico: o contador é
        # compartilhado com as demais fontes.
        state.source_idx += 1
        log.info("Wikimedia: categoria '%s' (índice %d).", category, cat_idx)

        known_ids = state.load_ids()
        images_dir = settings.images_dir
        seen: set[str] = set()
        yielded = 0
        gcmcontinue: str | None = None

        session = make_session()
        try:
            for _page in range(_MAX_PAGES):
                if yielded >= settings.wikimedia_limit:
                    break
                params = {
                    "action": "query",
                    "generator": "categorymembers",
                    "gcmtitle": category,
                    "gcmlimit": 50,
                    "gcmtype": "file",
                    "prop": "imageinfo",
                    "iiprop": "url|size|mime|extmetadata",
                    "iiurlwidth": 3840,
                    "format": "json",
                    "origin": "*",
                }
                if gcmcontinue:
                    params["gcmcontinue"] = gcmcontinue
                try:
                    resp = session.get(
                        API_URL,
                        params=params,
                        headers={"User-Agent": USER_AGENT},
                        timeout=TIMEOUT,
                    )
                except requests.RequestException as exc:
                    log.warning("Rede falhou (Wikimedia Commons): %s", exc)
                    return
                if resp.status_code != 200:
                    log.warning("Wikimedia Commons HTTP %d", resp.status_code)
                    return
                try:
                    payload = resp.json()
                except ValueError:
                    log.warning("JSON inválido da API do Wikimedia Commons.")
                    return

                if not isinstance(payload, dict):
                    log.warning("Resposta inesperada da API do Wikimedia Commons.")
                    return
                query = payload.get("query") or {}
                pages = query.get("pages") or {}
                if not isinstance(pages, dict):
                    pages = {}
                for page_key, pobj in pages.items():
                    if yielded >= settings.wikimedia_limit:
                        break
                    cand = self._to_candidate(
                        pobj, page_key, known_ids, images_dir, seen, settings
                    )
                    if cand is None:
                        continue
                    seen.add(cand.ident)
                    yielded += 1
                    yield cand

                gcmcontinue = (payload.get("continue") or {}).get("gcmcontinue")
                if not gcmcontinue:
                    break
        finally:
            session.close()

        log.info(
            "Wikimedia: %d candidatos ('%s').", yielded, category
        )

    # ------------------------------------------------------------------
    def _to_candidate(
        self,
        pobj: dict,
        page_key: str,
        known_ids: set[str],
        images_dir,
        seen: set[str],
        settings: Settings,
    ) -> Candidate | None:
        """Converte um objeto ``query.pages[*]`` em Candidate ou None."""
        infos = pobj.get("imageinfo") or []
        if not infos or not isinstance(infos[0], dict):
            return None
        ii = infos[0]
        meta = ii.get("extmetadata") or {}
        mime = str(
            ii.get("mime")
            or (meta.get("MimeType") or {}).get("value")
            or ""
        ).lower()
        ext = _EXT_BY_MIME.get(mime)
        if ext is None:
            return None

        width = _to_int(ii.get("width"))
        height = _to_int(ii.get("height"))
        # Precisa ser landscape grande o suficiente para wallpaper e
        # dentro da faixa de aspecto/resolução aceita pelo quality gate
        # (evita gastar download em imagens que serão rejeitadas).
        min_w, min_h = settings.resolution
        if width < min_w or height < min_h or height >= width:
            return None
        ratio = width / height
        if not (settings.min_ratio <= ratio <= settings.max_ratio):
            return None

        url = str(ii.get("url") or "")
        thumb = str(ii.get("thumburl") or "")
        thumb_w = _to_int(ii.get("thumbwidth"))
        if thumb and (thumb_w == 0 or thumb_w >= _MIN_WIDTH):
            chosen = thumb
            chosen_ext = _ext_from_url(thumb) or ext
        else:
            chosen = url
            chosen_ext = ext
        if not chosen:
            return None

        ident = str(pobj.get("pageid") or page_key)
        if not ident or ident in seen or ident in known_ids:
            return None
        name = f"wikimedia-{ident}.{chosen_ext}"
        if (images_dir / name).exists():
            return None
        return Candidate(
            source="wikimedia",
            ident=ident,
            name=name,
            url=chosen,
            ext=chosen_ext,
            hint=0.0,
        )
