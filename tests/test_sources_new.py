"""Testes das novas fontes: wikimedia, nasa, wallpaperscom, openverse.

Todo o HTTP é mockado via ``make_session`` patcheado em cada módulo —
nenhum teste faz requests reais.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wallpaper_carousel.config import Settings
from wallpaper_carousel.state import State
from wallpaper_carousel.sources import nasa, openverse, wallpaperscom, wikimedia


# ---------------------------------------------------------------------------
# fixtures / fakes de HTTP
# ---------------------------------------------------------------------------

@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings.from_env({"WALLPAPER_DATA_DIR": str(tmp_path / "wc")})
    s.ensure_dirs()
    return s


@pytest.fixture()
def state(settings) -> State:
    return State(settings.state_dir)


class FakeResp:
    def __init__(self, status_code: int = 200, payload=None, bad_json: bool = False):
        self.status_code = status_code
        self._payload = payload
        self._bad_json = bad_json
        self.content = b""

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Session fake: consome `responses` em ordem para .get/.post."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.get_calls: list[dict] = []
        self.post_calls: list[dict] = []

    def _next(self, url):
        if not self.responses:
            raise AssertionError(f"FakeSession sem resposta para {url}")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def get(self, url, params=None, headers=None, timeout=None):
        self.get_calls.append(
            {"url": url, "params": params or {}, "headers": headers or {}}
        )
        return self._next(url)

    def post(self, url, data=None, headers=None, timeout=None):
        self.post_calls.append(
            {"url": url, "data": data or {}, "headers": headers or {}}
        )
        return self._next(url)

    def close(self):
        pass


def patch_session(monkeypatch, module, session: FakeSession) -> FakeSession:
    monkeypatch.setattr(module, "make_session", lambda *a, **k: session)
    return session


def collect(source, settings, state) -> list:
    return list(source.candidates(settings, state))


# ---------------------------------------------------------------------------
# Wikimedia Commons
# ---------------------------------------------------------------------------

def _wiki_page(pageid, width=5000, height=3000, mime="image/jpeg",
               with_thumb=True):
    ii = {
        "url": f"https://upload.wikimedia.org/wikipedia/commons/{pageid}/orig.jpg",
        "width": width,
        "height": height,
        "mime": mime,
        "extmetadata": {},
    }
    if with_thumb:
        ii["thumburl"] = (
            f"https://upload.wikimedia.org/wikipedia/commons/thumb/{pageid}/orig.jpg/3840px-orig.jpg"
        )
        ii["thumbwidth"] = 3840
    return {"pageid": pageid, "title": f"File:{pageid}", "imageinfo": [ii]}


def test_wikimedia_happy(monkeypatch, settings, state):
    payload = {"query": {"pages": {
        "101": _wiki_page(101),
        "102": _wiki_page(102, width=1000, height=800),      # pequena: rejeita
        "103": _wiki_page(103, width=4000, height=4000),     # quadrada: rejeita
        "104": _wiki_page(104, mime="image/gif"),            # mime ruim: rejeita
    }}}
    sess = patch_session(monkeypatch, wikimedia, FakeSession([FakeResp(200, payload)]))
    cands = collect(wikimedia.WikimediaSource(), settings, state)
    assert len(cands) == 1
    c = cands[0]
    assert c.source == "wikimedia"
    assert c.ident == "101"
    assert c.name == "wikimedia-101.jpg"
    assert c.ext == "jpg"
    assert "thumb" in c.url or "3840px" in c.url
    assert c.hint == 0.0
    # params da API conferem o contrato
    params = sess.get_calls[0]["params"]
    assert params["generator"] == "categorymembers"
    assert params["prop"] == "imageinfo"


def test_wikimedia_dedup_and_limit(monkeypatch, settings, state):
    payload = {"query": {"pages": {
        str(200 + i): _wiki_page(200 + i) for i in range(5)
    }}}
    state.add_id("201")  # ident do Wikimedia é o pageid
    patch_session(monkeypatch, wikimedia, FakeSession([FakeResp(200, payload)]))
    s2 = Settings.from_env({
        "WALLPAPER_DATA_DIR": str(settings.data_dir),
        "WALLPAPER_WIKIMEDIA_LIMIT": "2",
    })
    cands = collect(wikimedia.WikimediaSource(), s2, state)
    assert len(cands) == 2
    assert all(c.ident != "201" for c in cands)


def test_wikimedia_http500_and_bad_json(monkeypatch, settings, state):
    patch_session(monkeypatch, wikimedia, FakeSession([FakeResp(500)]))
    assert collect(wikimedia.WikimediaSource(), settings, state) == []

    patch_session(monkeypatch, wikimedia, FakeSession([FakeResp(200, bad_json=True)]))
    assert collect(wikimedia.WikimediaSource(), settings, state) == []

    patch_session(
        monkeypatch, wikimedia,
        FakeSession([requests.Timeout("boom")]),
    )
    assert collect(wikimedia.WikimediaSource(), settings, state) == []


# ---------------------------------------------------------------------------
# NASA APOD
# ---------------------------------------------------------------------------

def _nasa_item(date, url="https://apod.nasa.gov/apod/image/1.jpg",
               hdurl="https://apod.nasa.gov/apod/image/1_hd.jpg",
               media_type="image"):
    return {"date": date, "title": "t", "url": url, "hdurl": hdurl,
            "media_type": media_type, "copyright": "x"}


def test_nasa_happy(monkeypatch, settings, state):
    payload = [
        _nasa_item("2026-11-10"),
        _nasa_item("2026-11-09", url="https://apod.nasa.gov/apod/image/2.mp4",
                   hdurl=None, media_type="video"),
        _nasa_item("2026-11-08", url="https://apod.nasa.gov/apod/image/3",
                   hdurl=None),  # sem extensão: rejeita
        _nasa_item("2026-11-07", url="https://apod.nasa.gov/apod/image/4.png?x=1",
                   hdurl=None),
    ]
    sess = patch_session(monkeypatch, nasa, FakeSession([FakeResp(200, payload)]))
    cands = collect(nasa.NasaSource(), settings, state)
    assert [c.ident for c in cands] == ["nasa-2026-11-10", "nasa-2026-11-07"]
    assert cands[0].name == "nasa-2026-11-10.jpg"
    assert cands[0].url.endswith("_hd.jpg")
    assert cands[1].ext == "png"
    params = sess.get_calls[0]["params"]
    assert params["api_key"] == "DEMO_KEY"
    assert params["count"] == min(settings.nasa_limit * 4, 30)


def test_nasa_dedup_and_limit(monkeypatch, settings, state):
    payload = [_nasa_item(f"2026-11-{d:02d}") for d in range(1, 9)]
    state.add_id("nasa-2026-11-03")
    patch_session(monkeypatch, nasa, FakeSession([FakeResp(200, payload)]))
    s2 = Settings.from_env({
        "WALLPAPER_DATA_DIR": str(settings.data_dir),
        "WALLPAPER_NASA_LIMIT": "2",
    })
    cands = collect(nasa.NasaSource(), s2, state)
    assert len(cands) <= 2
    assert all(c.ident != "nasa-2026-11-03" for c in cands)


def test_nasa_errors(monkeypatch, settings, state):
    patch_session(monkeypatch, nasa, FakeSession([FakeResp(429)]))
    assert collect(nasa.NasaSource(), settings, state) == []
    patch_session(monkeypatch, nasa, FakeSession([FakeResp(200, bad_json=True)]))
    assert collect(nasa.NasaSource(), settings, state) == []
    patch_session(monkeypatch, nasa, FakeSession([requests.ConnectionError("x")]))
    assert collect(nasa.NasaSource(), settings, state) == []


# ---------------------------------------------------------------------------
# Wallpapers.com
# ---------------------------------------------------------------------------

def _wcomp_item(i, high=None, webp=None, thumb=None):
    return {
        "id": i,
        "title": f"wall {i}",
        "thumb": thumb or f"https://wallpapers.com/thumb/{i}.jpg",
        "high": high or f"https://wallpapers.com/high/{i}.jpg",
        "webp": webp or f"https://wallpapers.com/high/{i}.webp",
        "url": f"https://wallpapers.com/w/{i}",
    }


def test_wallpaperscom_happy(monkeypatch, settings, state):
    payload = {"success": True, "data": [_wcomp_item(1), _wcomp_item(2)]}
    sess = patch_session(monkeypatch, wallpaperscom, FakeSession([FakeResp(200, payload)]))
    cands = collect(wallpaperscom.WallpaperscomSource(), settings, state)
    assert len(cands) == 2
    c = cands[0]
    assert c.ident == "wallpaperscom-1"
    assert c.name == "wallpaperscom-1.jpg"
    assert c.url.endswith("/high/1.jpg")
    assert sess.get_calls[0]["url"].endswith("/random")
    assert sess.get_calls[0]["params"]["q"] == "nature"  # source_idx=0


def test_wallpaperscom_fallback_and_dedup(monkeypatch, settings, state):
    # /random falha (HTTP 500) -> fallback /keyword/nature
    fb_payload = {"results": [_wcomp_item(9, high="https://x.com/9.webp")]}
    sess = patch_session(monkeypatch, wallpaperscom,
                         FakeSession([FakeResp(500), FakeResp(200, fb_payload)]))
    cands = collect(wallpaperscom.WallpaperscomSource(), settings, state)
    assert len(sess.get_calls) == 2
    assert sess.get_calls[1]["url"].endswith("/keyword/nature")
    assert len(cands) == 1
    assert cands[0].ext == "webp"
    assert cands[0].name == "wallpaperscom-9.webp"


def test_wallpaperscom_hd_rewrite(monkeypatch, settings, state):
    """URLs /images/high/ (900px) devem virar /images/hd/ (2560px)."""
    item = _wcomp_item(
        42,
        high="https://wallpapers.com/images/high/slug-abc.jpg",
        webp="https://wallpapers.com/images/high/slug-abc.webp",
    )
    payload = {"data": [item]}
    patch_session(monkeypatch, wallpaperscom, FakeSession([FakeResp(200, payload)]))
    cands = collect(wallpaperscom.WallpaperscomSource(), settings, state)
    assert len(cands) == 1
    assert cands[0].url == "https://wallpapers.com/images/hd/slug-abc.jpg"
    assert cands[0].ext == "jpg"


def test_wallpaperscom_errors(monkeypatch, settings, state):
    patch_session(monkeypatch, wallpaperscom,
                  FakeSession([FakeResp(500), FakeResp(500)]))
    assert collect(wallpaperscom.WallpaperscomSource(), settings, state) == []
    patch_session(monkeypatch, wallpaperscom,
                  FakeSession([requests.Timeout("x"), requests.Timeout("x")]))
    assert collect(wallpaperscom.WallpaperscomSource(), settings, state) == []


def test_wallpaperscom_limit(monkeypatch, settings, state):
    payload = {"data": [_wcomp_item(i) for i in range(10)]}
    patch_session(monkeypatch, wallpaperscom, FakeSession([FakeResp(200, payload)]))
    s2 = Settings.from_env({
        "WALLPAPER_DATA_DIR": str(settings.data_dir),
        "WALLPAPER_WALLPAPERSCOM_LIMIT": "3",
    })
    assert len(collect(wallpaperscom.WallpaperscomSource(), s2, state)) == 3


# ---------------------------------------------------------------------------
# Openverse
# ---------------------------------------------------------------------------

def _ov_item(i, width=3000, height=1800, url=None):
    return {
        "id": f"ov{i}",
        "title": f"img {i}",
        "url": url or f"https://live.staticflickr.com/{i}/photo_{i}.jpg",
        "thumbnail": f"https://thumb/{i}.jpg",
        "width": width,
        "height": height,
        "license": "by",
        "foreign_landing_url": "https://example.com",
    }


def _ov_payload(items, page_count=5):
    return {"result_count": len(items), "page_count": page_count,
            "page": 1, "page_size": 20, "results": items}


def test_openverse_happy_and_params(monkeypatch, settings, state):
    settings = Settings.from_env({
        "WALLPAPER_DATA_DIR": str(settings.data_dir),
        "WALLPAPER_OPENVERSE_ENABLED": "1",
    })
    assert openverse.OpenverseSource().enabled(settings)
    assert not openverse.OpenverseSource().enabled(
        Settings.from_env({"WALLPAPER_DATA_DIR": str(settings.data_dir)})
    )

    payload = _ov_payload([_ov_item(1, width=3000, height=1800),  # ratio 1.67 ok
                           _ov_item(2, width=2000),  # pequena
                           _ov_item(3, width=4000, height=4000)])  # quadrada
    sess = patch_session(monkeypatch, openverse,
                         FakeSession([FakeResp(200, payload)]))
    cands = collect(openverse.OpenverseSource(), settings, state)
    assert len(cands) == 1
    c = cands[0]
    assert c.source == "openverse"
    assert c.name.startswith("openverse-")
    assert c.name.endswith(".jpg")
    assert len(c.ident) == 12
    params = sess.get_calls[0]["params"]
    assert params["license_type"] == "commercial"
    assert params["size"] == "large"
    assert params["q"] == "nature"
    assert "Authorization" not in sess.get_calls[0]["headers"]  # anônimo


def test_openverse_dedup_limit_errors(monkeypatch, settings, state):
    settings = Settings.from_env({
        "WALLPAPER_DATA_DIR": str(settings.data_dir),
        "WALLPAPER_OPENVERSE_ENABLED": "1",
        "WALLPAPER_OPENVERSE_LIMIT": "1",
    })
    payload = _ov_payload([_ov_item(i) for i in range(5)])
    patch_session(monkeypatch, openverse, FakeSession([FakeResp(200, payload)]))
    cands = collect(openverse.OpenverseSource(), settings, state)
    assert len(cands) == 1

    # dedup por ident: payload só com o item já conhecido -> nada yielda
    state.add_id(cands[0].ident)
    only_known = _ov_payload([_ov_item(0)])
    patch_session(monkeypatch, openverse, FakeSession([FakeResp(200, only_known)]))
    assert collect(openverse.OpenverseSource(), settings, state) == []

    # HTTP 401 -> warning e fim
    patch_session(monkeypatch, openverse, FakeSession([FakeResp(401)]))
    assert collect(openverse.OpenverseSource(), settings, state) == []
    # HTTP 500 / JSON ruim / timeout
    patch_session(monkeypatch, openverse, FakeSession([FakeResp(500)]))
    assert collect(openverse.OpenverseSource(), settings, state) == []
    patch_session(monkeypatch, openverse, FakeSession([FakeResp(200, bad_json=True)]))
    assert collect(openverse.OpenverseSource(), settings, state) == []
    patch_session(monkeypatch, openverse, FakeSession([requests.Timeout("x")]))
    assert collect(openverse.OpenverseSource(), settings, state) == []


def test_openverse_bearer_token_passthrough(monkeypatch, settings, state):
    settings = Settings.from_env({
        "WALLPAPER_DATA_DIR": str(settings.data_dir),
        "WALLPAPER_OPENVERSE_ENABLED": "1",
        "WALLPAPER_OPENVERSE_TOKEN": "ya29.directtoken",
    })
    payload = _ov_payload([_ov_item(7)])
    sess = patch_session(monkeypatch, openverse, FakeSession([FakeResp(200, payload)]))
    cands = collect(openverse.OpenverseSource(), settings, state)
    assert len(cands) == 1
    assert sess.get_calls[0]["headers"]["Authorization"] == "Bearer ya29.directtoken"
    assert not sess.post_calls  # token direto não troca credenciais


def test_openverse_oauth_client_credentials(monkeypatch, settings, state):
    openverse._TOKEN_CACHE.update({"token": None, "expires_at": 0.0})
    settings = Settings.from_env({
        "WALLPAPER_DATA_DIR": str(settings.data_dir),
        "WALLPAPER_OPENVERSE_ENABLED": "1",
        "WALLPAPER_OPENVERSE_TOKEN": "myid:mysecret",
    })
    token_resp = FakeResp(200, {"access_token": "tok123", "expires_in": 21600})
    payload = _ov_payload([_ov_item(8)])
    sess = patch_session(monkeypatch, openverse,
                         FakeSession([token_resp, FakeResp(200, payload)]))
    cands = collect(openverse.OpenverseSource(), settings, state)
    assert len(cands) == 1
    assert sess.post_calls[0]["url"].endswith("/auth_tokens/token/")
    assert sess.post_calls[0]["data"]["client_id"] == "myid"
    assert sess.get_calls[0]["headers"]["Authorization"] == "Bearer tok123"
    # segunda chamada usa cache (sem novo POST)
    patch_session(monkeypatch, openverse, FakeSession([FakeResp(200, payload)]))
    collect(openverse.OpenverseSource(), settings, state)
