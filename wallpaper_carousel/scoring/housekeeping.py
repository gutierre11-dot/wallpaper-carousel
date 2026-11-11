"""Poda orientada por utilidade e higiene de estado (portado do v6).

- ``prune_candidates``: ranking das imagens menos valiosas para remoção
  (like nunca sai; bloqueada sai primeiro; nunca exibida vale mais).
- ``compact_history``: janela recente + última exibição de cada master.
- ``housekeeping``: resume poda de features/sources/hashes/history.

Correções em relação ao v6:
- ``features.before`` é medido ANTES de ``load_features`` regravar o cache
  (no v6 a contagem já vinha pós-poda, escondendo remoções).
- ``masters`` = arquivos de imagem válidos do ``images_dir``
  (``list_masters``), não apenas os que têm feature computável.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Mapping

from ..config import Settings
from ..imaging import Feature
from ..state import State, atomic_write_text, read_lines
from .features import list_masters, load_features, save_features
from .feedback import Feedback
from .model import (
    IMPACT_CONTRAST_FLOOR,
    IMPACT_CONTRAST_FULL,
    ramp,
    source_of,
)


def file_age_days(path: Path, now: datetime) -> float:
    """Idade do arquivo em dias (mtime); 0.0 em caso de erro de stat."""
    try:
        return max(
            0.0,
            (now - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds()
            / 86400.0,
        )
    except OSError:
        return 0.0


def prune_candidates(
    feats: Mapping[str, Feature],
    feedback: Mapping[str, Feedback],
    images_dir: Path,
    last_seen: Mapping[str, datetime] | None = None,
    count: int = 10,
    sources: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Imagens menos valiosas para remover, da mais descartável à menos.

    Mesma lógica do v6: like nunca é podado; ``keep`` combina bloqueio,
    impacto visual, saturação, recência de exibição/baixa. O shell ainda
    filtra as protegidas por histórico de 168h, então o chamador pede
    candidatos a mais do que o necessário.
    """
    now = now or datetime.now()
    last_seen = last_seen or {}
    if count <= 0:
        return []
    rows: list[dict] = []
    for name, f in feats.items():
        fb = feedback.get(name, Feedback())
        if fb.likes > 0:
            continue  # like nunca é podado
        keep = 1.0
        reasons: list[str] = []
        if fb.blocked:
            keep -= 100.0
            reasons.append("bloqueada")
        keep += 0.5 * ramp(
            f.luma_std, IMPACT_CONTRAST_FLOOR, IMPACT_CONTRAST_FULL
        )
        keep += 0.15 * min(1.0, f.sat)
        last = last_seen.get(name)
        last_days: float | None = None
        if last is None:
            keep += 5.0
            reasons.append("nunca exibida")
        else:
            last_days = max(0.0, (now - last).total_seconds() / 86400.0)
            keep += max(0.0, 3.0 - last_days / 8.0)
            if last_days < 7.0:
                reasons.append("exibida recentemente")
        age = file_age_days(images_dir / name, now)
        keep += max(0.0, 1.5 - age / 10.0)
        if age < 3.0:
            reasons.append("baixada recentemente")
        rows.append(
            {
                "name": name,
                "source": source_of(name, sources),
                "keep": round(keep, 3),
                "blocked": fb.blocked,
                "last_seen_days": None
                if last_days is None
                else round(last_days, 1),
                "age_days": round(age, 1),
                "reason": ", ".join(reasons) or "baixa utilidade",
            }
        )
    rows.sort(key=lambda r: (r["keep"], r["name"]))
    return rows[:count]


def prune_tsv(path: Path, keep_names: set[str]) -> dict:
    """Remove de um TSV (1ª coluna = nome) entradas fora do acervo."""
    if not path.is_file():
        return {"before": 0, "after": 0, "removed": 0}
    lines = read_lines(path)
    kept = [ln for ln in lines if ln.split("\t", 1)[0] in keep_names]
    if len(kept) != len(lines):
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, "\n".join(kept) + ("\n" if kept else ""))
    return {
        "before": len(lines),
        "after": len(kept),
        "removed": len(lines) - len(kept),
    }


def compact_history(
    state: State,
    masters: set[str],
    keep_days: float = 30.0,
    max_entries: int = 5000,
    now: datetime | None = None,
) -> dict:
    """Compacta o histórico: janela de ``keep_days`` + última exibição viva.

    Mantém apenas entradas de masters vivos dentro da janela, mais a
    última exibição de cada master (necessária ao frescor e à proteção
    da poda), ordenadas por timestamp e limitadas a ``max_entries``.
    """
    now = now or datetime.now()
    history = state.load_history()
    if not history:
        return {"before": 0, "after": 0, "removed": 0}
    cutoff = now - timedelta(days=max(1.0, keep_days))
    latest: dict[str, datetime] = {}
    for ts, name in history:
        if name in masters and (name not in latest or ts > latest[name]):
            latest[name] = ts
    kept: list[tuple[datetime, str]] = []
    seen: set[str] = set()
    for ts, name in history:
        if name not in masters or ts < cutoff:
            continue
        kept.append((ts, name))
        seen.add(name)
    for name, ts in latest.items():
        if name not in seen:
            kept.append((ts, name))
            seen.add(name)
    kept.sort(key=lambda row: row[0])
    kept = kept[-max_entries:] if max_entries > 0 else kept
    if kept != history:
        body = "".join(
            f"{ts.strftime('%Y-%m-%d %H:%M:%S')}\t{name}\n"
            for ts, name in kept
        )
        state.history_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(state.history_file, body)
    return {
        "before": len(history),
        "after": len(kept),
        "removed": len(history) - len(kept),
    }


def housekeeping(
    state: State,
    settings: Settings,
    keep_days: float = 30.0,
    max_history: int = 5000,
    now: datetime | None = None,
) -> dict:
    """Limpa estado obsoleto do acervo rotativo e devolve o resumo.

    Retorna ``{masters, features{before,after,removed},
    sources{...}, hashes{...}, history{...}}`` — mesmo formato do v6.
    ``ids`` nunca é podado (evita re-download).
    """
    now = now or datetime.now()

    # 'before' medido ANTES de load_features regravar o cache (bugfix v6).
    before_features = 0
    if state.features_file.is_file():
        before_features = len(read_lines(state.features_file))

    feats = load_features(state, settings.images_dir)
    masters = set(list_masters(settings.images_dir))

    summary: dict = {"masters": len(masters)}
    with state.locked("housekeeping"):
        # Regrava o cache só com masters vivos (load_features já faz isso;
        # o rewrite aqui garante o formato mesmo se nada mudou).
        save_features(state, feats)
        summary["features"] = {
            "before": before_features,
            "after": len(feats),
            "removed": max(0, before_features - len(feats)),
        }
        summary["sources"] = prune_tsv(state.sources_file, masters)
        summary["hashes"] = prune_tsv(state.hashes_file, masters)
        summary["history"] = compact_history(
            state,
            masters,
            keep_days=keep_days,
            max_entries=max_history,
            now=now,
        )
    return summary
