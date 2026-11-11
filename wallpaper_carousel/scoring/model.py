"""Motor de score do carrossel — paridade com ``score.py`` (v6).

Todos os pesos e fórmulas são idênticos aos do script original; os
parâmetros antes lidos de env ``SCORE_*`` agora vêm de
``Settings.score_*``. As constantes fixas do v6 permanecem como constantes
de módulo.

Diferenças deliberadas em relação ao v6 (mesma semântica pretendida):
- ``Feature`` é ``imaging.Feature`` (histograma guardado como ``hist_b64``);
  probabilidades são obtidas via ``imaging.hist_from_b64`` — mesmo vetor
  normalizado que o v6 usava em ``Feature.hist``.
- ``masters``/fontes usam ``state.source_from_name``/``is_master_name``.
- ``similarity`` evita a tabela O(n²) no pick: compara só com as últimas
  ``HIST_RECENT`` exibições (o v6 já fazia isso; ``build_sim_table`` segue
  disponível para simulate/eval).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from ..config import Settings
from ..imaging import Feature, hist_from_b64, hamming
from ..state import source_from_name
from .feedback import Feedback

# ---------------------------------------------------------------------------
# Constantes fixas do v6
# ---------------------------------------------------------------------------
JITTER_RANGE = 0.35
HIST_RECENT = 6

# Contexto noturno (constantes fixas; os alvos de dia vêm de Settings).
NIGHT_CTX_WEIGHT = 7.0
NIGHT_LUMA_TARGET = 45.0
NIGHT_LUMA_SIGMA = 35.0
NIGHT_BRIGHT_LUMA = 110.0
NIGHT_BRIGHT_PENALTY = -3.0
DAY_DARK_LUMA = 55.0
DAY_DARK_PENALTY = -1.0

# Cota justa por fonte.
SRC_REPEAT_PENALTY = -0.8
SRC_WINDOW = 8
SRC_PENALTY_CAP = -4.0

# Impacto visual (faixa tonal).
IMPACT_CONTRAST_WEIGHT = 0.8
IMPACT_CONTRAST_FLOOR = 30.0
IMPACT_CONTRAST_FULL = 55.0
IMPACT_FLAT_STD = 25.0
IMPACT_FLAT_PENALTY = -0.5

# Diversidade de gênero.
GENRE_WEIGHT = 0.06
GENRE_FLOOR = 45.0
GENRE_CAP = -1.5

# Gosto aprendido (centróide de likes).
TASTE_MIN_LIKES = 2
TASTE_WEIGHT = 1.5
TASTE_SIGMA = 0.25

# Pesos da similaridade (hamming, hist_l1, luma).
SIM_W_HASH = 0.55
SIM_W_HIST = 0.35
SIM_W_LUMA = 0.10


# ---------------------------------------------------------------------------
# Utilidades internas
# ---------------------------------------------------------------------------
def circ_dist(a: float, b: float) -> float:
    """Distância circular em graus (0..180)."""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def ramp(value: float, floor: float, full: float) -> float:
    """Rampa 0..1 de ``floor`` a ``full`` (1 se ``full <= floor``)."""
    if full <= floor:
        return 1.0 if value >= full else 0.0
    return max(0.0, min(1.0, (value - floor) / (full - floor)))


def jitter(name: str, seed: int) -> float:
    """Desempate determinístico em [0, JITTER_RANGE) via sha256."""
    digest = hashlib.sha256(f"{seed}:{name}".encode("utf-8")).digest()
    return (int.from_bytes(digest[:4], "big") / 0xFFFFFFFF) * JITTER_RANGE


def hour_float(when: datetime) -> float:
    return when.hour + when.minute / 60.0 + when.second / 3600.0


def hours_since(when: datetime, last: datetime | None) -> float | None:
    if last is None:
        return None
    return max(0.0, (when - last).total_seconds() / 3600.0)


def source_of(name: str, sources: Mapping[str, str] | None = None) -> str:
    """Fonte da imagem: ``sources`` (sources.tsv) ou prefixo do nome."""
    if sources and name in sources:
        return sources[name]
    return source_from_name(name)


# ---------------------------------------------------------------------------
# Contexto dia/noite com crepúsculo
# ---------------------------------------------------------------------------
def night_factor(when: datetime, settings: Settings) -> float:
    """Fator contínuo 0 (dia) .. 1 (noite), com crepúsculo nas bordas.

    Idêntico ao ``_night_factor`` do v6; parâmetros vêm de
    ``score_night_start``/``score_day_start``/``score_twilight_hours``.
    """
    night_start = settings.score_night_start % 24
    day_start = settings.score_day_start % 24
    twilight = max(0.0, min(4.0, settings.score_twilight_hours))
    h = hour_float(when)
    dusk_start = night_start - twilight
    dawn_start = day_start - twilight
    if twilight <= 0.0 or dusk_start <= day_start:
        return 1.0 if (h >= night_start or h < day_start) else 0.0
    if h >= night_start or h < dawn_start:
        return 1.0
    if h >= dusk_start:
        return (h - dusk_start) / twilight
    if h >= day_start:
        return 0.0
    return (day_start - h) / twilight


def is_night(when: datetime, settings: Settings) -> bool:
    return night_factor(when, settings) >= 0.5


@dataclass(frozen=True)
class ContextParams:
    """Parâmetros de contexto interpolados no crepúsculo."""

    target: float  # luma alvo
    sigma: float  # largura da gaussiana de luma
    weight: float  # peso do componente de contexto
    night_factor: float  # 0..1


def context_params(when: datetime, settings: Settings) -> ContextParams:
    """Alvo de luma, sigma, peso e fator noturno no instante ``when``."""
    factor = night_factor(when, settings)
    target = settings.score_day_luma + (
        NIGHT_LUMA_TARGET - settings.score_day_luma
    ) * factor
    sigma = settings.score_day_sigma + (
        NIGHT_LUMA_SIGMA - settings.score_day_sigma
    ) * factor
    weight = settings.score_day_weight + (
        NIGHT_CTX_WEIGHT - settings.score_day_weight
    ) * factor
    return ContextParams(
        target=target, sigma=sigma, weight=weight, night_factor=factor
    )


# ---------------------------------------------------------------------------
# Similaridade
# ---------------------------------------------------------------------------
def hist_probs(f: Feature) -> list[float]:
    """Probabilidades do histograma RGB (soma=1) via ``hist_from_b64``."""
    return hist_from_b64(f.hist_b64)


def similarity(
    a: Feature,
    b: Feature,
    hist_a: Sequence[float] | None = None,
    hist_b: Sequence[float] | None = None,
) -> float:
    """Similaridade 0..100 (100 = idênticas), mesma fórmula do v6.

    ``dist = 0.55*(hamming/64) + 0.35*hist_l1 + 0.10*dluma`` com
    ``hist_l1 = sum|a-b|/2`` sobre probabilidades normalizadas.
    """
    ha = hist_a if hist_a is not None else hist_probs(a)
    hb = hist_b if hist_b is not None else hist_probs(b)
    hd = hamming(a.dhash, b.dhash)
    hist_l1 = sum(abs(x - y) for x, y in zip(ha, hb)) / 2.0
    dluma = abs(a.luma - b.luma) / 255.0
    dist = (
        SIM_W_HASH * (hd / 64.0)
        + SIM_W_HIST * hist_l1
        + SIM_W_LUMA * dluma
    )
    return max(0.0, min(100.0, 100.0 * (1.0 - dist)))


def build_sim_table(feats: Mapping[str, Feature]) -> dict[tuple[str, str], float]:
    """Tabela O(n²) de similaridade — usada por simulate/eval e testes."""
    hists = {n: hist_probs(f) for n, f in feats.items()}
    table: dict[tuple[str, str], float] = {}
    names = list(feats)
    for i, a in enumerate(names):
        for b in names[i:]:
            s = similarity(feats[a], feats[b], hists[a], hists[b])
            table[(a, b)] = s
            table[(b, a)] = s
    return table


def sim_lookup(
    table: Mapping[tuple[str, str], float], a: str, b: str
) -> float:
    return table.get((a, b), 0.0)


# ---------------------------------------------------------------------------
# Cota justa por fonte
# ---------------------------------------------------------------------------
def source_shares(
    feats: Mapping[str, Feature],
    sources: Mapping[str, str] | None,
    settings: Settings,
    feedback: Mapping[str, Feedback] | None = None,
    when: datetime | None = None,
) -> dict[str, float]:
    """Fatia esperada de cada fonte: peso preferido × nº de imagens.

    Imagem bloqueada sai da conta; o multiplicador de dia do Bing só se
    aplica fora do período noturno (``night_factor < 0.5``).
    """
    feedback = feedback or {}
    counts: dict[str, int] = {}
    for name in feats:
        fb = feedback.get(name)
        if fb is not None and fb.blocked:
            continue
        src = source_of(name, sources)
        counts[src] = counts.get(src, 0) + 1
    weights = settings.score_source_weights
    default_other = weights.get("other", 0.8)
    day_bing = when is not None and night_factor(when, settings) < 0.5
    weighted = {
        src: weights.get(src, default_other)
        * n
        * (
            settings.score_day_bing_multiplier
            if src == "bing" and day_bing
            else 1.0
        )
        for src, n in counts.items()
    }
    total = sum(weighted.values()) or 1.0
    return {src: w / total for src, w in weighted.items()}


# ---------------------------------------------------------------------------
# Gosto aprendido (centróide de likes)
# ---------------------------------------------------------------------------
def taste_target(
    feats: Mapping[str, Feature], feedback: Mapping[str, Feedback]
) -> dict[str, float] | None:
    """Centróide normalizado dos likes; ``None`` com menos de TASTE_MIN_LIKES."""
    liked = [feats[n] for n, fb in feedback.items() if fb.likes > 0 and n in feats]
    if len(liked) < TASTE_MIN_LIKES:
        return None
    n = len(liked)
    sx = sum(math.sin(math.radians(f.hue)) for f in liked)
    sy = sum(math.cos(math.radians(f.hue)) for f in liked)
    return {
        "luma": sum(f.luma for f in liked) / n / 255.0,
        "std": sum(f.luma_std for f in liked) / n / 80.0,
        "sat": sum(f.sat for f in liked) / n,
        "warmth": sum(f.warmth for f in liked) / n / 128.0,
        "hue": math.degrees(math.atan2(sx, sy)) % 360.0,
    }


def taste_score(f: Feature, target: Mapping[str, float]) -> float:
    """Bônus gaussiano de proximidade ao centróide de gosto."""
    d = math.sqrt(
        (
            (f.luma / 255.0 - target["luma"]) ** 2
            + (f.luma_std / 80.0 - target["std"]) ** 2
            + (f.sat - target["sat"]) ** 2
            + (f.warmth / 128.0 - target["warmth"]) ** 2
            + (circ_dist(f.hue, target["hue"]) / 180.0) ** 2
        )
        / 5.0
    )
    return TASTE_WEIGHT * math.exp(-0.5 * (d / TASTE_SIGMA) ** 2)


# ---------------------------------------------------------------------------
# Score dos candidatos
# ---------------------------------------------------------------------------
def score_candidates(
    feats: Mapping[str, Feature],
    feedback: Mapping[str, Feedback],
    recent: Sequence[str],
    when: datetime,
    accent_hue: float | None,
    settings: Settings,
    *,
    table: Mapping[tuple[str, str], float] | None = None,
    last_seen: Mapping[str, datetime] | None = None,
    sources: Mapping[str, str] | None = None,
) -> list[dict]:
    """Pontua todos os candidatos e devolve a lista ordenada (desc).

    Cada item tem ``name``, ``source``, ``luma``, ``similar_max``,
    ``similar_mean``, ``night_factor``, ``context_target``, ``components``
    (breakdown por componente) e ``total`` — mesmo formato do v6.
    Imagens bloqueadas são excluídas.
    """
    last_seen = last_seen or {}
    ctx = context_params(when, settings)
    day_bing = ctx.night_factor < 0.5
    recent_feats = [feats[n] for n in recent if n in feats]
    recent_hists = {n: hist_probs(feats[n]) for n in recent if n in feats}
    recent_sources = [source_of(n, sources) for n in recent]
    last_source = recent_sources[-1] if recent_sources else None
    window = recent_sources[-SRC_WINDOW:]
    window_n = len(window)
    expected = source_shares(feats, sources, settings, feedback, when)
    taste = taste_target(feats, feedback)

    # Histogramas decodificados uma única vez (similaridade e taste).
    hists = {n: hist_probs(f) for n, f in feats.items()}

    out: list[dict] = []
    for name, f in feats.items():
        fb = feedback.get(name, Feedback())
        if fb.blocked:
            continue

        comp: dict[str, float] = {}
        comp["like"] = min(fb.likes * 3.0, 6.0)
        comp["context"] = ctx.weight * math.exp(
            -0.5 * ((f.luma - ctx.target) / ctx.sigma) ** 2
        )
        comp["ctx_extra"] = 0.0
        if f.luma > NIGHT_BRIGHT_LUMA:
            comp["ctx_extra"] += NIGHT_BRIGHT_PENALTY * ctx.night_factor
        if f.luma < DAY_DARK_LUMA:
            comp["ctx_extra"] += DAY_DARK_PENALTY * (1.0 - ctx.night_factor)
        if accent_hue is None:
            comp["accent"] = 0.0
        else:
            comp["accent"] = (
                settings.score_accent_weight
                if circ_dist(f.hue, accent_hue)
                <= settings.score_accent_hue_window
                else 0.0
            )

        hours = hours_since(when, last_seen.get(name))
        comp["fresh"] = (
            settings.score_fresh_weight
            if hours is None
            else settings.score_fresh_weight
            * min(1.0, hours / settings.score_fresh_hours)
        )
        if (
            hours is not None
            and settings.score_recent_repeat_penalty > 0.0
            and hours < settings.score_fresh_hours
        ):
            comp["fresh"] -= settings.score_recent_repeat_penalty * (
                1.0 - hours / settings.score_fresh_hours
            )

        comp["impact"] = IMPACT_CONTRAST_WEIGHT * ramp(
            f.luma_std, IMPACT_CONTRAST_FLOOR, IMPACT_CONTRAST_FULL
        )
        if f.luma_std < IMPACT_FLAT_STD:
            comp["impact"] += IMPACT_FLAT_PENALTY

        comp["taste"] = taste_score(f, taste) if taste is not None else 0.0

        # Anti-série: compara só com as últimas exibições (sem tabela O(n²)
        # no pick); ``table`` é usada por simulate/eval.
        if table is None:
            sims = [
                similarity(f, r, hists[name], recent_hists[r.name])
                for r in recent_feats
            ]
        else:
            sims = [sim_lookup(table, name, r.name) for r in recent_feats]
        max_sim = max(sims) if sims else 0.0
        mean_sim = sum(sims) / len(sims) if sims else 0.0
        comp["anti"] = -4.0 * (max_sim / 100.0)
        # Anti-série graduada: -3 acima de 60 e mais -2 acima de 75.
        comp["anti_extra"] = (-3.0 if max_sim > 60.0 else 0.0) + (
            -2.0 if max_sim > 75.0 else 0.0
        )
        comp["genre"] = max(
            GENRE_CAP, -GENRE_WEIGHT * max(0.0, mean_sim - GENRE_FLOOR)
        )

        cand_source = source_of(name, sources)
        comp["source_preference"] = (
            settings.score_day_bing_bonus
            if day_bing and cand_source == "bing"
            else 0.0
        )
        comp["source"] = 0.0
        if last_source is not None and cand_source == last_source:
            comp["source"] += SRC_REPEAT_PENALTY
        if window_n:
            over = (window.count(cand_source) / window_n) - expected.get(
                cand_source, 0.0
            )
            if over > 0:
                comp["source"] -= settings.score_src_over_penalty * over
        comp["source"] = max(SRC_PENALTY_CAP, comp["source"])
        comp["jitter"] = jitter(name, settings.score_seed)

        total = sum(comp.values())
        out.append(
            {
                "name": name,
                "source": cand_source,
                "luma": round(f.luma, 1),
                "similar_max": round(max_sim, 1),
                "similar_mean": round(mean_sim, 1),
                "night_factor": round(ctx.night_factor, 3),
                "context_target": round(ctx.target, 1),
                "components": {k: round(v, 3) for k, v in comp.items()},
                "total": round(total, 3),
            }
        )

    return sorted(out, key=lambda c: (-c["total"], c["name"]))


# ---------------------------------------------------------------------------
# Explicação legível (rótulos do cmd --explain do v6)
# ---------------------------------------------------------------------------
REASON_LABELS = {
    "like": "like",
    "context": "contexto dia/noite",
    "ctx_extra": "ajuste de brilho",
    "accent": "accent do KDE",
    "fresh": "frescor",
    "impact": "impacto visual",
    "taste": "gosto aprendido",
    "anti": "anti-série",
    "anti_extra": "repetição",
    "genre": "gênero",
    "source_preference": "fonte do dia",
    "source": "cota de fonte",
    "jitter": "desempate",
}


def reason_summary(cand: Mapping[str, object]) -> str:
    """Resumo legível dos componentes que mais pesaram (|v| >= 0,05)."""
    components = cand.get("components", {})
    items = [
        (abs(v), k, v)
        for k, v in components.items()  # type: ignore[union-attr]
        if abs(v) >= 0.05
    ]
    items.sort(reverse=True)
    return "; ".join(
        f"{REASON_LABELS.get(k, k)} {v:+.2f}" for _a, k, v in items
    ) or "empate técnico"
