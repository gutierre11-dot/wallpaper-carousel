"""Motor de seleção do carrossel — port do ``score.py`` (v6) para módulos.

API pública (todas recebem ``Settings``; ``state``/``when`` são opcionais
para testes):

- ``pick(settings, top=8)`` → ``PickResult`` (escolha + breakdown + top N);
  levanta ``PickEmpty`` quando não há candidatos (equivale ao exit 4).
- ``pick_explain(...)`` → ``PickResult`` com ``reason`` preenchido.
- ``score_file(settings, name)`` → dict do candidato, ``{"blocked": True}``
  ou ``None`` se a imagem não existe.
- ``feedback(settings, name, action)`` → registra like/unlike/block/unblock.
- ``prune_candidates(settings, count)`` → ranking de remoção.
- ``housekeeping(settings, keep_days, max_history)`` → dict resumo.
- ``list_features(settings, rebuild=False)`` → cache de features vivas.
- ``simulate``/``evaluate`` (em ``scoring.simulate``) → métricas old vs new.

Os módulos internos são ``features`` (cache TSV), ``model`` (constantes,
contexto dia/noite e componentes de score), ``feedback`` (replay de
scores.tsv), ``accent`` (hue do KDE) e ``housekeeping`` (poda/higiene).
"""

from __future__ import annotations

from ..config import Settings
from .engine import (
    PickEmpty,
    PickResult,
    ScoreContext,
    feedback,
    housekeeping,
    list_features,
    list_masters,
    load_context,
    make_state,
    pick,
    pick_explain,
    prune_candidates,
    score_file,
)
from .features import load_features, parse_feature, save_features
from .feedback import Feedback, load_feedback, record_feedback
from .model import (
    HIST_RECENT,
    ContextParams,
    build_sim_table,
    context_params,
    hist_probs,
    is_night,
    jitter,
    night_factor,
    reason_summary,
    score_candidates,
    similarity,
    source_of,
    source_shares,
    taste_score,
    taste_target,
)
from .simulate import SimulateError, evaluate, metrics, parse_start, simulate

__all__ = [
    "ContextParams",
    "Feedback",
    "HIST_RECENT",
    "PickEmpty",
    "PickResult",
    "ScoreContext",
    "Settings",
    "SimulateError",
    "build_sim_table",
    "context_params",
    "evaluate",
    "feedback",
    "housekeeping",
    "hist_probs",
    "is_night",
    "jitter",
    "list_features",
    "list_masters",
    "load_context",
    "load_feedback",
    "load_features",
    "make_state",
    "metrics",
    "night_factor",
    "parse_feature",
    "parse_start",
    "pick",
    "pick_explain",
    "prune_candidates",
    "reason_summary",
    "record_feedback",
    "save_features",
    "score_candidates",
    "score_file",
    "similarity",
    "simulate",
    "source_of",
    "source_shares",
    "taste_score",
    "taste_target",
]
