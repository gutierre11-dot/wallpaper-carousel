"""Persistência de estado — compatível com os arquivos da v6.

Arquivos em state/ (mesmo diretório, mesmos formatos):
  ids            um id por linha (append-only; dedup de download)
  hashes.tsv     nome<TAB>dhash64            (dedup perceptual)
  sources.tsv    nome<TAB>fonte<TAB>timestamp (última linha vence)
  history        timestamp<TAB>nome           (append por rotação)
  scores.tsv     nome<TAB>delta<TAB>acao<TAB>timestamp (feedback)
  features.tsv   cache de features (gerenciado por scoring)
  index/theme_idx/source_idx  contadores inteiros

Todas as reescritas são atômicas (tmp + os.replace) e protegidas por flock
quando o módulo `fcntl` está disponível.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - fcntl é stdlib em Unix
    fcntl = None  # type: ignore[assignment]

TS_FMT = "%Y-%m-%d %H:%M:%S"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def now_str() -> str:
    return datetime.now().strftime(TS_FMT)


def atomic_write_text(path: Path, content: str, mode: int = 0o644) -> None:
    """Escrita atômica preservando permissões do arquivo existente."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        if path.exists():
            try:
                mode = path.stat().st_mode & 0o777
            except OSError:
                pass
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_lines(path: Path) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return [ln.rstrip("\n") for ln in f]
    except OSError:
        return []


def is_master_name(name: str) -> bool:
    """True se `name` é um master (imagem válida, não-cache -screen)."""
    p = Path(name)
    if p.suffix.lower() not in IMAGE_EXTS:
        return False
    stem = p.stem.lower()
    return not stem.endswith("-screen")


def master_name_from_any(name: str) -> str:
    """Normaliza referência: 'foo-screen.jpg' -> 'foo.jpg'."""
    p = Path(name)
    if p.stem.lower().endswith("-screen"):
        return f"{p.stem[:-7]}{p.suffix}"
    return name


@dataclass
class TsvRow:
    fields: list[str]

    @property
    def name(self) -> str:
        return self.fields[0].strip() if self.fields else ""


