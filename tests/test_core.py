"""Testes do núcleo: config, state, imaging, pipeline, scoring, sources."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wallpaper_carousel.config import ConfigError, Settings
from wallpaper_carousel.imaging import (
    build_screen_cache,
    compute_features,
    dhash,
    hamming,
    is_image_file,
    needs_screen_cache,
    probe,
    resolve_screen_path,
    screen_cache_path,
    screen_cache_valid,
)
from wallpaper_carousel.pipeline import accept_image, check_quality
from wallpaper_carousel.state import State, is_master_name, master_name_from_any


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings.from_env({"WALLPAPER_DATA_DIR": str(tmp_path / "wc")})
    s.ensure_dirs()
    return s


@pytest.fixture()
def state(settings) -> State:
    return State(settings.state_dir)


def make_image(path: Path, w: int = 2560, h: int = 1440, color=(120, 80, 40)) -> Path:
    """JPEG com gradiente simples (não é cor sólida)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (w, h), color)
    px = img.load()
    for y in range(0, h, 16):
        for x in range(0, w, 16):
            px[x, y] = ((color[0] + x) % 256, (color[1] + y) % 256, color[2])
    img.save(path, quality=90)
    return path


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def test_settings_defaults(settings):
    assert settings.max_images == 500
    assert settings.resolution == (2560, 1440)
    assert settings.data_dir.name == "wc"


def test_config_invalid_resolution():
    with pytest.raises(ConfigError):
        Settings.from_env({"WALLPAPER_RESOLUTION": "abc"})


def test_config_invalid_ratio_order():
    env = {"WALLPAPER_MIN_RATIO": "2.0", "WALLPAPER_MAX_RATIO": "1.5"}
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_config_prep_auto():
    s = Settings.from_env({"WALLPAPER_PREP_RESOLUTION": "auto"})
    assert s.prep_resolution == "auto"


def test_config_non_bool_rejected():
    with pytest.raises(ConfigError):
        Settings.from_env({"WALLPAPER_NOTIFY_ENABLED": "talvez"})


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

def test_state_ids_roundtrip(state):
    state.add_id("abc123")
    assert state.has_id("abc123")
    assert state.load_ids() == {"abc123"}


def test_state_history_roundtrip(state):
    state.append_history("foo.jpg")
    hist = state.load_history()
    assert len(hist) == 1
    assert hist[0][1] == "foo.jpg"
    assert state.last_rotation() is not None


def test_state_sources_last_wins(state):
    state.add_source("a.jpg", "wallhaven")
    state.add_source("a.jpg", "bing")
    assert state.source_of("a.jpg") == "bing"


def test_state_counters(state):
    state.index = 5
    state.theme_idx = 2
    state.source_idx = 3
    assert (state.index, state.theme_idx, state.source_idx) == (5, 2, 3)
    # nova instância lê os mesmos valores (persistência)
    assert State(state.dir).index == 5


def test_state_try_lock(state):
    assert state.try_lock("test")
    # lock liberado ao sair do contexto
    with state.locked("test2"):
        pass


def test_master_name_helpers():
    assert is_master_name("foo.jpg")
    assert not is_master_name("foo-screen.jpg")
    assert master_name_from_any("foo-screen.png") == "foo.png"
    assert master_name_from_any("foo.jpg") == "foo.jpg"


# ---------------------------------------------------------------------------
# imaging
# ---------------------------------------------------------------------------

def test_probe_and_dhash(tmp_path):
    p = make_image(tmp_path / "a.jpg", 800, 450)
    pr = probe(p)
    assert (pr.w, pr.h) == (800, 450)
    assert pr.size_bytes > 0
    assert dhash(p) == dhash(p)
    d = dhash(p)
    assert 0 <= d < 2**64


def test_dhash_similar_images(tmp_path):
    import random

    def noisy(path: Path, seed: int, tint) -> Path:
        rng = random.Random(seed)
        img = Image.new("RGB", (800, 450))
        px = img.load()
        for y in range(450):
            for x in range(800):
                px[x, y] = (
                    min(255, rng.randrange(256) + tint[0]),
                    min(255, rng.randrange(256) + tint[1]),
                    min(255, rng.randrange(256) + tint[2]),
                )
        img.save(path, quality=90)
        return path

    a = noisy(tmp_path / "a.jpg", 1, (0, 0, 0))
    b = noisy(tmp_path / "b.jpg", 1, (10, 0, 0))   # quase idêntica
    c = noisy(tmp_path / "c.jpg", 2, (0, 200, 0))  # diferente
    da, db, dc = dhash(a), dhash(b), dhash(c)
    assert hamming(da, db) < hamming(da, dc)


