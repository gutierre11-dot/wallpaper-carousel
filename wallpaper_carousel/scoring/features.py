"""Cache de features (state/features.tsv).

Portado de ``score.py`` (v6) para usar ``wallpaper_carousel.imaging``:
as linhas do TSV são idênticas (``Feature.to_tsv()``), imagens ilegíveis
são puladas e o cache só guarda masters vivos, regravado de forma atômica
somente quando o conteúdo muda.
"""

from __future__ import annotations

import base64
from pathlib import Path

from ..imaging import Feature, compute_features
from ..state import State, atomic_write_text, is_master_name

_HIST_B64_BYTES = 128  # struct "<64H"


def list_masters(images_dir: Path) -> list[str]:
    """Nomes (ordenados) dos masters válidos em ``images_dir``.

    Master = arquivo de imagem (``IMAGE_EXTS``) cujo nome não é cache
    ``-screen`` (``state.is_master_name``).
    """
    if not images_dir.is_dir():
        return []
    return sorted(
        p.name
        for p in images_dir.iterdir()
        if p.is_file() and is_master_name(p.name)
    )


def parse_feature(line: str) -> Feature | None:
    """Converte uma linha do features.tsv em ``imaging.Feature``.

    Retorna ``None`` para linhas malformadas (nº de campos errado, números
    inválidos ou hist_b64 que não decodifica para 64 bins uint16).
    """
    parts = line.rstrip("\n").split("\t")
    if len(parts) != 10:
        return None
    try:
        raw = base64.b64decode(parts[9], validate=True)
    except Exception:
        return None
    if len(raw) != _HIST_B64_BYTES:
        return None
    try:
        return Feature(
            name=parts[0],
            w=int(parts[1]),
            h=int(parts[2]),
            luma=float(parts[3]),
            luma_std=float(parts[4]),
            warmth=float(parts[5]),
            hue=float(parts[6]),
            sat=float(parts[7]),
            dhash=int(parts[8]),
            hist_b64=parts[9],
        )
    except (ValueError, TypeError):
        return None


def load_features(
    state: State, images_dir: Path, rebuild: bool = False
) -> dict[str, Feature]:
    """Carrega o cache e calcula sob demanda o que faltar.

    - Linhas inválidas do TSV são ignoradas; em duplicatas, a última vence.
    - Com ``rebuild=True`` recalcula todos os masters.
    - O retorno contém apenas masters vivos com feature válida.
    - Regrava ``features.tsv`` (ordenado por nome, ``Feature.to_tsv()``)
      somente se o conteúdo mudou ou entradas obsoletas foram removidas.
    """
    images_dir.mkdir(parents=True, exist_ok=True)
    state.dir.mkdir(parents=True, exist_ok=True)

    cached: dict[str, Feature] = {}
    if not rebuild and state.features_file.is_file():
        for line in state.features_file.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            f = parse_feature(line)
            if f is not None:
                cached[f.name] = f  # última duplicata vence

    masters = list_masters(images_dir)
    missing = [m for m in masters if rebuild or m not in cached]
    changed = False
    for name in missing:
        f = compute_features(images_dir / name)
        if f is not None:
            cached[name] = f
            changed = True

    live = {m: cached[m] for m in masters if m in cached}
    if changed or set(cached) != set(live):
        # O cache guarda apenas imagens vivas: entradas de arquivos removidos
        # pela poda não voltam (o acervo é rotativo).
        save_features(state, live)

    return live


def save_features(state: State, feats: dict[str, Feature]) -> None:
    """Regrava ``features.tsv`` ordenado por nome (escrita atômica)."""
    state.features_file.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(feats[n].to_tsv() for n in sorted(feats)) + (
        "\n" if feats else ""
    )
    atomic_write_text(state.features_file, body)
