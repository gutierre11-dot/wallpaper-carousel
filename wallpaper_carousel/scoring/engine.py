"""Orquestração do pick/score/feedback — API pública do motor.

Equivale aos comandos ``pick``, ``score``, ``like|block|unlike|unblock``,
``prune-candidates`` e ``housekeeping`` do ``score.py`` v6, agora como
funções testáveis que recebem ``Settings`` (e usam ``state.State``).

Quando não há candidatos, ``pick`` levanta ``PickEmpty`` — equivalente ao
exit code 4 do CLI antigo (``reason`` = ``"no_images"`` ou
``"all_blocked"``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Mapping

from ..config import Settings
from ..imaging import Feature
from ..state import State
from .accent import accent_hue
from .features import list_masters, load_features
from .feedback import Feedback, load_feedback, record_feedback
from .housekeeping import housekeeping as _housekeeping, prune_candidates as _prune
from .model import (
    HIST_RECENT,
    context_params,
    is_night,
    reason_summary,
    score_candidates,
)


class PickEmpty(Exception):
    """Nenhum candidato disponível para pick (exit code 4 do v6).

    ``reason`` é ``"no_images"`` (acervo vazio/sem features) ou
    ``"all_blocked"`` (todas as imagens bloqueadas).
    """

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


@dataclass
class ScoreContext:
    """Estado carregado uma vez por pick/score (feedback, histórico, etc.)."""

    feedback: dict[str, Feedback]
    recent: list[str]
    last_seen: dict[str, datetime]
    accent_hue: float | None
    sources: dict[str, str]
    history_len: int


@dataclass
class PickResult:
    """Resultado de ``pick``/``pick_explain``.

    - ``choice``: nome do master escolhido.
    - ``score``: score total do vencedor.
    - ``components``: breakdown por componente do vencedor.
    - ``context``: ``night``, ``night_factor``, ``accent_hue``,
      ``target``, ``sigma``, ``weight`` do contexto vigente.
    - ``top``: até ``top`` candidatos ordenados (dicts do score_candidates).
    - ``counts``: likes/blocks/candidates/history.
    - ``when``: timestamp ISO do pick.
    - ``reason``: resumo legível (preenchido por ``pick_explain``).
    """

    choice: str
    score: float
    components: dict[str, float]
    context: dict
    top: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    when: str = ""
    reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "choice": self.choice,
            "score": self.score,
            "components": self.components,
            "context": self.context,
            "top": self.top,
            "counts": self.counts,
            "when": self.when,
            "reason": self.reason,
        }


def make_state(settings: Settings) -> State:
    """``State`` apontando para ``settings.state_dir``."""
    return State(settings.state_dir)


def load_context(
    state: State, settings: Settings, feats: Mapping[str, Feature]
) -> ScoreContext:
    """Carrega feedback, últimas exibições, last_seen, accent e fontes.

    ``feats`` é aceito por simetria com o v6 (o contexto não filtra por
    features; candidatos bloqueados/sem feature são tratados no score).
    """
    _ = feats
    history = state.load_history()
    recent = [n for _ts, n in history[-HIST_RECENT:]]
    last_seen: dict[str, datetime] = {}
    for ts, name in history:
        if name not in last_seen or ts > last_seen[name]:
            last_seen[name] = ts
    return ScoreContext(
        feedback=load_feedback(state),
        recent=recent,
        last_seen=last_seen,
        accent_hue=accent_hue(state, settings),
        sources=state.load_sources(),
        history_len=len(history),
    )


def _build_result(
    cands: list[dict],
    ctx: ScoreContext,
    settings: Settings,
    when: datetime,
    top: int,
) -> PickResult:
    winner = cands[0]
    params = context_params(when, settings)
    context = {
        "night": is_night(when, settings),
        "night_factor": winner["night_factor"],
        "accent_hue": (
            round(ctx.accent_hue, 1) if ctx.accent_hue is not None else None
        ),
        "target": winner["context_target"],
        "sigma": round(params.sigma, 3),
        "weight": round(params.weight, 3),
    }
    counts = {
        "likes": sum(1 for fb in ctx.feedback.values() if fb.likes > 0),
        "blocks": sum(1 for fb in ctx.feedback.values() if fb.blocked),
        "candidates": len(cands),
        "history": ctx.history_len,
    }
    return PickResult(
        choice=winner["name"],
        score=winner["total"],
        components=winner["components"],
        context=context,
        top=cands[: max(0, top)],
        counts=counts,
        when=when.strftime("%Y-%m-%d %H:%M:%S"),
    )


def pick(
    settings: Settings,
    top: int = 8,
    when: datetime | None = None,
    state: State | None = None,
) -> PickResult:
    """Escolhe o próximo wallpaper (equivale a ``score.py pick``).

    Levanta ``PickEmpty`` quando não há imagens ou todas estão bloqueadas.
    """
    state = state or make_state(settings)
    feats = load_features(state, settings.images_dir)
    if not feats:
        raise PickEmpty("no_images", f"nenhuma imagem em {settings.images_dir}")
    when = when or datetime.now()
    ctx = load_context(state, settings, feats)
    cands = score_candidates(
        feats,
        ctx.feedback,
        ctx.recent,
        when,
        ctx.accent_hue,
        settings,
        last_seen=ctx.last_seen,
        sources=ctx.sources,
    )
    if not cands:
        raise PickEmpty("all_blocked", "todas as imagens estão bloqueadas")
    return _build_result(cands, ctx, settings, when, top)


def pick_explain(
    settings: Settings,
    top: int = 8,
    when: datetime | None = None,
    state: State | None = None,
) -> PickResult:
    """Como ``pick``, mas preenche ``reason`` com o resumo do vencedor."""
    result = pick(settings, top=top, when=when, state=state)
    winner = result.top[0] if result.top else None
    result.reason = reason_summary(winner) if winner else "empate técnico"
    return result


def score_file(
    settings: Settings,
    name: str,
    when: datetime | None = None,
    state: State | None = None,
) -> dict | None:
    """Breakdown do score de um arquivo (equivale a ``score.py score``).

    Retorna ``None`` se ``name`` não é um master com features; se a imagem
    está bloqueada retorna ``{"name", "blocked": True, "total": None}``;
    caso contrário o dict do candidato (mesmo formato de
    ``score_candidates``).
    """
    state = state or make_state(settings)
    name = Path(name).name
    feats = load_features(state, settings.images_dir)
    if name not in feats:
        return None
    when = when or datetime.now()
    ctx = load_context(state, settings, feats)
    cands = score_candidates(
        feats,
        ctx.feedback,
        ctx.recent,
        when,
        ctx.accent_hue,
        settings,
        last_seen=ctx.last_seen,
        sources=ctx.sources,
    )
    found = next((c for c in cands if c["name"] == name), None)
    if found is None:
        fb = ctx.feedback.get(name, Feedback())
        return {"name": name, "blocked": fb.blocked, "total": None}
    return found


def feedback(
    settings: Settings, name: str, action: str, state: State | None = None
) -> Feedback:
    """Registra like/unlike/block/unblock e devolve o estado resultante.

    Levanta ``ValueError`` para ação inválida e ``FileNotFoundError``
    quando ``name`` não é um master existente.
    """
    state = state or make_state(settings)
    return record_feedback(state, settings.images_dir, name, action)


def prune_candidates(
    settings: Settings,
    count: int = 20,
    now: datetime | None = None,
    state: State | None = None,
) -> list[dict]:
    """Lista as ``count`` imagens mais descartáveis (equivale a
    ``score.py prune-candidates``)."""
    state = state or make_state(settings)
    feats = load_features(state, settings.images_dir)
    if not feats:
        return []
    fb = load_feedback(state)
    last_seen: dict[str, datetime] = {}
    for ts, name in state.load_history():
        if name in feats and (name not in last_seen or ts > last_seen[name]):
            last_seen[name] = ts
    return _prune(
        feats,
        fb,
        settings.images_dir,
        last_seen=last_seen,
        count=max(0, count),
        sources=state.load_sources(),
        now=now,
    )


def housekeeping(
    settings: Settings,
    keep_days: float = 30.0,
    max_history: int = 5000,
    now: datetime | None = None,
    state: State | None = None,
) -> dict:
    """Higiene de features/sources/hashes/history (equivale a
    ``score.py housekeeping``). Devolve o dict resumo do v6."""
    state = state or make_state(settings)
    return _housekeeping(
        state,
        settings,
        keep_days=max(1.0, keep_days),
        max_history=max(0, max_history),
        now=now,
    )


def list_features(
    settings: Settings,
    rebuild: bool = False,
    state: State | None = None,
) -> dict[str, Feature]:
    """Features vivas (equivale a ``score.py features`` sem a impressão)."""
    state = state or make_state(settings)
    return load_features(state, settings.images_dir, rebuild=rebuild)


__all__ = [
    "Feedback",
    "PickEmpty",
    "PickResult",
    "ScoreContext",
    "feedback",
    "housekeeping",
    "list_features",
    "list_masters",
    "load_context",
    "make_state",
    "pick",
    "pick_explain",
    "prune_candidates",
    "score_file",
]
