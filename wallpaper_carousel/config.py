"""Configuração via variáveis de ambiente WALLPAPER_* / SCORE_*.

Toda a configuração é validada na carga: valores inválidos abortam com
ConfigError antes de qualquer comando executar.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "wallpaper-carousel"

DEFAULT_THEMES = [
    "nature landscape",
    "space nebula stars",
    "city night skyline",
    "mountain snow peaks",
    "ocean sea waves",
    "minimal abstract",
    "forest fog trees",
    "architecture building",
    "sunset dusk golden",
    "desert dunes",
    "aurora polar",
    "cherry blossom japan",
    "northern lights aurora borealis",
    "mountain lake reflection",
    "waterfall forest moss",
    "starry night milky way",
    "-",
]

DEFAULT_SOURCE_WEIGHTS = {
    "wallhaven": 1.0,
    "wikimedia": 0.9,
    "nasa": 0.9,
    "wallpaperscom": 0.7,
    "openverse": 0.7,
    "reddit": 1.0,
    "bing": 0.55,
    "picsum": 0.75,
    "other": 0.8,
}

_RES_RE = re.compile(r"^(\d+)x(\d+)$")


class ConfigError(ValueError):
    """Configuração inválida (env var malformada ou fora do limite)."""


def _env(name: str, default: str) -> str:
    val = os.environ.get(name)
    return default if val is None or val == "" else val


def _int(name: str, default: int, lo: int, hi: int) -> int:
    raw = _env(name, str(default))
    try:
        val = int(raw)
    except ValueError:
        raise ConfigError(f"{name}='{raw}' não é um inteiro")
    if not lo <= val <= hi:
        raise ConfigError(f"{name}={val} fora do intervalo [{lo}, {hi}]")
    return val


def _float(name: str, default: float, lo: float, hi: float) -> float:
    raw = _env(name, str(default))
    try:
        val = float(raw)
    except ValueError:
        raise ConfigError(f"{name}='{raw}' não é um número")
    if not lo <= val <= hi or val != val:  # NaN check
        raise ConfigError(f"{name}={val} fora do intervalo [{lo}, {hi}]")
    return val


def _bool(name: str, default: bool) -> bool:
    raw = _env(name, "1" if default else "0")
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name}='{raw}' não é booleano (use 0/1)")


def _resolution(name: str, default: str, allow_auto: bool = False) -> tuple[int, int] | str:
    raw = _env(name, default)
    if allow_auto and raw == "auto":
        return "auto"
    m = _RES_RE.match(raw)
    if not m:
        raise ConfigError(f"{name}='{raw}' inválido (esperado WxH)")
    w, h = int(m.group(1)), int(m.group(2))
    if not (320 <= w <= 16384 and 320 <= h <= 16384):
        raise ConfigError(f"{name}={raw} fora dos limites [320..16384]")
    return w, h


def _ratios(raw: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        m = _RES_RE.match(item)
        if not m:
            raise ConfigError(f"WALLPAPER_RATIOS item '{item}' inválido (esperado WxH)")
        out.append((int(m.group(1)), int(m.group(2))))
    if not out:
        raise ConfigError("WALLPAPER_RATIOS vazio")
    return out


def _source_weights(raw: str) -> dict[str, float]:
    weights = dict(DEFAULT_SOURCE_WEIGHTS)
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ConfigError(f"SCORE_SOURCE_WEIGHTS item '{part}' inválido (esperado chave=float)")
        key, _, val = part.partition("=")
        try:
            weights[key.strip()] = float(val)
        except ValueError:
            raise ConfigError(f"SCORE_SOURCE_WEIGHTS valor '{val}' inválido")
        if weights[key.strip()] != weights[key.strip()]:  # NaN
            raise ConfigError(f"SCORE_SOURCE_WEIGHTS valor '{val}' é NaN")
    return weights


@dataclass(frozen=True)
class Settings:
    """Configuração imutável validada."""

    data_dir: Path
    # Agendamento
    rotate_minutes: int = 60
    download_hours: int = 6
    # Acervo
    max_images: int = 500
    max_storage_mb: int = 3072
    # Qualidade / aceite
    resolution: tuple[int, int] = (2560, 1440)  # resolução mínima (atleast)
    ratios: tuple[tuple[int, int], ...] = ((16, 9),)
    categories: int = 100
    purity: int = 100
    min_bpp: float = 0.35
    max_file_size_kb: int = 16384
    min_ratio: float = 1.55
    max_ratio: float = 1.90
    dup_hamming: int = 4
    # Download wallhaven
    pages: int = 5
    download_limit: int = 15
    api_sleep: float = 2.0
    themes: tuple[str, ...] = tuple(DEFAULT_THEMES)
    # Fontes
    reddit_enabled: bool = False  # API anônima morta; RSS morre em breve — reativar se voltar
    reddit_limit: int = 8
    bing_enabled: bool = True
    bing_days: int = 8
    bing_limit: int = 5
    picsum_enabled: bool = True
    picsum_limit: int = 10
    # Novas fontes premium (v7.1)
    wikimedia_enabled: bool = True
    wikimedia_limit: int = 6
    nasa_enabled: bool = False  # API APOD migrada/quebrada em 2026; reativar se voltar
    nasa_limit: int = 3
    wallpaperscom_enabled: bool = True
    wallpaperscom_limit: int = 5
    openverse_enabled: bool = False  # anônimo é 5 req/h; ativar com token
    openverse_limit: int = 5
    openverse_token: str = ""        # client_id:client_secret (opcional)
    # Cache de tela
    prep_resolution: tuple[int, int] | str = "auto"
    # Log
    log_max_kb: int = 1024
    # Notificações
    notify_enabled: bool = False
    notify_actions: bool = False
    # Score (parâmetros do modelo de seleção)
    score_seed: int = 20261022
    score_night_start: int = 19
    score_day_start: int = 6
    score_twilight_hours: float = 1.5
    score_day_weight: float = 3.0
    score_day_luma: float = 120.0
    score_day_sigma: float = 45.0
    score_fresh_weight: float = 1.0
    score_fresh_hours: float = 24.0
    score_recent_repeat_penalty: float = 0.6
    score_accent_weight: float = 0.2
    score_accent_hue_window: float = 20.0
    score_source_weights: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_SOURCE_WEIGHTS)
    )
    score_src_over_penalty: float = 8.0
    score_day_bing_multiplier: float = 12.0
    score_day_bing_bonus: float = 2.0
    # systemd
    systemd_dir: Path | None = None

    # --- diretórios derivados ---
    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "carousel.log"

    # --- validações cruzadas ---
    def validate(self) -> None:
        if not self.min_ratio < self.max_ratio:
            raise ConfigError(
                f"WALLPAPER_MIN_RATIO ({self.min_ratio}) deve ser < WALLPAPER_MAX_RATIO ({self.max_ratio})"
            )
        if self.score_fresh_hours <= 0:
            raise ConfigError("SCORE_FRESH_HOURS deve ser > 0")
        if self.score_day_sigma <= 0:
            raise ConfigError("SCORE_DAY_LUMA_SIGMA deve ser > 0")
        if self.score_twilight_hours < 0 or self.score_twilight_hours > 4:
            raise ConfigError("SCORE_TWILIGHT_HOURS deve estar em [0, 4]")
        if self.score_src_over_penalty < 0:
            raise ConfigError("SCORE_SRC_OVER_PENALTY deve ser >= 0")
        if isinstance(self.prep_resolution, tuple) and (
            not (320 <= self.prep_resolution[0] <= 16384)
            or not (320 <= self.prep_resolution[1] <= 16384)
        ):
            raise ConfigError("WALLPAPER_PREP_RESOLUTION fora dos limites")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        old = dict(os.environ)
        try:
            if env is not None:
                os.environ.update(env)
            data_dir_raw = _env("WALLPAPER_DATA_DIR", str(DEFAULT_DATA_DIR))
            data_dir = Path(data_dir_raw).expanduser()
            s = cls(
                data_dir=data_dir,
                rotate_minutes=_int("WALLPAPER_ROTATE_MINUTES", 60, 1, 1440),
                download_hours=_int("WALLPAPER_DOWNLOAD_HOURS", 6, 1, 24),
                max_images=_int("WALLPAPER_MAX_IMAGES", 500, 1, 100000),
                max_storage_mb=_int("WALLPAPER_MAX_STORAGE_MB", 3072, 1, 1048576),
                resolution=_resolution("WALLPAPER_RESOLUTION", "2560x1440"),
                ratios=tuple(_ratios(_env("WALLPAPER_RATIOS", "16x9"))),
                categories=_int("WALLPAPER_CATEGORIES", 100, 0, 100),
                purity=_int("WALLPAPER_PURITY", 100, 0, 100),
                min_bpp=_float("WALLPAPER_MIN_BPP", 0.35, 0.0, 64.0),
                max_file_size_kb=_int("WALLPAPER_MAX_FILE_SIZE_KB", 16384, 0, 1048576),
                min_ratio=_float("WALLPAPER_MIN_RATIO", 1.55, 1.0, 4.0),
                max_ratio=_float("WALLPAPER_MAX_RATIO", 1.90, 1.0, 4.0),
                dup_hamming=_int("WALLPAPER_DUP_HAMMING", 4, 0, 64),
                pages=_int("WALLPAPER_PAGES", 5, 1, 100),
                download_limit=_int("WALLPAPER_DOWNLOAD_LIMIT", 15, 0, 500),
                api_sleep=_float("WALLPAPER_API_SLEEP", 2.0, 0.0, 600.0),
                themes=tuple(
                    t for t in _env("WALLPAPER_THEMES", "|").split("|") if t
                ) or tuple(DEFAULT_THEMES),
                reddit_enabled=_bool("WALLPAPER_REDDIT_ENABLED", False),
                reddit_limit=_int("WALLPAPER_REDDIT_LIMIT", 8, 0, 500),
                bing_enabled=_bool("WALLPAPER_BING_ENABLED", True),
                bing_days=_int("WALLPAPER_BING_DAYS", 8, 1, 31),
                bing_limit=_int("WALLPAPER_BING_LIMIT", 5, 0, 500),
                picsum_enabled=_bool("WALLPAPER_PICSUM_ENABLED", True),
                picsum_limit=_int("WALLPAPER_PICSUM_LIMIT", 10, 0, 500),
                wikimedia_enabled=_bool("WALLPAPER_WIKIMEDIA_ENABLED", True),
                wikimedia_limit=_int("WALLPAPER_WIKIMEDIA_LIMIT", 6, 0, 500),
                nasa_enabled=_bool("WALLPAPER_NASA_ENABLED", False),
                nasa_limit=_int("WALLPAPER_NASA_LIMIT", 3, 0, 500),
                wallpaperscom_enabled=_bool(
                    "WALLPAPER_WALLPAPERSCOM_ENABLED", True
                ),
                wallpaperscom_limit=_int(
                    "WALLPAPER_WALLPAPERSCOM_LIMIT", 5, 0, 500
                ),
                openverse_enabled=_bool("WALLPAPER_OPENVERSE_ENABLED", False),
                openverse_limit=_int("WALLPAPER_OPENVERSE_LIMIT", 5, 0, 500),
                openverse_token=_env("WALLPAPER_OPENVERSE_TOKEN", ""),
                prep_resolution=_resolution(
                    "WALLPAPER_PREP_RESOLUTION", "auto", allow_auto=True
                ),
                log_max_kb=_int("WALLPAPER_LOG_MAX_KB", 1024, 1, 1048576),
                notify_enabled=_bool("WALLPAPER_NOTIFY_ENABLED", False),
                notify_actions=_bool("WALLPAPER_NOTIFY_ACTIONS", False),
                score_seed=_int("SCORE_SEED", 20261022, 0, 2**63 - 1),
                score_night_start=_int("SCORE_NIGHT_START", 19, 0, 23),
                score_day_start=_int("SCORE_DAY_START", 6, 0, 23),
                score_twilight_hours=_float("SCORE_TWILIGHT_HOURS", 1.5, 0.0, 4.0),
                score_day_weight=_float("SCORE_DAY_WEIGHT", 3.0, -100.0, 100.0),
                score_day_luma=_float("SCORE_DAY_LUMA", 120.0, 0.0, 255.0),
                score_day_sigma=_float("SCORE_DAY_SIGMA", 45.0, 0.001, 255.0),
                score_fresh_weight=_float("SCORE_FRESH_WEIGHT", 1.0, -100.0, 100.0),
                score_fresh_hours=_float("SCORE_FRESH_HOURS", 24.0, 0.001, 1e6),
                score_recent_repeat_penalty=_float(
                    "SCORE_RECENT_REPEAT_PENALTY", 0.6, -100.0, 100.0
                ),
                score_accent_weight=_float("SCORE_ACCENT_WEIGHT", 0.2, -100.0, 100.0),
                score_accent_hue_window=_float(
                    "SCORE_ACCENT_HUE_WINDOW", 20.0, 0.0, 180.0
                ),
                score_source_weights=_source_weights(
                    _env("SCORE_SOURCE_WEIGHTS", "")
                ),
                score_src_over_penalty=_float(
                    "SCORE_SRC_OVER_PENALTY", 8.0, 0.0, 1e6
                ),
                score_day_bing_multiplier=_float(
                    "SCORE_DAY_BING_MULTIPLIER", 12.0, 0.0, 1e6
                ),
                score_day_bing_bonus=_float("SCORE_DAY_BING_BONUS", 2.0, -1e6, 1e6),
                systemd_dir=(
                    Path(_env("WALLPAPER_SYSTEMD_DIR", "")).expanduser()
                    if _env("WALLPAPER_SYSTEMD_DIR", "")
                    else None
                ),
            )
            s.validate()
            return s
        finally:
            if env is not None:
                os.environ.clear()
                os.environ.update(old)

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.images_dir, self.state_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)
