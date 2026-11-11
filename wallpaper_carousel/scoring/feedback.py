"""Feedback do usuário (state/scores.tsv) — replay e registro.

Formato idêntico ao da v6: ``nome<TAB>delta<TAB>acao<TAB>timestamp``.
O estado efetivo é reconstruído por replay das ações
(``like``/``unlike``/``block``/``unblock``), como no ``score.py`` antigo.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..state import State, read_lines
from .features import list_masters

_ACTIONS = ("like", "unlike", "block", "unblock")


@dataclass
class Feedback:
    """Estado de feedback de uma imagem."""

    likes: int = 0
    blocked: bool = False


def load_feedback(state: State) -> dict[str, Feedback]:
    """Reconstrói likes/blocked por nome a partir do scores.tsv.

    Linhas malformadas são ignoradas; ações desconhecidas não alteram o
    estado (o delta numérico da linha não é usado — só a ação, como na v6).
    """
    out: dict[str, Feedback] = {}
    for line in read_lines(state.scores_file):
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name, action = parts[0], parts[2]
        if not name:
            continue
        fb = out.setdefault(name, Feedback())
        if action == "like":
            fb.likes += 1
        elif action == "unlike":
            fb.likes = max(0, fb.likes - 1)
        elif action == "block":
            fb.blocked = True
        elif action == "unblock":
            fb.blocked = False
    return out


def record_feedback(
    state: State, images_dir: Path, name: str, action: str
) -> Feedback:
    """Registra uma ação de feedback e devolve o estado resultante.

    Levanta ``ValueError`` para ação inválida e ``FileNotFoundError`` se
    ``name`` não for um master existente em ``images_dir``.
    """
    if action not in _ACTIONS:
        raise ValueError(f"ação de feedback inválida: {action}")
    name = Path(name).name
    if name not in set(list_masters(images_dir)):
        raise FileNotFoundError(
            f"imagem não encontrada em {images_dir}: {name}"
        )
    state.dir.mkdir(parents=True, exist_ok=True)
    with state.locked("score"):
        state.record_feedback(name, action)
    return load_feedback(state).get(name, Feedback())
