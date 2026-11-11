"""Fonte Reddit — r/wallpapers top do mês (JSON com fallback RSS).

Port da função ``do_download_reddit`` do wallpaper-carousel.sh:
- tenta ``top.json?t=month&limit=50``; se HTTP != 200, cai para ``top.rss``;
- aceita apenas URLs diretas i.redd.it / i.imgur.com de imagem;
- ordena por ups (JSON) e limita a ``settings.reddit_limit`` candidatos.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Iterator

import requests

from ..config import Settings
from ..state import State
from ._http import make_session
from .base import Candidate

log = logging.getLogger("wallpaper.sources.reddit")

JSON_URL = "https://www.reddit.com/r/wallpapers/top.json?t=month&limit=50"
RSS_URL = "https://www.reddit.com/r/wallpapers/top.rss?t=month"
USER_AGENT = "wallpaper-carousel/7.0-python (wallpaper carousel)"
USER_AGENT_MOZILLA = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)
TIMEOUT = 30

# Mesma regex do bash (jq test / python do fallback RSS).
IMG_URL_RE = re.compile(
    r"^https://(i\.redd\.it|i\.imgur\.com)/.+\.(jpe?g|png|webp)(\?.*)?$",
    re.IGNORECASE,
)
# Regex de extração dentro do <content> do RSS (pode haver texto ao redor).
CONTENT_IMG_RE = re.compile(
    r"https://(?:i\.redd\.it|i\.imgur\.com)/[^\s\"'<>]+?"
    r"\.(?:jpe?g|png|webp)(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)

_EXT_BY_SUFFIX = {"jpg": "jpg", "jpeg": "jpg", "png": "png", "webp": "webp"}

_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


class RedditSource:
    """Descobre candidatos via reddit.com/r/wallpapers (top/month)."""

    name = "reddit"

    def enabled(self, settings: Settings) -> bool:
        return bool(settings.reddit_enabled)

    def candidates(self, settings: Settings, state: State) -> Iterator[Candidate]:
        rows = self._fetch_rows(settings)
        if not rows:
            log.warning("Reddit sem links diretos de imagem.")
            return

        known_ids = state.load_ids()
        yielded = 0
        for mid, ups, url in rows:
            if yielded >= settings.reddit_limit:
                break
            ident = f"reddit-{mid}"
            if ident in known_ids:
                continue
            clean_url = url.split("?", 1)[0]
            ext = _EXT_BY_SUFFIX.get(clean_url.rsplit(".", 1)[-1].lower())
            if not ext:
                continue
            yielded += 1
            yield Candidate(
                source="reddit",
                ident=ident,
                name=f"{ident}.{ext}",
                url=clean_url,
                ext=ext,
                hint=float(ups),
            )

    # ------------------------------------------------------------------
    def _fetch_rows(self, settings: Settings) -> list[tuple[str, int, str]]:
        """Retorna [(post_id, ups, url)] — JSON por ups desc, RSS por id."""
        session = make_session()
        try:
            try:
                resp = session.get(
                    JSON_URL,
                    headers={"User-Agent": USER_AGENT},
                    timeout=TIMEOUT,
                )
            except requests.RequestException as exc:
                log.warning("Rede falhou (Reddit JSON): %s", exc)
                return []

            if resp.status_code == 200:
                log.info("Reddit via JSON.")
                return self._parse_json(resp)

            log.warning(
                "Reddit JSON HTTP %d; tentando RSS.", resp.status_code
            )
            time.sleep(2)
            return self._fetch_rss(session)
        finally:
            session.close()

    def _parse_json(self, resp: requests.Response) -> list[tuple[str, int, str]]:
        try:
            payload = resp.json()
        except ValueError:
            log.warning("JSON inválido da API do Reddit.")
            return []
        rows: list[tuple[str, int, str]] = []
        for child in (payload.get("data") or {}).get("children") or []:
            data = child.get("data") or {}
            url = data.get("url") or ""
            mid = data.get("id") or ""
            if not mid or not IMG_URL_RE.match(url):
                continue
            try:
                ups = int(data.get("ups") or 0)
            except (TypeError, ValueError):
                ups = 0
            rows.append((str(mid), ups, str(url)))
        rows.sort(key=lambda r: r[1], reverse=True)
        return rows

    def _fetch_rss(
        self, session: requests.Session
    ) -> list[tuple[str, int, str]]:
        try:
            resp = session.get(
                RSS_URL,
                headers={"User-Agent": USER_AGENT_MOZILLA},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            log.warning("Rede falhou (Reddit RSS): %s", exc)
            return []
        if resp.status_code != 200:
            log.warning("Reddit RSS HTTP %d", resp.status_code)
            return []
        log.info("Reddit via RSS (JSON indisponível).")
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            log.warning("RSS do Reddit inválido: %s", exc)
            return []
        rows: list[tuple[str, int, str]] = []
        for entry in root.findall("a:entry", _ATOM_NS):
            link = entry.find("a:link", _ATOM_NS)
            href = link.get("href", "") if link is not None else ""
            if "/comments/" not in href:
                continue
            mid = href.split("/comments/", 1)[1].split("/", 1)[0]
            content = entry.find("a:content", _ATOM_NS)
            text = content.text if content is not None and content.text else ""
            m = CONTENT_IMG_RE.search(text)
            if mid and m:
                rows.append((mid, 0, m.group(0)))
        rows.sort(key=lambda r: r[0])
        return rows
