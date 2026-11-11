"""Fonte Openverse — API de imagens da WordPress (api.openverse.org).

- ``license_type=commercial``, ``size=large``, ``page_size=20``;
- auth opcional: ``openverse_token`` no formato ``client_id:client_secret``
  troca por access_token via OAuth2 client_credentials (cache em memória);
  token sem ``:`` é usado diretamente como Bearer; vazio = anônimo (~5 req/h);
- exige landscape com largura >= 2560px.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Iterator

import requests

from ..config import Settings
from ..state import State
from ._http import make_session
from .base import Candidate

log = logging.getLogger("wallpaper.sources.openverse")

API_URL = "https://api.openverse.org/v1/images/"
TOKEN_URL = "https://api.openverse.org/v1/auth_tokens/token/"
USER_AGENT = "wallpaper-carousel/7.1-python (wallpaper carousel; Openverse)"
TIMEOUT = 30

QUERIES: tuple[str, ...] = (
    "nature",
    "landscape",
    "mountain",
    "sunset",
    "forest",
)
_N_QUERIES = len(QUERIES)
_MAX_PAGES = 2
_PAGE_SIZE = 20

_EXT_BY_SUFFIX = {"jpg": "jpg", "jpeg": "jpg", "png": "png", "webp": "webp"}

# Cache em memória do access_token OAuth2 (expira ~6h; renovamos com folga).
# Inclui o client_id para não reutilizar token de outra credencial.
_TOKEN_CACHE: dict = {"key": None, "token": None, "expires_at": 0.0}
_anon_logged = False


def _ext_from_url(url: str) -> str | None:
    path = url.split("?", 1)[0].split("#", 1)[0]
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return None
    return _EXT_BY_SUFFIX.get(name.rsplit(".", 1)[-1].lower())


def _to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _access_token(session, settings: Settings) -> str | None:
    """Resolve o Bearer token; None = anônimo."""
    raw = (settings.openverse_token or "").strip()
    if not raw:
        global _anon_logged
        if not _anon_logged:
            log.info("Openverse em modo anônimo (limite ~5 req/h).")
            _anon_logged = True
        return None
    if ":" not in raw:
        return raw  # access_token direto

    client_id, _, client_secret = raw.partition(":")
    now = time.time()
    if (
        _TOKEN_CACHE["key"] == client_id
        and _TOKEN_CACHE["token"]
        and _TOKEN_CACHE["expires_at"] > now + 60
    ):
        return _TOKEN_CACHE["token"]

    try:
        resp = session.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        log.warning("Falha de rede ao obter token Openverse: %s", exc)
        return None
    if resp.status_code != 200:
        log.warning("Openverse token HTTP %d; seguindo anônimo.", resp.status_code)
        return None
    try:
        payload = resp.json()
    except ValueError:
        log.warning("JSON inválido na resposta de token do Openverse.")
        return None
    token = payload.get("access_token")
    if not token:
        log.warning("Openverse não retornou access_token.")
        return None
    try:
        expires_in = float(payload.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0.0
    _TOKEN_CACHE["key"] = client_id
    _TOKEN_CACHE["token"] = str(token)
    _TOKEN_CACHE["expires_at"] = now + max(expires_in, 300.0)
    return _TOKEN_CACHE["token"]


class OpenverseSource:
    """Descobre candidatos via Openverse (imagens, licença comercial)."""

    name = "openverse"

    def enabled(self, settings: Settings) -> bool:
        return bool(settings.openverse_enabled)

    def candidates(self, settings: Settings, state: State) -> Iterator[Candidate]:
        rot = state.source_idx
        query = QUERIES[rot % _N_QUERIES]
        # Página inicial rotativa (1 ou 2) para variar a janela.
        page_start = 1 + (rot % _MAX_PAGES)
        state.source_idx = rot + 1
        log.info("Openverse: query '%s' a partir da página %d.", query, page_start)

        known_ids = state.load_ids()
        images_dir = settings.images_dir
        seen: set[str] = set()
        yielded = 0
        page_count = _MAX_PAGES + page_start  # teto generoso; ajustado pela API

        session = make_session()
        try:
            token = _access_token(session, settings)
            headers = {"User-Agent": USER_AGENT}
            if token:
                headers["Authorization"] = f"Bearer {token}"

            fetched = 0
            page = page_start
            while fetched < _MAX_PAGES and page <= page_count and yielded < settings.openverse_limit:
                try:
                    resp = session.get(
                        API_URL,
                        params={
                            "q": query,
                            "license_type": "commercial",
                            "page_size": _PAGE_SIZE,
                            "page": page,
                            "size": "large",
                        },
                        headers=headers,
                        timeout=TIMEOUT,
                    )
                except requests.RequestException as exc:
                    log.warning("Rede falhou (Openverse p.%d): %s", page, exc)
                    return
                if resp.status_code in (401, 403):
                    log.warning(
                        "Openverse HTTP %d (token inválido/expirado ou "
                        "limite anônimo); encerrando.",
                        resp.status_code,
                    )
                    return
                if resp.status_code != 200:
                    log.warning("Openverse HTTP %d p.%d", resp.status_code, page)
                    return
                try:
                    payload = resp.json()
                except ValueError:
                    log.warning("JSON inválido do Openverse (p.%d).", page)
                    return
                if not isinstance(payload, dict):
                    log.warning("Resposta inesperada do Openverse (p.%d).", page)
                    return

                try:
                    pc = int(payload.get("page_count") or 0)
                except (TypeError, ValueError):
                    pc = 0
                if pc > 0:
                    page_count = min(page_count, pc)

                results = payload.get("results") or []
                for result in results:
                    if yielded >= settings.openverse_limit:
                        break
                    cand = self._to_candidate(
                        result, known_ids, images_dir, seen, settings
                    )
                    if cand is None:
                        continue
                    seen.add(cand.ident)
                    yielded += 1
                    yield cand

                fetched += 1
                page += 1
                if pc > 0 and page > pc:
                    break
                # Página incompleta = última do resultado; evita request extra.
                if len(results) < _PAGE_SIZE:
                    break
        finally:
            session.close()

        log.info("Openverse: %d candidatos.", yielded)

    # ------------------------------------------------------------------
    def _to_candidate(
        self, result, known_ids: set[str], images_dir, seen: set[str],
        settings: Settings,
    ) -> Candidate | None:
        if not isinstance(result, dict):
            return None
        url = str(result.get("url") or "")
        if not url:
            return None
        ext = _ext_from_url(url)
        if not ext:
            return None
        width = _to_int(result.get("width"))
        height = _to_int(result.get("height"))
        min_w, min_h = settings.resolution
        if width < min_w or height < min_h or height >= width:
            return None
        ratio = width / height
        if not (settings.min_ratio <= ratio <= settings.max_ratio):
            return None
        ident = hashlib.md5(url.encode()).hexdigest()[:12]
        if ident in seen or ident in known_ids:
            return None
        name = f"openverse-{ident}.{ext}"
        if (images_dir / name).exists():
            return None
        return Candidate(
            source="openverse",
            ident=ident,
            name=name,
            url=url,
            ext=ext,
            hint=0.0,
        )