class State:
    """Acesso tipado aos arquivos de estado."""

    def __init__(self, state_dir: Path):
        self.dir = state_dir
        self.ids_file = state_dir / "ids"
        self.hashes_file = state_dir / "hashes.tsv"
        self.sources_file = state_dir / "sources.tsv"
        self.history_file = state_dir / "history"
        self.scores_file = state_dir / "scores.tsv"
        self.features_file = state_dir / "features.tsv"
        self.accent_file = state_dir / "accent"
        self.index_file = state_dir / "index"
        self.theme_idx_file = state_dir / "theme_idx"
        self.source_idx_file = state_dir / "source_idx"
        self.lock_file = state_dir / ".run.lock"

    # ---------- ids ----------
    def load_ids(self) -> set[str]:
        return {ln.strip() for ln in read_lines(self.ids_file) if ln.strip()}

    def has_id(self, ident: str) -> bool:
        return ident in self.load_ids()

    def add_id(self, ident: str) -> None:
        append_line(self.ids_file, ident)

    # ---------- hashes.tsv ----------
    def load_hashes(self) -> dict[str, int]:
        """nome -> dhash (último campo vence em duplicatas)."""
        out: dict[str, int] = {}
        for ln in read_lines(self.hashes_file):
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            parts = ln.split("\t")
            if len(parts) < 2:
                continue
            try:
                out[parts[0]] = int(parts[-1])
            except ValueError:
                continue
        return out

    def hash_distances(self, dhash: int) -> int:
        """Menor distância de Hamming contra todos os hashes registrados."""
        best = 64
        for h in self.load_hashes().values():
            d = (dhash ^ h).bit_count()
            if d < best:
                best = d
        return best

    def add_hash(self, name: str, dhash: int) -> None:
        append_line(self.hashes_file, f"{name}\t{dhash}")

    # ---------- sources.tsv ----------
    def load_sources(self) -> dict[str, str]:
        """nome -> fonte (última linha vence)."""
        out: dict[str, str] = {}
        for ln in read_lines(self.sources_file):
            parts = ln.split("\t")
            if len(parts) >= 2:
                name = parts[0].strip()
                if name:
                    out[name] = parts[1].strip().lower()
        return out

    def add_source(self, name: str, source: str) -> None:
        append_line(self.sources_file, f"{name}\t{source}\t{now_str()}")

    def source_of(self, name: str) -> str:
        src = self.load_sources().get(name)
        if src:
            return src
        return source_from_name(name)

    # ---------- history ----------
    def append_history(self, name: str) -> None:
        append_line(self.history_file, f"{now_str()}\t{name}")

    def load_history(self) -> list[tuple[datetime, str]]:
        out: list[tuple[datetime, str]] = []
        for ln in read_lines(self.history_file):
            parts = ln.split("\t")
            if len(parts) < 2:
                continue
            try:
                ts = datetime.strptime(parts[0].strip(), TS_FMT)
            except ValueError:
                continue
            out.append((ts, parts[1].strip()))
        return out

    def recent_names(self, n: int) -> list[str]:
        hist = self.load_history()
        return [name for _, name in hist[-n:]]

    def last_seen(self) -> dict[str, datetime]:
        out: dict[str, datetime] = {}
        for ts, name in self.load_history():
            out[name] = ts  # última ocorrência vence
        return out

    def last_rotation(self) -> tuple[datetime, str] | None:
        hist = self.load_history()
        return (hist[-1][0], hist[-1][1]) if hist else None

    # ---------- feedback (scores.tsv) ----------
    def record_feedback(self, name: str, action: str) -> None:
        delta = {"like": 1, "unlike": -1, "block": -1, "unblock": 1}.get(action)
        if delta is None:
            raise ValueError(f"ação de feedback inválida: {action}")
        append_line(self.scores_file, f"{name}\t{delta}\t{action}\t{now_str()}")

    # ---------- contadores ----------
    def _counter(self, path: Path) -> int:
        try:
            return int(read_lines(path)[0].strip())
        except (ValueError, IndexError):
            return 0

    def _set_counter(self, path: Path, value: int) -> None:
        atomic_write_text(path, f"{value}\n")

    @property
    def index(self) -> int:
        return self._counter(self.index_file)

    @index.setter
    def index(self, v: int) -> None:
        self._set_counter(self.index_file, v)

    @property
    def theme_idx(self) -> int:
        return self._counter(self.theme_idx_file)

    @theme_idx.setter
    def theme_idx(self, v: int) -> None:
        self._set_counter(self.theme_idx_file, v)

    @property
    def source_idx(self) -> int:
        return self._counter(self.source_idx_file)

    @source_idx.setter
    def source_idx(self, v: int) -> None:
        self._set_counter(self.source_idx_file, v)

    # ---------- locks ----------
    @contextmanager
    def locked(self, name: str = "run") -> Iterator[None]:
        """flock exclusivo em state/<name>.lock (no-op sem fcntl)."""
        lock_path = self.dir / f".{name}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None:
            yield
            return
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def try_lock(self, name: str = "run") -> bool:
        """Lock não-bloqueante; retorna False se já houver dono."""
        if fcntl is None:
            return True
        lock_path = self.dir / f".{name}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        # mantém o fd aberto até o processo terminar (lock morre com o processo)
        return True


_SOURCE_PREFIXES = (
    ("wallhaven-", "wallhaven"),
    ("reddit-", "reddit"),
    ("r-", "reddit"),
    ("bing-", "bing"),
    ("picsum-", "picsum"),
)


def source_from_name(name: str) -> str:
    for prefix, src in _SOURCE_PREFIXES:
        if name.startswith(prefix):
            return src
    return "other"
