"""Accent do KDE para o bônus de cor do score.

Delega para ``display.kde_accent_hue``, que implementa a mesma ordem de
prioridade do ``score.py`` v6: ``[General] AccentColor`` do kdeglobals →
``state/accent`` (override manual) → ``[Colors:Selection]
BackgroundNormal`` → ``[Colors:Button] DecorationFocus``. O override em
``state/accent`` é lido pelo próprio display; falhas de leitura devolvem
``None`` (bônus de accent simplesmente não se aplica).
"""

from __future__ import annotations

from ..config import Settings
from ..display import kde_accent_hue
from ..state import State


def accent_hue(state: State, settings: Settings) -> float | None:
    """Hue (0-360) do accent do KDE, ou ``None`` se indisponível."""
    # `state` é aceito por simetria com os demais módulos; o display resolve
    # o override em settings.state_dir/accent.
    _ = state
    return kde_accent_hue(settings)