def test_compute_features(tmp_path):
    p = make_image(tmp_path / "f.jpg", 400, 225)
    f = compute_features(p)
    assert f is not None
    assert f.name == "f.jpg"
    assert f.w == 400 and f.h == 225
    assert 0.0 <= f.sat <= 1.0
    assert 0 <= f.dhash < 2**64
    assert len(f.hist_b64) > 0


def test_screen_cache(tmp_path):
    master = make_image(tmp_path / "img" / "m.jpg", 2560, 1440)
    images = tmp_path / "img"
    target = (1366, 768)
    assert needs_screen_cache(master, target)
    cache = screen_cache_path(master, images)
    assert cache.name == "m-screen.jpg"
    assert not screen_cache_valid(master, cache, target)
    assert build_screen_cache(master, cache, target)
    assert screen_cache_valid(master, cache, target)
    pc = probe(cache)
    assert (pc.w, pc.h) == target
    # master menor que alvo não precisa de cache
    small = make_image(tmp_path / "small.jpg", 800, 450)
    assert not needs_screen_cache(small, target)


def test_resolve_screen_path(tmp_path):
    master = make_image(tmp_path / "img" / "m.jpg", 2560, 1440)
    images = tmp_path / "img"
    target = (1366, 768)
    path, regen = resolve_screen_path(master, images, target)
    assert regen is True
    assert path.name == "m-screen.jpg"
    path2, regen2 = resolve_screen_path(master, images, target)
    assert regen2 is False and path2 == path


def test_is_image_file(tmp_path):
    assert is_image_file(Path("a.jpg"))
    assert is_image_file(Path("a.PNG"))
    assert not is_image_file(Path("a.txt"))


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

def test_check_quality(tmp_path, settings):
    good = make_image(tmp_path / "good.jpg", 2560, 1440)
    assert check_quality(good, settings) is None

    small = make_image(tmp_path / "small.jpg", 800, 450)
    assert check_quality(small, settings) is not None

    portrait = make_image(tmp_path / "port.jpg", 1000, 2000)
    assert check_quality(portrait, settings) is not None

    # ratio 2.0 fora de [1.55, 1.90], mas resolução mínima OK
    wide = make_image(tmp_path / "wide.jpg", 2880, 1440)
    assert check_quality(wide, settings) is not None


def test_accept_image(tmp_path, settings, state):
    src = make_image(tmp_path / "src.jpg", 2560, 1440, (10, 200, 100))
    dest = settings.images_dir / "test-1.jpg"
    assert accept_image(src, dest, "test-1", "wallhaven", settings, state)
    assert dest.is_file()
    assert not src.exists()  # movido
    assert state.has_id("test-1")
    assert state.source_of("test-1.jpg") == "wallhaven"


def test_accept_image_rejects_bad(tmp_path, settings, state):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    dest = settings.images_dir / "bad.jpg"
    assert not accept_image(bad, dest, "bad", "x", settings, state)
    assert not dest.exists()


def test_accept_image_dedup(tmp_path, settings, state):
    src = make_image(tmp_path / "src.jpg", 2560, 1440, (200, 100, 50))
    dest = settings.images_dir / "dup-1.jpg"
    assert accept_image(src, dest, "dup-1", "wallhaven", settings, state)

    # cópia exata do mesmo arquivo deve ser rejeitada como near-duplicata
    import shutil
    src2 = tmp_path / "src2.jpg"
    shutil.copy(dest, src2)
    dest2 = settings.images_dir / "dup-2.jpg"
    assert not accept_image(src2, dest2, "dup-2", "wallhaven", settings, state)
    assert not dest2.exists()


