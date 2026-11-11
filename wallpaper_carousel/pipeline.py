"""Pipeline de download: candidatos -> download -> validação -> acervo.

Todo candidato passa por accept_image: probe (Pillow), quality gate,
dedup perceptual (dHash), registro em state/ e cache de tela -screen.
"""

from __future__ import annotations

import logging
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import Settings
from .imaging import (
    ImageError,
    build_screen_cache,
    dhash,
    is_image_file,
    needs_screen_cache,
    probe,
    screen_cache_path,
)
from .sources.base import Candidate
from .state import State, is_master_name

log = logging.getLogger("wallpaper.pipeline")

USER_AGENT = "wallpaper-carousel/7.0 (Fedora KDE; rewrite Python)"

# Nomes de arquivo vindos de APIs externas: só ASCII seguro, sem path.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def http_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=(408, 425, 429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers["User-Agent"] = USER_AGENT
    return s


# ---------------------------------------------------------------------------
# Quality gate
# ---------------------------------------------------------------------------

def check_quality(path: Path, settings: Settings) -> str | None:
    """Retorna None se OK, ou o motivo da rejeição."""
    try:
        if not is_image_file(path):
            return "não é imagem"
        p = probe(path)
    except ImageError as exc:
        return str(exc)
    min_w, min_h = settings.resolution
    if p.w < min_w or p.h < min_h:
        return f"resolução {p.w}x{p.h} < {min_w}x{min_h}"
    if p.w <= p.h:
        return "não é landscape"
    ratio = p.w / p.h
    if not (settings.min_ratio <= ratio <= settings.max_ratio):
        return f"ratio {ratio:.2f} fora de [{settings.min_ratio}, {settings.max_ratio}]"
    if settings.max_file_size_kb > 0 and p.size_bytes > settings.max_file_size_kb * 1024:
        return f"tamanho {p.size_bytes} > {settings.max_file_size_kb * 1024} bytes"
    if p.w * p.h > 0:
        bpp = p.size_bytes * 8 / (p.w * p.h)
        if bpp < settings.min_bpp:
            return f"bpp {bpp:.2f} < {settings.min_bpp} (upscale?)"
    return None


# ---------------------------------------------------------------------------
# Aceite
# ---------------------------------------------------------------------------

def accept_image(
    tmp: Path,
    dest: Path,
    ident: str,
    source: str,
    settings: Settings,
    state: State,
) -> bool:
    """Valida e move `tmp` para `dest`, registrando no estado. True se aceito."""
    try:
        if not is_image_file(tmp):
            # sniff real: extensão pode mentir; probe decide
            pass
        reason = check_quality(tmp, settings)
        if reason is not None:
            log.warning("Rejeitado %s: %s", ident, reason)
            return False
        d = dhash(tmp)
    except Exception as exc:
        log.warning("Rejeitado %s: %s", ident, exc)
        return False

    mind = state.hash_distances(d)
    if mind <= settings.dup_hamming:
        log.warning("Near-duplicata (dHash=%d): descartado %s", mind, ident)
        return False

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp.replace(dest)
    except OSError as exc:
        log.warning("Falha ao mover %s: %s", ident, exc)
        return False

    state.add_hash(dest.name, d)
    if ident:
        state.add_id(ident)
    state.add_source(dest.name, source)

    # cache -screen (silencioso; falha não invalida o aceite)
    try:
        target = _screen_target_cached(settings)
        if needs_screen_cache(dest, target):
            cache = screen_cache_path(dest, settings.images_dir)
            build_screen_cache(dest, cache, target)
    except Exception:
        pass

    log.info("Aceito %s (%s) → %s", ident, source, dest.name)
    return True


_screen_target_cache: tuple[int, int] | None = None


def _screen_target_cached(settings: Settings) -> tuple[int, int]:
    global _screen_target_cache
    if _screen_target_cache is None:
        from .display import screen_target

        _screen_target_cache = screen_target(settings)
    return _screen_target_cache


# ---------------------------------------------------------------------------
# Download orquestrado
# ---------------------------------------------------------------------------

@dataclass
class DownloadResult:
    source: str
    accepted: int = 0
    rejected: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _download_one(session: requests.Session, url: str, tmp: Path) -> bool:
    try:
        with session.get(url, stream=True, timeout=(15, 60)) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
        return True
    except Exception as exc:
        log.warning("Falha download %s: %s", url, exc)
        return False


