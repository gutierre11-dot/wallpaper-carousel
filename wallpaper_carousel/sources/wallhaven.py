"""Fonte Wallhaven — API v1 de busca (sem chave de API).

Port da função ``do_download_wallhaven`` do wallpaper-carousel.sh:
- 5 estratégias rotativas indexadas por ``state.source_idx``;
- temas rotativos de ``settings.themes`` indexados por ``state.theme_idx``;
- fallback "broad" (sem ``q``) quando a busca com tema não retorna nada;
- paginação 1..settings.pages com ``api_sleep`` entre chamadas.
"""

from __future__ import annotations

import logging
import time
from typing import Iterator

import requests

from ..config import Settings
from ..state import State
from ._http import make_session
from .base import Candidate

log = logging.getLogger("wallpaper.sources.wallhaven")

API_URL = "https://wallhaven.cc/api/v1/search"
USER_AGENT = "wallpaper-carousel/7.0-python (wallpaper carousel; +https://wallhaven.cc)"
TIMEOUT = 30

# (sorting, topRange|None, broad) — idêntico a wh_strategy() do bash.
_STRATEGIES: tuple[tuple[str, str | None, bool], ...] = (
    ("toplist", "1w", False),
    ("toplist", "1M", False),
    ("toplist", "6M", False),
    ("date_added", None, False),
    ("toplist", "1M", True),
)
_N_STRATEGIES = len(_STRATEGIES)

_EXT_BY_FILE_TYPE = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


class WallhavenSource:
    """Descobre candidatos via https://wallhaven.cc/api/v1/search."""

    name = "wallhaven"

    def enabled(self, settings: Settings) -> bool:
        # O bash não tem flag para wallhaven (sempre roda no modo 'all');
        # getattr permite habilitar via settings.wallhaven_enabled no futuro.
        return bool(getattr(settings, "wallhaven_enabled", True))

    def candidates(self, settings: Settings, state: State) -> Iterator[Candidate]:
        # Contadores são lidos e incrementados uma única vez por execução,
        # antes da busca — garante avanço mesmo se o consumidor abandonar
        # o iterador no meio (equivale ao bash, que sempre grava no fim).
        strategy_idx = state.source_idx % _N_STRATEGIES
        themes = settings.themes
        theme_idx = state.theme_idx % len(themes) if themes else 0
        theme = themes[theme_idx] if themes else ""
        state.source_idx = (strategy_idx + 1) % _N_STRATEGIES
        if themes:
            state.theme_idx = (theme_idx + 1) % len(themes)

        sorting, top_range, broad = _STRATEGIES[strategy_idx]
        q_label = "—" if (broad or theme in ("", "-")) else theme
        log.info(
            "Wallhaven: estratégia %d (%s, topRange=%s, broad=%s) tema='%s'",
            strategy_idx, sorting, top_range, broad, q_label,
        )

        rows = self._fetch_all(settings, sorting, top_range, broad, theme)
        if not rows:
            log.warning("Wallhaven sem resultados (estratégia %d, tema '%s').",
                        strategy_idx, q_label)
            return

        known_ids = state.load_ids()
        images_dir = settings.images_dir
        seen: set[str] = set()
        for fav, wid, path, ext in rows:
            if wid in seen or wid in known_ids:
                continue
            seen.add(wid)
            name = f"wallhaven-{wid}.{ext}"
            if (images_dir / name).exists():
                continue
            yield Candidate(
                source="wallhaven",
                ident=wid,
                name=name,
                url=path,
                ext=ext,
                hint=float(fav),
            )

    # ------------------------------------------------------------------
    def _fetch_all(
        self,
        settings: Settings,
        sorting: str,
        top_range: str | None,
        broad: bool,
        theme: str,
    ) -> list[tuple[int, str, str, str]]:
        """Coleta (favorites, id, path, ext) de todas as páginas/estratégias.

        Retorna ordenado por favorites desc. Em caso de 0 resultados com
        tema, refaz a estratégia uma vez com broad=1 (sem 'q').
        """
        for attempt in range(2):
            rows = self._fetch_pages(settings, sorting, top_range, broad, theme)
            if rows:
                break
            if attempt == 0 and not broad and theme not in ("", "-"):
                log.warning(
                    "Sem resultados com o tema '%s'; repetindo a estratégia sem 'q'.",
                    theme,
                )
                broad = True
            else:
                break
        rows.sort(key=lambda r: r[0], reverse=True)
        return rows

    def _fetch_pages(
        self,
        settings: Settings,
        sorting: str,
        top_range: str | None,
        broad: bool,
        theme: str,
    ) -> list[tuple[int, str, str, str]]:
        """Busca as páginas 1..settings.pages; nunca levanta exceção."""
        w, h = settings.resolution
        params_base = {
            "sorting": sorting,
            "order": "desc",
            "atleast": f"{w}x{h}",
            "ratios": ",".join(f"{rw}x{rh}" for rw, rh in settings.ratios),
            "categories": settings.categories,
            "purity": settings.purity,
        }
        if sorting == "toplist" and top_range:
            params_base["topRange"] = top_range
        # requests codifica espaço como '+' via quote_plus — paridade com o bash.
        use_q = not broad and theme not in ("", "-")
        if use_q:
            params_base["q"] = theme

        rows: list[tuple[int, str, str, str]] = []
        session = make_session()
        try:
            for page in range(1, settings.pages + 1):
                params = dict(params_base, page=page)
                try:
                    resp = session.get(
                        API_URL,
                        params=params,
                        headers={"User-Agent": USER_AGENT},
                        timeout=TIMEOUT,
                    )
                except requests.RequestException as exc:
                    log.warning("Rede falhou p.%d (%s); tentando próxima página.",
                                page, exc)
                    time.sleep(settings.api_sleep)
                    continue
                if resp.status_code != 200:
                    log.warning("HTTP %d p.%d da API Wallhaven.",
                                resp.status_code, page)
                    time.sleep(settings.api_sleep)
                    continue
                try:
                    payload = resp.json()
                except ValueError:
                    log.warning("JSON inválido p.%d da API Wallhaven.", page)
                    time.sleep(settings.api_sleep)
                    continue
                for item in payload.get("data") or []:
                    wid = item.get("id")
                    path = item.get("path")
                    ftype = item.get("file_type")
                    ext = _EXT_BY_FILE_TYPE.get(ftype or "")
                    if not wid or not path or not ext:
                        continue
                    try:
                        fav = int(item.get("favorites") or 0)
                    except (TypeError, ValueError):
                        fav = 0
                    rows.append((fav, str(wid), str(path), ext))
                time.sleep(settings.api_sleep)
        finally:
            session.close()
        return rows