def test_download_rejects_unsafe_name(tmp_path, settings, state, monkeypatch):
    """Candidato com name malicioso (path traversal) é pulado sem download."""
    from wallpaper_carousel import pipeline
    from wallpaper_carousel.sources import ALL_SOURCES
    from wallpaper_carousel.sources.base import Candidate

    class EvilSource:
        name = "evil"

        def enabled(self, s):
            return True

        def candidates(self, s, st):
            yield Candidate(
                source="evil", ident="e1", name="../evil.jpg",
                url="http://127.0.0.1:1/x.jpg", ext="jpg",
            )
            yield Candidate(
                source="evil", ident="e2", name="ok name!.jpg",
                url="http://127.0.0.1:1/x.jpg", ext="jpg",
            )

    monkeypatch.setitem(ALL_SOURCES, "evil", EvilSource())
    result = pipeline.download_from_source("evil", settings, state)
    assert result.accepted == 0
    assert result.skipped == 2
    assert not (tmp_path / "evil.jpg").exists()
    assert not (settings.images_dir.parent / "evil.jpg").exists()


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

def _seed_images(settings, n=4):
    colors = [(200, 50, 50), (50, 200, 50), (50, 50, 200), (200, 200, 50),
              (50, 200, 200), (200, 50, 200)]
    for i in range(n):
        make_image(
            settings.images_dir / f"wallhaven-test{i}.jpg",
            2560, 1440, colors[i % len(colors)],
        )


def test_scoring_pick(settings, state):
    from wallpaper_carousel.scoring import PickEmpty, pick

    _seed_images(settings)
    try:
        result = pick(settings, top=3, state=state)
    except PickEmpty:
        pytest.fail("PickEmpty inesperado com imagens válidas")
    assert result.choice in {f"wallhaven-test{i}.jpg" for i in range(4)}
    assert result.score == result.top[0]["total"]
    assert result.context["night"] in (True, False)


def test_scoring_pick_empty(settings, state):
    from wallpaper_carousel.scoring import PickEmpty, pick

    with pytest.raises(PickEmpty):
        pick(settings, state=state)


def test_scoring_score_file(settings, state):
    from wallpaper_carousel.scoring import score_file

    _seed_images(settings, 1)
    info = score_file(settings, "wallhaven-test0.jpg", state=state)
    assert info is not None
    assert info["name"] == "wallhaven-test0.jpg"
    assert "total" in info
    assert score_file(settings, "nao-existe.jpg", state=state) is None


def test_scoring_feedback_roundtrip(settings, state):
    from wallpaper_carousel.scoring import feedback, load_feedback

    _seed_images(settings, 1)
    fb = feedback(settings, "wallhaven-test0.jpg", "like", state=state)
    assert fb.likes == 1
    fb = feedback(settings, "wallhaven-test0.jpg", "block", state=state)
    assert fb.blocked is True
    all_fb = load_feedback(state)
    assert all_fb["wallhaven-test0.jpg"].likes == 1
    assert all_fb["wallhaven-test0.jpg"].blocked is True
    with pytest.raises(ValueError):
        feedback(settings, "wallhaven-test0.jpg", "hack", state=state)


def test_scoring_night_model(settings):
    from wallpaper_carousel.scoring import is_night, night_factor

    assert night_factor(datetime(2026, 1, 1, 12, 0), settings) == 0.0
    assert night_factor(datetime(2026, 1, 1, 23, 0), settings) == 1.0
    assert is_night(datetime(2026, 1, 1, 23, 0), settings)
    assert not is_night(datetime(2026, 1, 1, 12, 0), settings)


def test_scoring_prune_candidates(settings, state):
    from wallpaper_carousel.scoring import prune_candidates

    _seed_images(settings, 3)
    cands = prune_candidates(settings, count=5, state=state)
    assert isinstance(cands, list)
    for c in cands:
        assert "name" in c


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------

def test_sources_registry(settings):
    from wallpaper_carousel.sources import ALL_SOURCES, all_enabled, get_source

    assert set(ALL_SOURCES) == {
        "wallhaven", "wikimedia", "nasa", "wallpaperscom",
        "picsum", "bing", "openverse", "reddit",
    }
    assert get_source("wallhaven") is not None
    assert get_source("nope") is None
    enabled = {s.name for s in all_enabled(settings)}
    # reddit/nasa/openverse off por padrão (APIs mortas ou rate limit)
    assert enabled == {
        "wallhaven", "wikimedia", "wallpaperscom",
        "picsum", "bing",
    }


def test_candidate_fields():
    from wallpaper_carousel.sources.base import Candidate

    c = Candidate(
        source="wallhaven", ident="abc", name="wallhaven-abc.jpg",
        url="http://x", ext="jpg", hint=3.0,
    )
    assert c.name == "wallhaven-abc.jpg"
    assert c.ident == "abc"