def download_from_source(
    source_name: str,
    settings: Settings,
    state: State,
    limit: int | None = None,
) -> DownloadResult:
    """Baixa candidatos de uma fonte até `limit` aceitos."""
    from .sources import get_source

    src = get_source(source_name)
    if src is None:
        raise ValueError(f"fonte desconhecida: {source_name}")
    if not src.enabled(settings):
        log.info("Fonte %s desabilitada; pulando.", source_name)
        return DownloadResult(source=source_name)

    max_new = limit if limit is not None else _default_limit(source_name, settings)
    result = DownloadResult(source=source_name)
    known_ids = state.load_ids()
    session = http_session()

    try:
        try:
            candidates = list(src.candidates(settings, state))
        except Exception as exc:
            log.error("Fonte %s falhou ao listar candidatos: %s", source_name, exc)
            result.errors.append(str(exc))
            return result

        candidates.sort(key=lambda c: c.hint, reverse=True)
        log.info(
            "%s: %d candidato(s). Baixando até %d novos...",
            source_name,
            len(candidates),
            max_new,
        )

        for cand in candidates:
            if result.accepted >= max_new:
                break
            if cand.ident in known_ids:
                result.skipped += 1
                continue
            # Segurança: name vem de APIs externas — rejeita path
            # traversal, separadores e nomes que não são master válido.
            if (
                not _SAFE_NAME_RE.match(cand.name)
                or "/" in cand.name
                or "\\" in cand.name
                or not is_master_name(cand.name)
            ):
                result.skipped += 1
                continue
            dest = settings.images_dir / cand.name
            if dest.exists():
                result.skipped += 1
                continue
            suffix = f".{cand.ext}"
            with tempfile.NamedTemporaryFile(
                suffix=suffix, dir=settings.logs_dir, delete=False
            ) as tf:
                tmp = Path(tf.name)
            try:
                if not _download_one(session, cand.url, tmp):
                    result.rejected += 1
                    continue
                if accept_image(tmp, dest, cand.ident, source_name, settings, state):
                    result.accepted += 1
                    known_ids.add(cand.ident)
                else:
                    result.rejected += 1
            finally:
                tmp.unlink(missing_ok=True)
            time.sleep(1)

        log.info(
            "%s: %d nova(s), %d rejeitada(s), %d pulada(s).",
            source_name,
            result.accepted,
            result.rejected,
            result.skipped,
        )
    finally:
        session.close()
    return result


def _default_limit(source_name: str, settings: Settings) -> int:
    return {
        "wallhaven": settings.download_limit,
        "reddit": settings.reddit_limit,
        "bing": settings.bing_limit,
        "picsum": settings.picsum_limit,
        "wikimedia": settings.wikimedia_limit,
        "nasa": settings.nasa_limit,
        "wallpaperscom": settings.wallpaperscom_limit,
        "openverse": settings.openverse_limit,
    }.get(source_name, settings.download_limit)


def download(
    sources: str,
    settings: Settings,
    state: State,
) -> list[DownloadResult]:
    """Executa download para uma fonte ou 'all' (todas as habilitadas)."""
    if sources == "all":
        from .sources import all_enabled

        names = [src.name for src in all_enabled(settings)]
    else:
        names = [sources]
    results = []
    for name in names:
        results.append(download_from_source(name, settings, state))
    return results


# ---------------------------------------------------------------------------
# Poda
# ---------------------------------------------------------------------------

def protected_names(settings: Settings, state: State) -> set[str]:
    """Imagens intocáveis pela poda: likes efetivos + últimas 168h."""
    protected: set[str] = set()
    try:
        from .scoring import load_feedback

        feedback = load_feedback(state)
        for name, fb in feedback.items():
            if fb.likes > 0 and not fb.blocked:
                protected.add(name)
    except Exception:
        pass
    cutoff = time.time() - 168 * 3600
    for ts, name in state.load_history():
        if ts.timestamp() >= cutoff:
            protected.add(name)
    return protected


def remove_master(name: str, settings: Settings, state: State) -> None:
    """Remove master + cache -screen + linhas de hashes/sources."""
    img = settings.images_dir / name
    for p in (img, screen_cache_path(img, settings.images_dir)):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    _drop_state_line(state.hashes_file, name)
    _drop_state_line(state.sources_file, name)


def _drop_state_line(path: Path, name: str) -> None:
    try:
        lines = [
            ln
            for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.split("\t")[0].strip() != name
        ]
    except OSError:
        return
    from .state import atomic_write_text

    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))


def prune(settings: Settings, state: State) -> int:
    """Poda para settings.max_images / max_storage_mb. Retorna removidas."""
    from .scoring import load_feedback, prune_candidates

    masters = sorted(
        p.name for p in settings.images_dir.iterdir() if is_master_name(p.name)
    )
    total = len(masters)
    if total == 0:
        return 0

    target = min(total, settings.max_images)
    # teto de storage: reduz alvo proporcionalmente
    try:
        import shutil

        used_mb = sum(
            f.stat().st_size for f in settings.images_dir.iterdir() if f.is_file()
        ) / (1024 * 1024)
        if used_mb > settings.max_storage_mb and total > 0:
            avg = used_mb / total
            target = min(target, max(10, int(settings.max_images * settings.max_storage_mb / used_mb)))
    except OSError:
        pass
    target = max(min(10, settings.max_images, total), target) if target < total else target
    over = total - target
    if over <= 0:
        return 0

    protected = protected_names(settings, state)
    feedback = load_feedback(state)

    try:
        rows = prune_candidates(settings, count=over * 4, state=state)
        candidates = [r["name"] for r in rows]
    except Exception:
        candidates = []

    removed = 0
    for name in candidates:
        if removed >= over:
            break
        if name in protected or name not in masters:
            continue
        # likes efetivos também protegem (mesmo regra do score.py)
        fb = feedback.get(name)
        if fb is not None and fb.likes > 0 and not fb.blocked:
            continue
        remove_master(name, settings, state)
        removed += 1

    # fallback por mtime se ainda acima do alvo
    if total - removed > target:
        remaining = sorted(
            (p for p in settings.images_dir.iterdir() if is_master_name(p.name)),
            key=lambda p: p.stat().st_mtime,
        )
        for p in remaining:
            if total - removed <= target:
                break
            if p.name in protected:
                continue
            fb = feedback.get(p.name)
            if fb is not None and fb.likes > 0 and not fb.blocked:
                continue
            remove_master(p.name, settings, state)
            removed += 1

    if removed:
        log.info("Poda: %d imagem(ns) removida(s); restam %d.", removed, total - removed)
    return removed
