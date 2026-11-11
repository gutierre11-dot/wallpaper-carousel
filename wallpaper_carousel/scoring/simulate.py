"""Simulação e avaliação do motor (portado de ``score.py simulate/eval``).

``simulate`` rejoga N picks com a política ``"old"`` (alfabética, como o
carrossel pré-score) ou ``"new"`` (motor de score), calculando as mesmas
métricas do v6. ``evaluate`` compara as duas políticas.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Mapping, Sequence

from ..config import Settings
from ..display import kde_accent_hue
from ..imaging import Feature
from ..state import State
from .feedback import Feedback
from .model import (
    HIST_RECENT,
    build_sim_table,
    is_night,
    score_candidates,
    sim_lookup,
    source_of,
    source_shares,
)


class SimulateError(Exception):
    """Simulação impossível (sem imagens ou tudo bloqueado)."""


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def metrics(
    picks: Sequence[str],
    feats: Mapping[str, Feature],
    table: Mapping[tuple[str, str], float],
    start: datetime,
    settings: Settings,
    sources: Mapping[str, str] | None = None,
    feedback: Mapping[str, Feedback] | None = None,
) -> dict:
    """Métricas de qualidade de uma sequência de picks (idênticas ao v6)."""
    sources = sources if sources is not None else {}
    pairs = list(zip(picks, picks[1:]))
    sim_pairs = [sim_lookup(table, a, b) for a, b in pairs]
    pct_similar = (
        round(100.0 * sum(1 for s in sim_pairs if s > 60.0) / len(pairs), 1)
        if pairs
        else 0.0
    )

    streak = best = 0
    prev_src = None
    for name in picks:
        src = source_of(name, sources)
        if src == prev_src:
            streak += 1
        else:
            streak = 1
            prev_src = src
        best = max(best, streak)

    disagree = 0
    for i, name in enumerate(picks):
        when = start + timedelta(minutes=settings.rotate_minutes * i)
        luma = feats[name].luma if name in feats else 0.0
        night = is_night(when, settings)
        if (night and luma > 95.0) or ((not night) and luma < 95.0):
            disagree += 1
    pct_disagree = round(100.0 * disagree / len(picks), 1) if picks else 0.0

    # Fatia real por fonte vs fatia esperada (peso × tamanho do acervo).
    counts: dict[str, int] = {}
    for name in picks:
        src = source_of(name, sources)
        counts[src] = counts.get(src, 0) + 1
    picks_n = len(picks) or 1
    share = {src: round(100.0 * n / picks_n, 1) for src, n in counts.items()}
    expected: dict[str, float] = {}
    if feats and picks:
        for i in range(len(picks)):
            when = start + timedelta(minutes=settings.rotate_minutes * i)
            for src, source_share in source_shares(
                feats, sources, settings, feedback, when
            ).items():
                expected[src] = (
                    expected.get(src, 0.0) + source_share / len(picks)
                )
    deviation = max(
        (
            abs(counts.get(src, 0) / picks_n - exp) * 100.0
            for src, exp in expected.items()
        ),
        default=0.0,
    )

    pick_counts: dict[str, int] = {}
    for name in picks:
        pick_counts[name] = pick_counts.get(name, 0) + 1
    max_repeticoes = max(pick_counts.values()) if pick_counts else 0

    picked_feats = [feats[n] for n in picks if n in feats]
    return {
        "picks": len(picks),
        "pct_pares_similares": pct_similar,
        "sim_media_pares": (
            round(sum(sim_pairs) / len(sim_pairs), 1) if sim_pairs else 0.0
        ),
        "maior_streak_fonte": best,
        "fatia_fonte": share,
        "desvio_fonte_pp": round(deviation, 1),
        "pct_desacordo_dia_noite": pct_disagree,
        "imagens_distintas": len(set(picks)),
        "max_repeticoes": max_repeticoes,
        "luma_mediana": round(_median([f.luma for f in picked_feats]), 1),
        "contraste_mediano": round(
            _median([f.luma_std for f in picked_feats]), 1
        ),
        "saturacao_mediana": round(_median([f.sat for f in picked_feats]), 1),
        "pares_avaliados": len(pairs),
    }


def simulate(
    feats: Mapping[str, Feature],
    feedback: Mapping[str, Feedback],
    picks: int,
    policy: str,
    start: datetime,
    settings: Settings,
    *,
    accent_hue: float | None = None,
    seed_history: bool = True,
    history: Sequence[tuple[datetime, str]] | None = None,
    sources: Mapping[str, str] | None = None,
) -> dict:
    """Simula ``picks`` escolhas com a política ``"old"`` ou ``"new"``.

    Com ``seed_history=True`` parte do histórico real informado em
    ``history`` (últimas ``HIST_RECENT`` exibições + frescor), como o v6.
    Levanta ``SimulateError`` se não houver imagens ou tudo bloqueado.
    """
    if policy not in ("old", "new"):
        raise ValueError(f"política inválida: {policy}")
    table = build_sim_table(feats)
    names = sorted(feats)
    if not names:
        raise SimulateError("nenhuma imagem para simular")
    sources = sources if sources is not None else {}

    picked: list[str] = []
    recent: list[str] = []
    last_seen: dict[str, datetime] = {}

    if seed_history and history:
        for ts, name in history:
            if name not in feats:
                continue
            if name not in last_seen or ts > last_seen[name]:
                last_seen[name] = ts
        recent = [
            name for _ts, name in history[-HIST_RECENT:] if name in feats
        ]

    if policy == "old":
        for i in range(picks):
            name = names[i % len(names)]
            picked.append(name)
            when = start + timedelta(minutes=settings.rotate_minutes * i)
            last_seen[name] = when
    else:
        if accent_hue is None:
            # Mesma resolução do v6: accent do KDE (ou None).
            accent_hue = kde_accent_hue(settings)
        for i in range(picks):
            when = start + timedelta(minutes=settings.rotate_minutes * i)
            cands = score_candidates(
                feats,
                feedback,
                recent,
                when,
                accent_hue,
                settings,
                table=table,
                last_seen=last_seen,
                sources=sources,
            )
            if not cands:
                raise SimulateError("todas as imagens estão bloqueadas")
            name = cands[0]["name"]
            picked.append(name)
            last_seen[name] = when
            recent = (recent + [name])[-HIST_RECENT:]

    result = metrics(
        picked, feats, table, start, settings, sources, feedback
    )
    result["policy"] = policy
    result["start"] = start.strftime("%Y-%m-%d %H:%M:%S")
    result["rotacao_min"] = settings.rotate_minutes
    result["picks_list"] = picked
    return result


def eval_checks(old: dict, new: dict, pool: int) -> dict[str, bool]:
    """Critérios old-vs-new do ``score.py eval`` (horizonte 60-90 picks)."""
    horizon = min(pool, new["picks"]) or 1
    return {
        "anti_serie": new["pct_pares_similares"]
        <= old["pct_pares_similares"],
        "similaridade_media": new["sim_media_pares"]
        <= old["sim_media_pares"],
        "fatia_fonte": new["desvio_fonte_pp"] <= old["desvio_fonte_pp"],
        "contexto_dia_noite": new["pct_desacordo_dia_noite"]
        <= old["pct_desacordo_dia_noite"],
        "diversidade": new["imagens_distintas"] >= 0.30 * horizon,
        "repeticao": new["max_repeticoes"] <= 8,
    }


def evaluate(
    feats: Mapping[str, Feature],
    feedback: Mapping[str, Feedback],
    picks: int,
    start: datetime,
    settings: Settings,
    *,
    state: State | None = None,
    accent_hue: float | None = None,
) -> dict:
    """Compara política ``old`` vs ``new``; devolve
    ``{old, new, checks, criterio_ok}`` (sem ``picks_list``, como no v6).
    """
    history = state.load_history() if state is not None else None
    sources = state.load_sources() if state is not None else None
    old = simulate(
        feats,
        feedback,
        picks,
        "old",
        start,
        settings,
        accent_hue=accent_hue,
        history=history,
        sources=sources,
    )
    new = simulate(
        feats,
        feedback,
        picks,
        "new",
        start,
        settings,
        accent_hue=accent_hue,
        history=history,
        sources=sources,
    )
    checks = eval_checks(old, new, len(feats))
    old.pop("picks_list", None)
    new.pop("picks_list", None)
    return {
        "old": old,
        "new": new,
        "checks": checks,
        "criterio_ok": all(checks.values()),
    }


def parse_start(value: str | None) -> datetime:
    """Parse de ``--start`` ISO; default = agora no início da hora."""
    if not value:
        now = datetime.now()
        return now.replace(minute=0, second=0, microsecond=0)
    for fmt in (
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(
        f"start inválido: {value} (use ISO, ex.: 2026-10-22T08:00)"
    )
