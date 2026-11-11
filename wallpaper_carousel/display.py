"""Detecção de display e accent do KDE.

Resolução do monitor primário: kscreen-doctor -j -> xrandr -> fallback.
Accent: ~/.config/kdeglobals (com XDG_CONFIG_HOME) -> state/accent.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .config import Settings

FALLBACK_RESOLUTION = (2560, 1440)


def _run(cmd: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "QT_QPA_PLATFORM": os.environ.get("QT_QPA_PLATFORM", "")},
    )


def detect_resolution() -> tuple[tuple[int, int], str]:
    """Retorna ((w,h), fonte). Fonte: env|kscreen-doctor|xrandr|fallback."""
    # kscreen-doctor (precisa de sessão gráfica; falha silenciosamente)
    try:
        cp = _run(["kscreen-doctor", "-j"], timeout=20)
        if cp.returncode == 0 and cp.stdout.strip():
            data = json.loads(cp.stdout)
            outputs = data.get("outputs", [])
            enabled = [
                o for o in outputs if o.get("enabled") and o.get("size", {}).get("width", 0) > 0
            ]
            if enabled:
                enabled.sort(key=lambda o: o.get("priority", 99))
                size = enabled[0]["size"]
                return (int(size["width"]), int(size["height"])), "kscreen-doctor"
    except Exception:
        pass

    # xrandr
    try:
        cp = _run(["xrandr", "--current"], timeout=10)
        if cp.returncode == 0:
            for line in cp.stdout.splitlines():
                if " connected" not in line:
                    continue
                for tok in line.split():
                    if "x" in tok and "+" in tok:
                        geom = tok.split("+")[0]
                        if "x" in geom:
                            w, h = geom.split("x")[:2]
                            if w.isdigit() and h.isdigit():
                                return (int(w), int(h)), "xrandr"
    except Exception:
        pass

    return FALLBACK_RESOLUTION, "fallback"


def screen_target(settings: Settings) -> tuple[int, int]:
    """Resolução alvo do cache -screen (auto -> detecta; senão config)."""
    if settings.prep_resolution == "auto":
        res, _src = detect_resolution()
        return res
    return settings.prep_resolution  # type: ignore[return-value]


def _parse_color(text: str) -> tuple[int, int, int] | None:
    text = text.strip()
    if text.startswith("#") and len(text) == 7:
        try:
            return (
                int(text[1:3], 16),
                int(text[3:5], 16),
                int(text[5:7], 16),
            )
        except ValueError:
            return None
    parts = [p.strip() for p in text.split(",")]
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        vals = [int(p) for p in parts]
        if all(0 <= v <= 255 for v in vals):
            return vals[0], vals[1], vals[2]  # type: ignore[return-value]
    return None


def _rgb_to_hue(rgb: tuple[int, int, int]) -> float:
    r, g, b = (c / 255.0 for c in rgb)
    mx, mn = max(r, g, b), min(r, g, b)
    if mx == mn:
        return 0.0
    d = mx - mn
    if mx == r:
        h = ((g - b) / d) % 6.0
    elif mx == g:
        h = (b - r) / d + 2.0
    else:
        h = (r - g) / d + 4.0
    return (h * 60.0) % 360.0


def _kdeglobals_section_vals(settings: Settings) -> dict[tuple[str, str], str]:
    """Mapa (secao, chave) -> valor do kdeglobals."""
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    out: dict[tuple[str, str], str] = {}
    try:
        current = ""
        with open(config_home / "kdeglobals", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("[") and line.endswith("]"):
                    current = line[1:-1]
                    continue
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    out[(current, k.strip())] = v.strip()
    except OSError:
        pass
    return out


def kde_accent_hue(settings: Settings) -> float | None:
    """Hue (0-360) do accent do KDE; None se indisponível.

    Prioridade: AccentColor do kdeglobals -> state/accent ->
    BackgroundNormal -> DecorationFocus.
    """
    vals = _kdeglobals_section_vals(settings)
    try:
        override = settings.state_dir.joinpath("accent").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        override = ""
    ordered = [
        vals.get(("General", "AccentColor"), ""),
        override,
        vals.get(("Colors:Selection", "BackgroundNormal"), ""),
        vals.get(("Colors:Button", "DecorationFocus"), ""),
    ]
    for val in ordered:
        if not val:
            continue
        rgb = _parse_color(val)
        if rgb is not None:
            return _rgb_to_hue(rgb)
    return None
