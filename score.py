#!/usr/bin/env python3
"""score.py — seleção inteligente do carrossel de wallpapers (motor v6).

Substitui o antigo "próximo por índice alfabético" por um score determinístico
que combina feedback (like/block), contexto dia/noite (com crepúsculo suave),
accent do KDE, frescor, anti-série (similaridade com as últimas 6 exibições),
cota justa por fonte, impacto visual (faixa tonal), diversidade de gênero,
gosto aprendido dos likes e um jitter de desempate.

No v6 o pick não constrói mais a tabela O(n²) de similaridade: compara cada
candidato apenas com as últimas 6 exibições (≈10× mais rápido). O contexto
dia/noite agora é contínuo no crepúsculo (SCORE_NIGHT_START/SCORE_DAY_START/
SCORE_TWILIGHT_HOURS) e o accent usa o accent do KDE ou, na falta dele, a cor
de seleção do tema. Novos comandos: prune-candidates (poda orientada pela
qualidade/uso) e housekeeping (higiene de features/sources/hashes/history).

Uso:
  score.py features [--rebuild] [--json]
  score.py pick [--explain] [--json] [--top N]
  score.py score <arquivo> [--json]
  score.py like|block|unlike|unblock <arquivo>
  score.py history [--limit N] [--json]
  score.py simulate --picks N --policy old|new [--start ISO] [--json]
  score.py eval [--picks N] [--start ISO] [--json]
  score.py prune-candidates --count N [--json]
  score.py housekeeping [--json]
  score.py selftest

Estado (em $WALLPAPER_DATA_DIR/state, default ~/.local/share/wallpaper-carousel):
  features.tsv  cache de features por imagem
  scores.tsv    log de feedback (arquivo, score, origem, ts)
  history       ts<TAB>arquivo (histórico de exibições)
  accent        accent opcional (R,G,B) quando o kdeglobals não tem AccentColor
  sources.tsv   origem de cada imagem (escrito pelo wallpaper-carousel.sh)

Dependências: Python 3 + Pillow. Sem rede.
"""

from __future__ import annotations

import argparse
import base64
import colorsys
import hashlib
import json
import math
import os
import struct
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover - mensagem amigável
    sys.stderr.write("ERRO: Pillow não instalado (sudo dnf install python3-pillow)\n")
    raise SystemExit(2)

ROTATE_MINUTES = int(os.environ.get("WALLPAPER_ROTATE_MINUTES", "60"))
DEFAULT_SEED = os.environ.get("SCORE_SEED", "20261022")
JITTER_RANGE = 0.35
HIST_RECENT = 6


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _hour_env(name: str, default: int) -> int:
    value = int(_env_float(name, float(default))) % 24
    return value


# Janela dia/noite. O alvo de luma e o peso do contexto são interpolados
# suavemente no crepúsculo (SCORE_TWILIGHT_HOURS antes do início da noite e
# depois do início do dia) para não haver um salto abrupto às 19h/6h.
NIGHT_START_HOUR = _hour_env("SCORE_NIGHT_START", 19)
DAY_START_HOUR = _hour_env("SCORE_DAY_START", 6)
TWILIGHT_HOURS = max(0.0, min(4.0, _env_float("SCORE_TWILIGHT_HOURS", 1.5)))


# Contexto temporal: a luma ideal e a largura da gaussiana mudam entre dia e noite.
# À noite o alvo escuro é forte (7.0) porque o acervo escuro é um único agrupamento
# similar e a anti-série sozinha barra a sequência escura. De dia 120 é o ponto
# medido: 150 premiava só o que era muito claro (céu lavado) e transformava o
# contexto diurno num filtro de brilho; 105 deixava cair a variedade (32 imagens
# distintas em 60 picks contra 36 com 120).
NIGHT_CTX_WEIGHT = 7.0
NIGHT_LUMA_TARGET = 45.0
NIGHT_LUMA_SIGMA = 35.0
NIGHT_BRIGHT_LUMA = 110.0
NIGHT_BRIGHT_PENALTY = -3.0
DAY_CTX_WEIGHT = _env_float("SCORE_DAY_WEIGHT", 3.0)
DAY_LUMA_TARGET = _env_float("SCORE_DAY_LUMA", 120.0)
DAY_LUMA_SIGMA = _env_float("SCORE_DAY_SIGMA", 45.0)
DAY_DARK_LUMA = 55.0
DAY_DARK_PENALTY = -1.0

# Frescor: +1.0 no máximo (24h). Com +2.0 (48h) "nunca vista" valia quase todo o
# peso do contexto diurno e a ordem virava "não vistas primeiro", ciclando o pool
# inteiro em vez de escolher pela imagem.
FRESH_WEIGHT = _env_float("SCORE_FRESH_WEIGHT", 1.0)
FRESH_HOURS = _env_float("SCORE_FRESH_HOURS", 24.0)
# Repetição recente: a anti-série só olha as últimas 6 exibições, então a mesma
# imagem podia voltar depois de ~7 picks (sobretudo à noite, quando o pool escuro
# é pequeno). Este degrau tira parte do frescor de quem apareceu nas últimas
# FRESH_HOURS, incentivando acervo novo em vez de repetir exatamente a mesma foto.
RECENT_REPEAT_PENALTY = _env_float("SCORE_RECENT_REPEAT_PENALTY", 0.6)

# Accent do KDE: bônus modesto. 0,6 era forte demais quando o fallback passou a
# usar a cor de seleção do tema (o acervo tem muitos azuis e o pick repetia).
ACCENT_WEIGHT = _env_float("SCORE_ACCENT_WEIGHT", 0.2)
ACCENT_HUE_WINDOW = _env_float("SCORE_ACCENT_HUE_WINDOW", 20.0)

# Diversidade de fonte por cota justa, não por alternância. A fatia esperada de cada
# fonte é proporcional a (peso × nº de imagens no acervo). Penaliza só o excesso na
# janela recente, então não existe mais o A-B-A-B forçado que colocava um wallpaper
# do Bing a cada duas trocas.
SRC_REPEAT_PENALTY = -0.8
SRC_OVER_PENALTY = _env_float("SCORE_SRC_OVER_PENALTY", 8.0)
SRC_WINDOW = 8
SRC_PENALTY_CAP = -4.0
DEFAULT_SOURCE_WEIGHTS = {"wallhaven": 1.0, "reddit": 1.0, "bing": 0.55, "picsum": 0.75, "other": 0.8}
# Amplia a cota e dá preferência direta ao Bing apenas fora do período noturno.
DAY_BING_SHARE_MULTIPLIER = _env_float("SCORE_DAY_BING_MULTIPLIER", 12.0)
DAY_BING_BONUS = _env_float("SCORE_DAY_BING_BONUS", 2.0)


def _parse_source_weights(raw: str | None) -> dict[str, float]:
    weights = dict(DEFAULT_SOURCE_WEIGHTS)
    for item in (raw or "").split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, _, value = item.partition("=")
        try:
            weights[key.strip()] = float(value)
        except ValueError:
            continue
    return weights


SOURCE_WEIGHTS = _parse_source_weights(os.environ.get("SCORE_SOURCE_WEIGHTS"))

# Impacto visual: luma_std já estava no cache de features e não entrava no score.
# Imagem de faixa tonal estreita (névoa, parede lisa, céu chapado) rende menos como
# wallpaper que uma cena com relevo. Peso baixo: nunca sobrepõe like nem contexto.
IMPACT_CONTRAST_WEIGHT = 0.8
IMPACT_CONTRAST_FLOOR = 30.0
IMPACT_CONTRAST_FULL = 55.0
IMPACT_FLAT_STD = 25.0
IMPACT_FLAT_PENALTY = -0.5

# Gênero: a anti-série usa o máximo de similaridade e só vê quase-duplicata.
# A média com as últimas exibições pega "outra foto do mesmo tipo" (gênero), com
# peso baixo e piso alto para não premiar imagem bizarra só por ser diferente.
GENRE_WEIGHT = 0.06
GENRE_FLOOR = 45.0
GENRE_CAP = -1.5

# Gosto: um like só protegia aquela imagem. A partir de 2 likes, um centróide
# normalizado (luma, contraste, saturação, warmth, hue) generaliza o gosto para
# imagens parecidas que o usuário nunca viu. Sigma 0,25 medido no acervo atual:
# espalha o bônus de 0,17 a 1,32, enquanto 0,45 achatava tudo perto de 1,2.
TASTE_MIN_LIKES = 2
TASTE_WEIGHT = 1.5
TASTE_SIGMA = 0.25

SOURCE_PREFIXES = (
    ("bing-", "bing"),
    ("wallhaven-", "wallhaven"),
    ("reddit-", "reddit"),
    ("r-", "reddit"),
    ("picsum-", "picsum"),
)

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
DATA_DIR = Path(
    os.environ.get(
        "WALLPAPER_DATA_DIR", Path.home() / ".local/share/wallpaper-carousel"
    )
)
IMG_DIR = DATA_DIR / "images"
STATE_DIR = DATA_DIR / "state"
FEATURES_FILE = STATE_DIR / "features.tsv"
SCORES_FILE = STATE_DIR / "scores.tsv"
HISTORY_FILE = STATE_DIR / "history"
ACCENT_FILE = STATE_DIR / "accent"
SOURCES_FILE = STATE_DIR / "sources.tsv"
HASHES_FILE = STATE_DIR / "hashes.tsv"
IDS_FILE = STATE_DIR / "ids"
LOCK_FILE = STATE_DIR / ".score.lock"
KDE_GLOBALS = Path.home() / ".config" / "kdeglobals"


# --------------------------------------------------------------------------
# Utilidades de caminho / IO atômico
# --------------------------------------------------------------------------
def set_data_dir(root: str | Path) -> None:
    global DATA_DIR, IMG_DIR, STATE_DIR, FEATURES_FILE, SCORES_FILE
    global HISTORY_FILE, ACCENT_FILE, SOURCES_FILE, HASHES_FILE, IDS_FILE, LOCK_FILE
    DATA_DIR = Path(root)
    IMG_DIR = DATA_DIR / "images"
    STATE_DIR = DATA_DIR / "state"
    FEATURES_FILE = STATE_DIR / "features.tsv"
    SCORES_FILE = STATE_DIR / "scores.tsv"
    HISTORY_FILE = STATE_DIR / "history"
    ACCENT_FILE = STATE_DIR / "accent"
    SOURCES_FILE = STATE_DIR / "sources.tsv"
    HASHES_FILE = STATE_DIR / "hashes.tsv"
    IDS_FILE = STATE_DIR / "ids"
    LOCK_FILE = STATE_DIR / ".score.lock"


def ensure_dirs() -> None:
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def atomic_write(path: Path, text: str) -> None:
    """Escreve via tmp + rename no mesmo diretório (atômico)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class state_lock:
    """Lock curto e opcional para feedback/histórico (não falha se indisponível)."""

    def __enter__(self):
        self.fh = None
        try:
            import fcntl

            ensure_dirs()
            self.fh = open(LOCK_FILE, "a+")
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        except Exception:
            self.fh = None
        return self

    def __exit__(self, *exc):
        if self.fh is not None:
            try:
                import fcntl

                fcntl.flock(self.fh, fcntl.LOCK_UN)
            except Exception:
                pass
            self.fh.close()
        return False


def list_masters() -> list[str]:
    if not IMG_DIR.is_dir():
        return []
    names = []
    for p in IMG_DIR.iterdir():
        if not p.is_file() or p.suffix.lower() not in IMG_EXTS:
            continue
        if "-screen" in p.stem:
            continue
        names.append(p.name)
    return sorted(names)


def source_from_name(name: str) -> str:
    """Fonte pelo prefixo do arquivo (fallback quando sources.tsv não cobre)."""
    for prefix, src in SOURCE_PREFIXES:
        if name.startswith(prefix):
            return src
    return "other"


def load_sources() -> dict[str, str]:
    """Lê state/sources.tsv (arquivo<TAB>fonte[<TAB>ts]); tolera lixo e linhas curtas."""
    out: dict[str, str] = {}
    if not SOURCES_FILE.is_file():
        return out
    try:
        text = SOURCES_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[0].strip()
        src = parts[1].strip().lower()
        if not name or not src:
            continue
        out[name] = src  # a última linha vence (recadastro da mesma imagem)
    return out


def source_of(name: str, sources: dict[str, str] | None = None) -> str:
    if sources and name in sources:
        return sources[name]
    return source_from_name(name)


# --------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Feature:
    name: str
    w: int
    h: int
    luma: float
    luma_std: float
    warmth: float
    hue: float
    sat: float
    dhash: int
    hist: tuple[float, ...]


def _dhash_bits(px: bytes, rows: int = 8, cols: int = 8) -> int:
    bits = 0
    for r in range(rows):
        base = r * (cols + 1)
        for c in range(cols):
            bits = (bits << 1) | (1 if px[base + c] < px[base + c + 1] else 0)
    return bits


def _hist_b64(pixels: bytes) -> str:
    counts = [0] * 64
    for i in range(0, len(pixels), 3):
        r, g, b = pixels[i], pixels[i + 1], pixels[i + 2]
        counts[(r >> 6) * 16 + (g >> 6) * 4 + (b >> 6)] += 1
    return base64.b64encode(struct.pack("<64H", *counts)).decode("ascii")


def _hist_from_b64(blob: str) -> tuple[float, ...]:
    raw = base64.b64decode(blob.encode("ascii"))
    counts = struct.unpack("<64H", raw)
    total = float(sum(counts)) or 1.0
    return tuple(c / total for c in counts)


def compute_features(path: Path) -> Feature | None:
    try:
        with Image.open(path) as im:
            im.draft("RGB", (512, 512))
            w, h = im.size
            rgb = im.convert("RGB")
            small = rgb.resize((64, 64), Image.Resampling.BOX)
            gray = rgb.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    except Exception as exc:
        sys.stderr.write(f"AVISO: features de {path.name}: {exc}\n")
        return None

    raw = small.tobytes()
    n = float(len(raw) // 3) or 1.0
    r_sum = g_sum = b_sum = 0.0
    lumas = []
    sat_sum = 0.0
    hx = hy = 0.0
    for i in range(0, len(raw), 3):
        r, g, b = raw[i], raw[i + 1], raw[i + 2]
        r_sum += r
        g_sum += g
        b_sum += b
        luma = 0.299 * r + 0.587 * g + 0.114 * b
        lumas.append(luma)
        hh, ss, vv = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        sat_sum += ss
        ang = hh * 2.0 * math.pi
        hx += math.cos(ang) * ss
        hy += math.sin(ang) * ss

    luma_mean = sum(lumas) / n
    luma_std = math.sqrt(sum((x - luma_mean) ** 2 for x in lumas) / n)
    sat_mean = sat_sum / n
    if math.hypot(hx, hy) < 1e-9 or sat_mean < 0.02:
        hue = 0.0
    else:
        hue = (math.degrees(math.atan2(hy, hx)) + 360.0) % 360.0

    return Feature(
        name=path.name,
        w=w,
        h=h,
        luma=round(luma_mean, 3),
        luma_std=round(luma_std, 3),
        warmth=round((r_sum - b_sum) / n, 3),
        hue=round(hue, 3),
        sat=round(sat_mean, 4),
        dhash=_dhash_bits(gray.tobytes()),
        hist=_hist_from_b64(_hist_b64(raw)),
    )


def _serialize_feature(f: Feature) -> str:
    counts = [int(round(x * 4096)) for x in f.hist]
    # reempacota a partir das proporções normalizadas (soma volta a ~4096)
    blob = base64.b64encode(struct.pack("<64H", *counts)).decode("ascii")
    return "\t".join(
        [
            f.name,
            str(f.w),
            str(f.h),
            f"{f.luma:.3f}",
            f"{f.luma_std:.3f}",
            f"{f.warmth:.3f}",
            f"{f.hue:.3f}",
            f"{f.sat:.4f}",
            str(f.dhash),
            blob,
        ]
    )


def _parse_feature(line: str) -> Feature | None:
    parts = line.rstrip("\n").split("\t")
    if len(parts) != 10:
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
            hist=_hist_from_b64(parts[9]),
        )
    except (ValueError, TypeError, struct.error):
        return None


def load_features(rebuild: bool = False) -> dict[str, Feature]:
    """Carrega o cache e calcula sob demanda o que faltar. Regrava só se mudou."""
    ensure_dirs()
    cached: dict[str, Feature] = {}
    if not rebuild and FEATURES_FILE.is_file():
        for line in FEATURES_FILE.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            f = _parse_feature(line)
            if f is not None:
                cached[f.name] = f

    masters = list_masters()
    missing = [m for m in masters if rebuild or m not in cached]
    changed = False
    for name in missing:
        f = compute_features(IMG_DIR / name)
        if f is not None:
            cached[name] = f
            changed = True

    live = {m: cached[m] for m in masters if m in cached}
    if changed or set(cached) != set(live):
        # O cache guarda apenas imagens vivas: entradas de arquivos removidos
        # pela poda não voltam (o acervo é rotativo), então não há por que
        # mantê-las e o features.tsv deixaria de refletir o acervo atual.
        body = "\n".join(_serialize_feature(live[n]) for n in sorted(live)) + (
            "\n" if live else ""
        )
        atomic_write(FEATURES_FILE, body)

    return live


def similar(a: Feature, b: Feature) -> float:
    """Similaridade 0..100 (100 = idênticas) conforme o design do carrossel."""
    hamming = (a.dhash ^ b.dhash).bit_count()
    hist_l1 = sum(abs(x - y) for x, y in zip(a.hist, b.hist)) / 2.0
    dluma = abs(a.luma - b.luma) / 255.0
    dist = 0.55 * (hamming / 64.0) + 0.35 * hist_l1 + 0.10 * dluma
    return max(0.0, min(100.0, 100.0 * (1.0 - dist)))


# --------------------------------------------------------------------------
# Feedback / histórico / accent
# --------------------------------------------------------------------------
@dataclass
class Feedback:
    likes: int = 0
    blocked: bool = False


def load_feedback() -> dict[str, Feedback]:
    out: dict[str, Feedback] = {}
    if not SCORES_FILE.is_file():
        return out
    for line in SCORES_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name, _score, origin = parts[0], parts[1], parts[2]
        fb = out.setdefault(name, Feedback())
        if origin == "like":
            fb.likes += 1
        elif origin == "unlike":
            fb.likes = max(0, fb.likes - 1)
        elif origin == "block":
            fb.blocked = True
        elif origin == "unblock":
            fb.blocked = False
    return out


def record_feedback(action: str, name: str) -> Feedback:
    if name not in set(list_masters()):
        raise SystemExit(f"ERRO: imagem não encontrada em {IMG_DIR}: {name}")
    ensure_dirs()
    delta = {"like": 1, "unlike": -1, "block": -1, "unblock": 1}[action]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with state_lock():
        old = (
            SCORES_FILE.read_text(encoding="utf-8", errors="replace")
            if SCORES_FILE.is_file()
            else ""
        )
        if old and not old.endswith("\n"):
            old += "\n"
        atomic_write(SCORES_FILE, old + f"{name}\t{delta}\t{action}\t{ts}\n")
        return load_feedback().get(name, Feedback())


def load_history() -> list[tuple[datetime, str]]:
    rows: list[tuple[datetime, str]] = []
    if not HISTORY_FILE.is_file():
        return rows
    for line in HISTORY_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            ts = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        rows.append((ts, parts[1]))
    return rows


def _rgb_to_hue(r: int, g: int, b: int) -> float:
    hh, _ss, _vv = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    return (hh * 360.0) % 360.0


def _parse_color(value: str) -> tuple[int, int, int] | None:
    value = value.strip()
    if value.startswith("#") and len(value) == 7:
        try:
            return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)
        except ValueError:
            return None
    parts = [p.strip() for p in value.split(",")]
    if len(parts) == 3:
        try:
            rgb = tuple(int(p) for p in parts)
            if all(0 <= c <= 255 for c in rgb):
                return rgb  # type: ignore[return-value]
        except ValueError:
            pass
    return None


def read_accent_hue(kde_globals: Path | None = None) -> float | None:
    """Hue do accent do KDE, com fallback para a cor de seleção do tema.

    Ordem: ``[General] AccentColor`` → ``state/accent`` (override manual) →
    ``[Colors:Selection] BackgroundNormal`` → ``[Colors:Button]
    DecorationFocus``. O fallback ativa o bônus de accent em temas Plasma 6
    que não gravam AccentColor (caso desta máquina), sem inventar uma cor.
    Nunca grava cache derivado; falhas de leitura devolvem None.
    """
    path = kde_globals if kde_globals is not None else KDE_GLOBALS
    explicit: tuple[int, int, int] | None = None
    selection: tuple[int, int, int] | None = None
    decoration: tuple[int, int, int] | None = None
    try:
        section = ""
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
                continue
            low = line.lower()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().lower()
            if section == "General" and key == "accentcolor":
                explicit = _parse_color(value) or explicit
            elif section == "Colors:Selection" and key == "backgroundnormal":
                selection = _parse_color(value) or selection
            elif section == "Colors:Button" and key == "decorationfocus":
                decoration = _parse_color(value) or decoration
    except OSError:
        pass

    override: tuple[int, int, int] | None = None
    if ACCENT_FILE.is_file():
        try:
            override = _parse_color(
                ACCENT_FILE.read_text(encoding="utf-8", errors="replace").strip()
            )
        except OSError:
            override = None

    rgb = explicit or override or selection or decoration
    if rgb is None:
        return None
    return _rgb_to_hue(*rgb)


def _circ_dist(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def _jitter(name: str) -> float:
    seed = os.environ.get("SCORE_SEED", DEFAULT_SEED)
    digest = hashlib.sha256(f"{seed}:{name}".encode("utf-8")).digest()
    return (int.from_bytes(digest[:4], "big") / 0xFFFFFFFF) * JITTER_RANGE


def _hour_float(when: datetime) -> float:
    return when.hour + when.minute / 60.0 + when.second / 3600.0


def _night_factor(when: datetime) -> float:
    """Fator contínuo 0 (dia) .. 1 (noite), com crepúsculo nas bordas.

    O dia vai de DAY_START_HOUR até NIGHT_START_HOUR; nas TWILIGHT_HOURS
    anteriores à noite o fator sobe de 0 a 1 e nas TWILIGHT_HOURS anteriores
    ao amanhecer desce de 1 a 0. Sem crepúsculo (0h) volta ao corte seco.
    """
    h = _hour_float(when)
    dusk_start = NIGHT_START_HOUR - TWILIGHT_HOURS
    dawn_start = DAY_START_HOUR - TWILIGHT_HOURS
    if TWILIGHT_HOURS <= 0.0 or dusk_start <= DAY_START_HOUR:
        return 1.0 if (h >= NIGHT_START_HOUR or h < DAY_START_HOUR) else 0.0
    if h >= NIGHT_START_HOUR or h < dawn_start:
        return 1.0
    if h >= dusk_start:
        return (h - dusk_start) / TWILIGHT_HOURS
    if h >= DAY_START_HOUR:
        return 0.0
    return (DAY_START_HOUR - h) / TWILIGHT_HOURS


def is_night(when: datetime) -> bool:
    return _night_factor(when) >= 0.5


def _context_params(when: datetime) -> tuple[float, float, float, float]:
    """Alvo de luma, sigma, peso do contexto e fator noturno (0..1)."""
    factor = _night_factor(when)
    target = DAY_LUMA_TARGET + (NIGHT_LUMA_TARGET - DAY_LUMA_TARGET) * factor
    sigma = DAY_LUMA_SIGMA + (NIGHT_LUMA_SIGMA - DAY_LUMA_SIGMA) * factor
    weight = DAY_CTX_WEIGHT + (NIGHT_CTX_WEIGHT - DAY_CTX_WEIGHT) * factor
    return target, sigma, weight, factor


def _hours_since(when: datetime, last: datetime | None) -> float | None:
    if last is None:
        return None
    return max(0.0, (when - last).total_seconds() / 3600.0)


# --------------------------------------------------------------------------
# Score
# --------------------------------------------------------------------------
def build_sim_table(feats: dict[str, Feature]) -> dict[tuple[str, str], float]:
    table: dict[tuple[str, str], float] = {}
    names = list(feats)
    for i, a in enumerate(names):
        for b in names[i:]:
            s = similar(feats[a], feats[b])
            table[(a, b)] = s
            table[(b, a)] = s
    return table


def _sim(table: dict[tuple[str, str], float], a: str, b: str) -> float:
    return table.get((a, b), 0.0)


def _ramp(value: float, floor: float, full: float) -> float:
    if full <= floor:
        return 1.0 if value >= full else 0.0
    return max(0.0, min(1.0, (value - floor) / (full - floor)))


def _source_shares(
    feats: dict[str, Feature],
    sources: dict[str, str],
    feedback: dict[str, "Feedback"] | None = None,
    when: datetime | None = None,
) -> dict[str, float]:
    """Fatia esperada de cada fonte: peso preferido × nº de imagens no acervo.

    Imagem bloqueada sai da conta: se o usuário bloqueia metade do Bing, a cota
    do Bing precisa encolher junto, senão o resto da fonte vira cota cativa.
    """
    feedback = feedback or {}
    counts: dict[str, int] = {}
    for name in feats:
        fb = feedback.get(name)
        if fb is not None and fb.blocked:
            continue
        src = source_of(name, sources)
        counts[src] = counts.get(src, 0) + 1
    weighted = {
        src: SOURCE_WEIGHTS.get(src, SOURCE_WEIGHTS.get("other", 0.8))
        * n
        * (
            DAY_BING_SHARE_MULTIPLIER
            if src == "bing" and when is not None and _night_factor(when) < 0.5
            else 1.0
        )
        for src, n in counts.items()
    }
    total = sum(weighted.values()) or 1.0
    return {src: w / total for src, w in weighted.items()}


def _taste_target(
    feedback: dict[str, Feedback], feats: dict[str, Feature]
) -> dict[str, float] | None:
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


def _taste_score(f: Feature, target: dict[str, float]) -> float:
    d = math.sqrt(
        (
            (f.luma / 255.0 - target["luma"]) ** 2
            + (f.luma_std / 80.0 - target["std"]) ** 2
            + (f.sat - target["sat"]) ** 2
            + (f.warmth / 128.0 - target["warmth"]) ** 2
            + (_circ_dist(f.hue, target["hue"]) / 180.0) ** 2
        )
        / 5.0
    )
    return TASTE_WEIGHT * math.exp(-0.5 * (d / TASTE_SIGMA) ** 2)


def score_candidates(
    feats: dict[str, Feature],
    feedback: dict[str, Feedback],
    recent: list[str],
    when: datetime,
    accent_hue: float | None,
    table: dict[tuple[str, str], float] | None = None,
    last_seen: dict[str, datetime] | None = None,
    sources: dict[str, str] | None = None,
) -> list[dict]:
    last_seen = last_seen or {}
    sources = sources if sources is not None else load_sources()
    target, sigma, weight, night_factor = _context_params(when)
    day_bing = night_factor < 0.5
    recent_feats = [feats[n] for n in recent if n in feats]
    recent_sources = [source_of(n, sources) for n in recent]
    last_source = recent_sources[-1] if recent_sources else None
    window = recent_sources[-SRC_WINDOW:]
    window_n = len(window)
    expected = _source_shares(feats, sources, feedback, when)
    taste = _taste_target(feedback, feats)

    out = []
    for name, f in feats.items():
        fb = feedback.get(name, Feedback())
        if fb.blocked:
            continue

        comp: dict[str, float] = {}
        comp["like"] = min(fb.likes * 3.0, 6.0)
        comp["context"] = weight * math.exp(-0.5 * ((f.luma - target) / sigma) ** 2)
        comp["ctx_extra"] = 0.0
        if f.luma > NIGHT_BRIGHT_LUMA:
            comp["ctx_extra"] += NIGHT_BRIGHT_PENALTY * night_factor
        if f.luma < DAY_DARK_LUMA:
            comp["ctx_extra"] += DAY_DARK_PENALTY * (1.0 - night_factor)
        if accent_hue is None:
            comp["accent"] = 0.0
        else:
            comp["accent"] = (
                ACCENT_WEIGHT
                if _circ_dist(f.hue, accent_hue) <= ACCENT_HUE_WINDOW
                else 0.0
            )

        hours = _hours_since(when, last_seen.get(name))
        comp["fresh"] = (
            FRESH_WEIGHT
            if hours is None
            else FRESH_WEIGHT * min(1.0, hours / FRESH_HOURS)
        )
        if (
            hours is not None
            and RECENT_REPEAT_PENALTY > 0.0
            and hours < FRESH_HOURS
        ):
            comp["fresh"] -= RECENT_REPEAT_PENALTY * (1.0 - hours / FRESH_HOURS)

        comp["impact"] = IMPACT_CONTRAST_WEIGHT * _ramp(
            f.luma_std, IMPACT_CONTRAST_FLOOR, IMPACT_CONTRAST_FULL
        )
        if f.luma_std < IMPACT_FLAT_STD:
            comp["impact"] += IMPACT_FLAT_PENALTY

        comp["taste"] = _taste_score(f, taste) if taste is not None else 0.0

        # v6: sem tabela O(n²), compara só com as últimas exibições. build_sim_table
        # continua disponível para simulate/eval (métricas de pares e replay).
        if table is None:
            sims = [similar(f, r) for r in recent_feats]
        else:
            sims = [_sim(table, name, r.name) for r in recent_feats]
        max_sim = max(sims) if sims else 0.0
        mean_sim = sum(sims) / len(sims) if sims else 0.0
        comp["anti"] = -4.0 * (max_sim / 100.0)
        # Anti-série graduada: -3 acima de 60 (mesmo limiar das métricas) e
        # mais -2 acima de 75. O degrau em 60 é mais forte que os -2 originais:
        # com contexto noturno forte é o que mantém os pares similares <= 2%.
        comp["anti_extra"] = (-3.0 if max_sim > 60.0 else 0.0) + (
            -2.0 if max_sim > 75.0 else 0.0
        )
        comp["genre"] = max(
            GENRE_CAP, -GENRE_WEIGHT * max(0.0, mean_sim - GENRE_FLOOR)
        )

        cand_source = source_of(name, sources)
        comp["source_preference"] = (
            DAY_BING_BONUS if day_bing and cand_source == "bing" else 0.0
        )
        comp["source"] = 0.0
        if last_source is not None and cand_source == last_source:
            comp["source"] += SRC_REPEAT_PENALTY
        if window_n:
            over = (window.count(cand_source) / window_n) - expected.get(
                cand_source, 0.0
            )
            if over > 0:
                comp["source"] -= SRC_OVER_PENALTY * over
        comp["source"] = max(SRC_PENALTY_CAP, comp["source"])
        comp["jitter"] = _jitter(name)

        total = sum(comp.values())
        out.append(
            {
                "name": name,
                "source": cand_source,
                "luma": round(f.luma, 1),
                "similar_max": round(max_sim, 1),
                "similar_mean": round(mean_sim, 1),
                "night_factor": round(night_factor, 3),
                "context_target": round(target, 1),
                "components": {k: round(v, 3) for k, v in comp.items()},
                "total": round(total, 3),
            }
        )

    return sorted(out, key=lambda c: (-c["total"], c["name"]))


def _context(feats: dict[str, Feature]):
    """Feedback, recentes, last_seen, accent, tabela (None) e fontes.

    A tabela de similaridade O(n²) não é mais construída no pick: o motor
    compara só com as últimas HIST_RECENT exibições. Mantido o slot None na
    tupla para não quebrar chamadores/uso interativo.
    """
    history = load_history()
    recent = [n for _ts, n in history[-HIST_RECENT:]]
    last_seen: dict[str, datetime] = {}
    for ts, name in history:
        if name not in last_seen or ts > last_seen[name]:
            last_seen[name] = ts
    feedback = load_feedback()
    accent = read_accent_hue()
    sources = load_sources()
    return feedback, recent, last_seen, accent, None, sources


# --------------------------------------------------------------------------
# Poda orientada por utilidade/qualidade e higiene de estado (v6)
# --------------------------------------------------------------------------
def _file_age_days(path: Path, now: datetime) -> float:
    try:
        return max(
            0.0,
            (now - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds()
            / 86400.0,
        )
    except OSError:
        return 0.0


def prune_candidates(
    feats: dict[str, Feature],
    feedback: dict[str, Feedback],
    last_seen: dict[str, datetime] | None = None,
    count: int = 10,
    sources: dict[str, str] | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Imagens menos valiosas para remover, da mais descartável à menos.

    A poda antiga removia só por mtime (a mais antiga baixada). Aqui a ordem
    combina: nunca remove like; imagem bloqueada é descartada primeiro (nunca
    seria escolhida); imagem nunca exibida vale mais (é acervo novo a explorar);
    exibição/baixa recente protegem; impacto visual (faixa tonal) desempata.
    O shell ainda filtra as protegidas por histórico de 168h, então pedimos
    alguns candidatos a mais do que o necessário.
    """
    now = now or datetime.now()
    last_seen = last_seen or {}
    sources = sources if sources is not None else load_sources()
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
        keep += 0.5 * _ramp(f.luma_std, IMPACT_CONTRAST_FLOOR, IMPACT_CONTRAST_FULL)
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
        age = _file_age_days(IMG_DIR / name, now)
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


def _prune_table(path: Path, keep_names: set[str], lock_held: bool = True) -> dict:
    """Remove de um TSV (primeira coluna = nome) as entradas fora do acervo."""
    if not path.is_file():
        return {"before": 0, "after": 0, "removed": 0}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    kept = [ln for ln in lines if ln.split("\t", 1)[0] in keep_names]
    if len(kept) != len(lines):
        atomic_write(path, "\n".join(kept) + ("\n" if kept else ""))
    return {
        "before": len(lines),
        "after": len(kept),
        "removed": len(lines) - len(kept),
    }


def compact_history(
    keep_days: float = 30.0,
    max_entries: int = 5000,
    now: datetime | None = None,
) -> dict:
    """Compacta o histórico: janela recente + a última exibição de cada imagem.

    O frescor só precisa da última exibição por imagem e a proteção da poda
    olha 168h; o histórico completo (que cresce ~24 linhas/dia) é preservado
    apenas na janela de `keep_days` (mais a última de cada imagem viva).
    """
    now = now or datetime.now()
    masters = set(list_masters())
    history = load_history()
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
    if kept != history or len(kept) != len(history):
        body = "".join(
            f"{ts.strftime('%Y-%m-%d %H:%M:%S')}\t{name}\n" for ts, name in kept
        )
        atomic_write(HISTORY_FILE, body)
    return {
        "before": len(history),
        "after": len(kept),
        "removed": len(history) - len(kept),
    }


def run_housekeeping(
    keep_days: float = 30.0,
    max_history: int = 5000,
    now: datetime | None = None,
) -> dict:
    """Limpa estado obsoleto do acervo rotativo e devolve um resumo."""
    now = now or datetime.now()
    feats = load_features()
    masters = set(feats)
    summary: dict = {"masters": len(masters)}
    before_features = 0
    if FEATURES_FILE.is_file():
        before_features = len(
            FEATURES_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        )
    body = "\n".join(_serialize_feature(feats[n]) for n in sorted(feats)) + (
        "\n" if feats else ""
    )
    with state_lock():
        # poda entradas de imagens que saíram do acervo
        atomic_write(FEATURES_FILE, body)
        summary["features"] = {
            "before": before_features,
            "after": len(feats),
            "removed": max(0, before_features - len(feats)),
        }
        summary["sources"] = _prune_table(SOURCES_FILE, masters)
        summary["hashes"] = _prune_table(HASHES_FILE, masters)
        # ids.tsv guarda IDs já baixados (evita re-download); não podar.
        summary["history"] = compact_history(
            keep_days=keep_days, max_entries=max_history, now=now
        )
    return summary


# --------------------------------------------------------------------------
# Métricas / simulação
# --------------------------------------------------------------------------
def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _metrics(
    picks: list[str],
    feats: dict[str, Feature],
    table: dict[tuple[str, str], float],
    start: datetime,
    sources: dict[str, str] | None = None,
    feedback: dict[str, Feedback] | None = None,
) -> dict:
    sources = sources if sources is not None else load_sources()
    pairs = list(zip(picks, picks[1:]))
    sim_pairs = [_sim(table, a, b) for a, b in pairs]
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
        when = start + timedelta(minutes=ROTATE_MINUTES * i)
        luma = feats[name].luma if name in feats else 0.0
        night = is_night(when)
        if (night and luma > 95.0) or ((not night) and luma < 95.0):
            disagree += 1
    pct_disagree = round(100.0 * disagree / len(picks), 1) if picks else 0.0

    # Fatia real por fonte vs fatia esperada (peso × tamanho do acervo). O desvio
    # máximo, em pontos percentuais, é o que detecta monopólio ou alternância.
    counts: dict[str, int] = {}
    for name in picks:
        src = source_of(name, sources)
        counts[src] = counts.get(src, 0) + 1
    picks_n = len(picks) or 1
    share = {src: round(100.0 * n / picks_n, 1) for src, n in counts.items()}
    expected: dict[str, float] = {}
    if feats and picks:
        for i in range(len(picks)):
            when = start + timedelta(minutes=ROTATE_MINUTES * i)
            for src, source_share in _source_shares(
                feats, sources, feedback, when
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
        "sim_media_pares": round(sum(sim_pairs) / len(sim_pairs), 1) if sim_pairs else 0.0,
        "maior_streak_fonte": best,
        "fatia_fonte": share,
        "desvio_fonte_pp": round(deviation, 1),
        "pct_desacordo_dia_noite": pct_disagree,
        "imagens_distintas": len(set(picks)),
        "max_repeticoes": max_repeticoes,
        "luma_mediana": round(_median([f.luma for f in picked_feats]), 1),
        "contraste_mediano": round(_median([f.luma_std for f in picked_feats]), 1),
        "saturacao_mediana": round(_median([f.sat for f in picked_feats]), 1),
        "pares_avaliados": len(pairs),
    }


def simulate(
    feats: dict[str, Feature],
    feedback: dict[str, Feedback],
    picks: int,
    policy: str,
    start: datetime,
    accent_hue: float | None = None,
    seed_history: bool = True,
) -> dict:
    table = build_sim_table(feats)
    names = sorted(feats)
    if not names:
        raise SystemExit("ERRO: nenhuma imagem para simular")
    sources = load_sources()

    picked: list[str] = []
    recent: list[str] = []
    last_seen: dict[str, datetime] = {}

    # v6: a simulação parte do histórico real (últimas exibições + frescor),
    # senão o "próximo pick" simulado não corresponde ao pick ao vivo. O
    # histórico de sandbox/selftest pode estar vazio — aí começa do zero.
    if seed_history:
        history = load_history()
        for ts, name in history:
            if name not in feats:
                continue
            if name not in last_seen or ts > last_seen[name]:
                last_seen[name] = ts
        recent = [name for _ts, name in history[-HIST_RECENT:] if name in feats]

    if policy == "old":
        for i in range(picks):
            name = names[i % len(names)]
            picked.append(name)
            when = start + timedelta(minutes=ROTATE_MINUTES * i)
            last_seen[name] = when
    else:
        if accent_hue is None:
            accent_hue = read_accent_hue()
        for i in range(picks):
            when = start + timedelta(minutes=ROTATE_MINUTES * i)
            cands = score_candidates(
                feats, feedback, recent, when, accent_hue, table, last_seen, sources
            )
            if not cands:
                raise SystemExit("ERRO: todas as imagens estão bloqueadas")
            name = cands[0]["name"]
            picked.append(name)
            last_seen[name] = when
            recent = (recent + [name])[-HIST_RECENT:]

    result = _metrics(picked, feats, table, start, sources, feedback)
    result["policy"] = policy
    result["start"] = start.strftime("%Y-%m-%d %H:%M:%S")
    result["rotacao_min"] = ROTATE_MINUTES
    result["picks_list"] = picked
    return result


def _parse_start(value: str | None) -> datetime:
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
    raise SystemExit(
        f"ERRO: --start inválido: {value} (use ISO, ex.: 2026-10-22T08:00)"
    )


# --------------------------------------------------------------------------
# Comandos
# --------------------------------------------------------------------------
def cmd_features(args) -> int:
    ensure_dirs()
    feats = load_features(rebuild=args.rebuild)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "name": f.name,
                        "w": f.w,
                        "h": f.h,
                        "luma": f.luma,
                        "luma_std": f.luma_std,
                        "warmth": f.warmth,
                        "hue": f.hue,
                        "sat": f.sat,
                        "dhash": f.dhash,
                    }
                    for f in feats.values()
                ],
                indent=2,
            )
        )
        return 0
    print(
        f"{'arquivo':<34} {'dim':>11} {'luma':>7} {'std':>6} {'warm':>7} {'hue':>6} {'sat':>5}"
    )
    for f in feats.values():
        print(
            f"{f.name:<34} {f.w:>5}x{f.h:<5} {f.luma:>7.1f} {f.luma_std:>6.1f} "
            f"{f.warmth:>7.1f} {f.hue:>6.1f} {f.sat:>5.2f}"
        )
    print(f"\n{len(feats)} imagem(ns) — cache: {FEATURES_FILE}")
    return 0


def _print_breakdown(cands: list[dict], top: int) -> None:
    print(
        f"{'#':<2} {'arquivo':<34} {'total':>7} {'like':>6} {'ctx':>6} {'ctx+':>6} "
        f"{'acc':>5} {'fresh':>6} {'imp':>5} {'taste':>6} {'anti':>6} {'extra':>6} "
        f"{'gen':>5} {'src':>6} {'src+':>6} {'jit':>5} {'sim6':>5}"
    )
    for i, c in enumerate(cands[:top], 1):
        k = c["components"]
        print(
            f"{i:<2} {c['name']:<34} {c['total']:>7.2f} {k['like']:>6.2f} {k['context']:>6.2f} "
            f"{k['ctx_extra']:>6.2f} {k['accent']:>5.1f} {k['fresh']:>6.2f} {k['impact']:>5.2f} "
            f"{k['taste']:>6.2f} {k['anti']:>6.2f} {k['anti_extra']:>6.2f} "
            f"{k['genre']:>5.2f} {k['source']:>6.2f} {k['source_preference']:>6.2f} "
            f"{k['jitter']:>5.2f} "
            f"{c['similar_max']:>5.1f}"
        )


_REASON_LABELS = {
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


def _reason_summary(cand: dict) -> str:
    """Resumo legível dos componentes que mais pesaram na escolha."""
    items = [
        (abs(v), k, v)
        for k, v in cand.get("components", {}).items()
        if abs(v) >= 0.05
    ]
    items.sort(reverse=True)
    return "; ".join(
        f"{_REASON_LABELS.get(k, k)} {v:+.2f}" for _a, k, v in items
    ) or "empate técnico"


def cmd_pick(args) -> int:
    feats = load_features()
    if not feats:
        sys.stderr.write(f"AVISO: nenhuma imagem em {IMG_DIR}\n")
        return 4
    when = datetime.now()
    feedback, recent, last_seen, accent, table, sources = _context(feats)
    cands = score_candidates(
        feats, feedback, recent, when, accent, table, last_seen, sources
    )
    if not cands:
        sys.stderr.write("AVISO: todas as imagens estão bloqueadas\n")
        return 4

    likes = sum(1 for fb in feedback.values() if fb.likes > 0)
    blocks = sum(1 for fb in feedback.values() if fb.blocked)
    payload = {
        "choice": cands[0]["name"],
        "score": cands[0]["total"],
        "now": when.strftime("%Y-%m-%d %H:%M:%S"),
        "night": is_night(when),
        "counts": {
            "likes": likes,
            "blocks": blocks,
            "candidates": len(cands),
            "history": len(load_history()),
        },
        "accent_hue": round(accent, 1) if accent is not None else None,
        "night_factor": cands[0]["night_factor"],
        "context_target": cands[0]["context_target"],
        "top": cands[: args.top],
    }

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if args.explain:
        print(
            f"Contexto: {'noite' if payload['night'] else 'dia'} ({when.strftime('%H:%M')}) | "
            f"accent: {payload['accent_hue']} | likes: {likes} | blocks: {blocks} | "
            f"candidatos: {len(cands)}"
        )
        print(
            f"Escolha: {cands[0]['name']}  (score {cands[0]['total']:.2f})"
        )
        _print_breakdown(cands, args.top)
        print(f"\nPor quê: {_reason_summary(cands[0])}")
        return 0
    print(cands[0]["name"])
    return 0


def cmd_score(args) -> int:
    name = os.path.basename(args.file)
    feats = load_features()
    if name not in feats:
        sys.stderr.write(f"ERRO: imagem não encontrada em {IMG_DIR}: {name}\n")
        return 2
    when = datetime.now()
    feedback, recent, last_seen, accent, table, sources = _context(feats)
    cands = score_candidates(
        feats, feedback, recent, when, accent, table, last_seen, sources
    )
    found = next((c for c in cands if c["name"] == name), None)
    if found is None:
        fb = feedback.get(name, Feedback())
        print(
            json.dumps({"name": name, "blocked": fb.blocked, "total": None})
            if args.json
            else f"{name}: EXCLUÍDA (block)"
        )
        return 0
    if args.json:
        print(json.dumps(found, indent=2, ensure_ascii=False))
    else:
        print(
            f"{name}  score {found['total']:.2f}  (luma {found['luma']}, sim6 {found['similar_max']})"
        )
        for k, v in found["components"].items():
            print(f"  {k:<10} {v:+.3f}")
    return 0


def cmd_feedback(args) -> int:
    name = os.path.basename(args.file)
    fb = record_feedback(args.action, name)
    print(f"{args.action}\t{name}\tlikes={fb.likes}\tblocked={str(fb.blocked).lower()}")
    return 0


def cmd_history(args) -> int:
    rows = load_history()
    if args.json:
        print(
            json.dumps(
                [
                    {"ts": ts.strftime("%Y-%m-%d %H:%M:%S"), "name": n}
                    for ts, n in rows[-args.limit :]
                ],
                indent=2,
            )
        )
        return 0
    for ts, name in rows[-args.limit :]:
        print(f"{ts.strftime('%Y-%m-%d %H:%M:%S')}\t{name}")
    return 0


def cmd_prune_candidates(args) -> int:
    feats = load_features()
    if not feats:
        return 0
    feedback = load_feedback()
    last_seen: dict[str, datetime] = {}
    for ts, name in load_history():
        if name in feats and (name not in last_seen or ts > last_seen[name]):
            last_seen[name] = ts
    rows = prune_candidates(
        feats,
        feedback,
        last_seen,
        count=max(0, args.count),
        sources=load_sources(),
    )
    if args.json:
        print(
            json.dumps(
                {"count": len(rows), "candidates": rows},
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        for row in rows:
            print(row["name"])
    return 0


def cmd_housekeeping(args) -> int:
    summary = run_housekeeping(
        keep_days=max(1.0, args.keep_days),
        max_history=max(0, args.max_history),
    )
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
    print("=== Housekeeping do carrossel ===")
    print(
        f"Acervo              : {summary['masters']} imagem(ns)\n"
        f"features.tsv        : {summary['features']['before']} → "
        f"{summary['features']['after']} "
        f"({summary['features']['removed']} removida(s))\n"
        f"sources.tsv         : {summary['sources']['before']} → "
        f"{summary['sources']['after']} "
        f"({summary['sources']['removed']} removida(s))\n"
        f"hashes.tsv          : {summary['hashes']['before']} → "
        f"{summary['hashes']['after']} "
        f"({summary['hashes']['removed']} removida(s))\n"
        f"history             : {summary['history']['before']} → "
        f"{summary['history']['after']} "
        f"({summary['history']['removed']} removida(s), janela {args.keep_days:g} dias)"
    )
    return 0


def cmd_simulate(args) -> int:
    feats = load_features()
    feedback = load_feedback()
    start = _parse_start(args.start)
    result = simulate(feats, feedback, args.picks, args.policy, start)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    print(
        f"Política {result['policy']} — {result['picks']} picks, início {result['start']}, "
        f"rotação {result['rotacao_min']}min"
    )
    print(f"  pares consecutivos similares (>60): {result['pct_pares_similares']}%")
    print(f"  similaridade média entre pares    : {result['sim_media_pares']}")
    print(f"  maior streak da mesma fonte       : {result['maior_streak_fonte']}")
    print(f"  fatia por fonte                   : {result['fatia_fonte']}")
    print(f"  desvio da fatia esperada          : {result['desvio_fonte_pp']}pp")
    print(f"  picks em desacordo dia/noite      : {result['pct_desacordo_dia_noite']}%")
    print(f"  imagens distintas                 : {result['imagens_distintas']}")
    print(
        f"  luma/contraste/saturação (mediana): "
        f"{result['luma_mediana']}/{result['contraste_mediano']}/{result['saturacao_mediana']}"
    )
    return 0


def _eval_checks(old: dict, new: dict, pool: int) -> dict[str, bool]:
    # A política alfabética cicla o pool inteiro, então "distintas" contra ela é
    # inalcançável por desenho. Os patamares abaixo valem para o horizon comum (60-90
    # picks); simulações muito longas (>150 picks) ficam sujeitas à poda rotativa do
    # acervo e às repetições noturnas inevitáveis (anti-série bloqueia a mesma imagem
    # por ~7 picks quando o pool escuro é pequeno), por isso são menos confiáveis.
    horizon = min(pool, new["picks"]) or 1
    return {
        "anti_serie": new["pct_pares_similares"] <= old["pct_pares_similares"],
        "similaridade_media": new["sim_media_pares"] <= old["sim_media_pares"],
        "fatia_fonte": new["desvio_fonte_pp"] <= old["desvio_fonte_pp"],
        "contexto_dia_noite": new["pct_desacordo_dia_noite"]
        <= old["pct_desacordo_dia_noite"],
        "diversidade": new["imagens_distintas"] >= 0.30 * horizon,
        "repeticao": new["max_repeticoes"] <= 8,
    }
    return {
        "anti_serie": new["pct_pares_similares"] <= old["pct_pares_similares"],
        "similaridade_media": new["sim_media_pares"] <= old["sim_media_pares"],
        "fatia_fonte": new["desvio_fonte_pp"] <= old["desvio_fonte_pp"],
        "contexto_dia_noite": new["pct_desacordo_dia_noite"]
        <= old["pct_desacordo_dia_noite"],
        "diversidade": new["imagens_distintas"] >= 0.30 * horizon,
        "repeticao": new["max_repeticoes"] <= 8,
    }


def cmd_eval(args) -> int:
    feats = load_features()
    feedback = load_feedback()
    start = _parse_start(args.start)
    old = simulate(feats, feedback, args.picks, "old", start)
    new = simulate(feats, feedback, args.picks, "new", start)
    checks = _eval_checks(old, new, len(feats))
    if args.json:
        for r in (old, new):
            r.pop("picks_list", None)
        print(
            json.dumps(
                {"old": old, "new": new, "checks": checks, "criterio_ok": all(checks.values())},
                indent=2,
            )
        )
        return 0 if all(checks.values()) else 1

    def pp(d: dict) -> str:
        return " ".join(f"{k}={v}%" for k, v in sorted(d["fatia_fonte"].items()))

    rows = [
        (
            "Pares consecutivos similares",
            f"{old['pct_pares_similares']:.1f}%",
            f"{new['pct_pares_similares']:.1f}%",
        ),
        (
            "Similaridade média entre pares",
            f"{old['sim_media_pares']:.1f}",
            f"{new['sim_media_pares']:.1f}",
        ),
        (
            "Maior streak mesma fonte",
            str(old["maior_streak_fonte"]),
            str(new["maior_streak_fonte"]),
        ),
        (
            "Desvio da fatia de fonte (pp)",
            f"{old['desvio_fonte_pp']:.1f}",
            f"{new['desvio_fonte_pp']:.1f}",
        ),
        (
            "Picks em desacordo dia/noite",
            f"{old['pct_desacordo_dia_noite']:.1f}%",
            f"{new['pct_desacordo_dia_noite']:.1f}%",
        ),
        (
            "Imagens distintas",
            str(old["imagens_distintas"]),
            str(new["imagens_distintas"]),
        ),
        (
            "Máx. repetições da mesma imagem",
            str(old["max_repeticoes"]),
            str(new["max_repeticoes"]),
        ),
        (
            "Luma mediana",
            f"{old['luma_mediana']:.1f}",
            f"{new['luma_mediana']:.1f}",
        ),
        (
            "Contraste mediano",
            f"{old['contraste_mediano']:.1f}",
            f"{new['contraste_mediano']:.1f}",
        ),
        (
            "Saturação mediana",
            f"{old['saturacao_mediana']:.1f}",
            f"{new['saturacao_mediana']:.1f}",
        ),
    ]
    print(
        f"=== Avaliação: {args.picks} picks, início {old['start']}, rotação {ROTATE_MINUTES}min ==="
    )
    print(f"{'Métrica':<34} {'old':>10} {'new':>10}")
    for label, a, b in rows:
        print(f"{label:<34} {a:>10} {b:>10}")
    print(f"\nFatia por fonte   old: {pp(old)}")
    print(f"                  new: {pp(new)}")
    print("\nCritérios:")
    for label, passed in checks.items():
        print(f"  {'OK  ' if passed else 'FALHOU'} {label}")
    return 0 if all(checks.values()) else 1


# --------------------------------------------------------------------------
# Selftest
# --------------------------------------------------------------------------
def _make_fixture(path: Path, kind: str) -> None:
    size = (320, 180)
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            if kind == "dark":
                px[x, y] = (42 + (y // 30) * 6, 45, 52 + (x // 40) * 4)
            elif kind == "bright":
                px[x, y] = (148 + (x // 40) * 6, 143 + (y // 30) * 5, 132)
            elif kind == "mid":
                px[x, y] = (128, 128, 128)
            elif kind == "blue":
                px[x, y] = (20 + x // 8, 40 + y // 8, 140 + x // 4)
            elif kind == "blue2":
                px[x, y] = (20 + x // 8, 41 + y // 8, 140 + x // 4)
            elif kind == "green":
                px[x, y] = (20, 120 + x // 8, 40)
            elif kind == "busy":
                v = int(255 * x / (size[0] - 1))
                px[x, y] = (v, v, v)
            else:
                px[x, y] = (0, 0, 0)
    img.save(path, quality=95)


def run_selftest() -> int:
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="wc-selftest-"))
    set_data_dir(tmp)
    ensure_dirs()

    for name, kind in (
        ("escuro.jpg", "dark"),
        ("claro.jpg", "bright"),
        ("meio.jpg", "mid"),
        ("serie-a.jpg", "blue"),
        ("serie-b.jpg", "blue2"),
        ("verde.jpg", "green"),
        ("contraste.jpg", "busy"),
        ("src-a.jpg", "mid"),
        ("src-b.jpg", "mid"),
    ):
        _make_fixture(IMG_DIR / name, kind)

    def check(label: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"PASS  {label}")
        else:
            print(f"FAIL  {label}  {detail}")
            failures.append(label)

    try:
        feats = load_features(rebuild=True)
        check("features calculadas", len(feats) == 9, f"obtidas {len(feats)}")
        check(
            "dimensões corretas",
            feats["escuro.jpg"].w == 320 and feats["escuro.jpg"].h == 180,
        )
        check(
            "similaridade(a,a)=100",
            similar(feats["escuro.jpg"], feats["escuro.jpg"]) > 99.9,
        )
        check(
            "similaridade claro/escuro baixa",
            similar(feats["claro.jpg"], feats["escuro.jpg"]) < 70,
        )

        # 1) block nunca escolhido
        record_feedback("block", "claro.jpg")
        night = datetime(2026, 10, 22, 23, 0)
        fb = load_feedback()
        table = build_sim_table(feats)
        cands = score_candidates(feats, fb, [], night, None, table, {})
        check("block exclui candidato", all(c["name"] != "claro.jpg" for c in cands))
        check("block persiste", fb.get("claro.jpg", Feedback()).blocked)
        sim = simulate(feats, fb, 30, "new", night)
        check("block nunca escolhido em 30 picks", "claro.jpg" not in sim["picks_list"])
        record_feedback("unblock", "claro.jpg")

        # 2) par quase idêntico penalizado
        record_feedback("like", "serie-a.jpg")  # garante que a série é atrativa
        fb = load_feedback()
        cands = score_candidates(feats, fb, ["serie-a.jpg"], night, None, table, {})
        b = next(c for c in cands if c["name"] == "serie-b.jpg")
        check(
            "similaridade serie-a/serie-b alta",
            similar(feats["serie-a.jpg"], feats["serie-b.jpg"]) > 75,
        )
        check(
            "anti-série penaliza par quase idêntico",
            b["components"]["anti"] <= -3.0 and b["components"]["anti_extra"] < 0,
            f"anti={b['components']['anti']} extra={b['components']['anti_extra']}",
        )

        # 3) noite prefere escura, dia prefere clara
        record_feedback("unlike", "serie-a.jpg")  # limpa o like do teste 2
        fb = load_feedback()
        table = build_sim_table(feats)
        cnight = score_candidates(feats, fb, [], night, None, table, {})[0]
        cday = score_candidates(
            feats, fb, [], datetime(2026, 10, 22, 12, 0), None, table, {}
        )[0]
        check(
            "noite prefere imagem escura",
            feats[cnight["name"]].luma < 100,
            f"luma={feats[cnight['name']].luma}",
        )
        check(
            "dia prefere imagem mais clara que a noite",
            feats[cday["name"]].luma > feats[cnight["name"]].luma,
            f"dia={feats[cday['name']].luma} noite={feats[cnight['name']].luma}",
        )
        cnight_all = score_candidates(feats, fb, [], night, None, table, {})
        cday_all = score_candidates(
            feats, fb, [], datetime(2026, 10, 22, 12, 0), None, table, {}
        )
        dark_night = next(c for c in cnight_all if c["name"] == "escuro.jpg")
        bright_night = next(c for c in cnight_all if c["name"] == "claro.jpg")
        dark_day = next(c for c in cday_all if c["name"] == "escuro.jpg")
        check(
            "noite aplica bônus de contexto cheio",
            dark_night["components"]["context"] > 6.0,
            f"context={dark_night['components']['context']}",
        )
        check(
            "noite penaliza imagem clara (luma > 110)",
            bright_night["components"]["ctx_extra"] == NIGHT_BRIGHT_PENALTY,
            f"extra={bright_night['components']['ctx_extra']}",
        )
        check(
            "dia penaliza imagem escura (luma < 70)",
            dark_day["components"]["ctx_extra"] == DAY_DARK_PENALTY,
            f"extra={dark_day['components']['ctx_extra']}",
        )

        # 4) persistência like/block roundtrip
        for act in ("like", "like", "block", "unblock", "unlike"):
            record_feedback(act, "verde.jpg")
        fb = load_feedback()
        g = fb.get("verde.jpg", Feedback())
        check("roundtrip like/unlike", g.likes == 1, f"likes={g.likes}")
        check("roundtrip block/unblock", g.blocked is False)
        lines = [ln for ln in SCORES_FILE.read_text().splitlines() if ln.strip()]
        green = sum(1 for ln in lines if ln.startswith("verde.jpg\t"))
        check("scores.tsv registra eventos", green == 5, f"eventos={green}")

        # 5) simulate determinístico
        record_feedback("block", "claro.jpg")
        fb = load_feedback()
        start = datetime(2026, 10, 22, 8, 0)
        a = simulate(feats, fb, 20, "new", start)
        b2 = simulate(feats, fb, 20, "new", start)
        check(
            "simulate determinístico",
            a["picks_list"] == b2["picks_list"]
            and a["pct_pares_similares"] == b2["pct_pares_similares"],
        )
        check("simulate respeita block", "claro.jpg" not in a["picks_list"])

        # 6) cache de features roundtrip
        before = FEATURES_FILE.read_text()
        load_features()
        check("cache de features reaproveitado", FEATURES_FILE.read_text() == before)

        # 7) diversidade de fonte (sources.tsv + fallback pelo prefixo)
        check("fonte por prefixo bing", source_of("bing-20260101.jpg") == "bing")
        check(
            "fonte por prefixo wallhaven",
            source_of("wallhaven-abc.jpg") == "wallhaven",
        )
        check(
            "fonte por prefixo reddit",
            source_of("reddit-x.jpg") == "reddit" and source_of("r-x.jpg") == "reddit",
        )
        check("fonte por prefixo other", source_of("foto.jpg") == "other")
        atomic_write(
            SOURCES_FILE,
            "bing-20260101.jpg\twallhaven\n"
            "src-a.jpg\tbing\n"
            "src-b.jpg\twallhaven\n"
            "bing-anchor.jpg\tbing\n"
            "wallhaven-anchor.jpg\twallhaven\n"
            "linha-sem-tab\n",
        )
        src_map = load_sources()
        check(
            "sources.tsv lido e linha inválida ignorada",
            src_map.get("src-a.jpg") == "bing" and "linha-sem-tab" not in src_map,
        )
        check(
            "sources.tsv sobrepõe o prefixo",
            source_of("bing-20260101.jpg", src_map) == "wallhaven",
        )
        day = datetime(2026, 10, 22, 12, 0)
        night_shares = _source_shares(feats, src_map, {}, night)
        day_shares = _source_shares(feats, src_map, {}, day)
        check(
            "cota Bing aumenta apenas de dia",
            day_shares.get("bing", 0.0) > night_shares.get("bing", 0.0),
        )
        preference_sources = dict(src_map)
        preference_sources.update({"claro.jpg": "bing", "escuro.jpg": "wallhaven"})
        day_candidates = score_candidates(
            feats, {}, [], day, None, table, {}, preference_sources
        )
        night_candidates = score_candidates(
            feats, {}, [], night, None, table, {}, preference_sources
        )
        day_bing = next(c for c in day_candidates if c["name"] == "claro.jpg")
        night_bing = next(c for c in night_candidates if c["name"] == "claro.jpg")
        check(
            "Bing recebe bônus somente de dia",
            day_bing["components"]["source_preference"] == DAY_BING_BONUS
            and night_bing["components"]["source_preference"] == 0.0,
        )
        # 8) impacto visual (contraste/saturação) e gosto generalizado
        record_feedback("unblock", "claro.jpg")
        table = build_sim_table(feats)
        cands_day = score_candidates(feats, load_feedback(), [], day, None, table, {})
        by_name = {c["name"]: c for c in cands_day}
        check(
            "dia usa alvo 120 (cinza 128 > claro 157)",
            by_name["meio.jpg"]["components"]["context"]
            > by_name["claro.jpg"]["components"]["context"],
            f"meio={by_name['meio.jpg']['components']['context']} "
            f"claro={by_name['claro.jpg']['components']['context']}",
        )
        check(
            "imagem plana perde impacto",
            by_name["meio.jpg"]["components"]["impact"] < 0.0
            and by_name["contraste.jpg"]["components"]["impact"] > 0.5,
            f"meio={by_name['meio.jpg']['components']['impact']} "
            f"contraste={by_name['contraste.jpg']['components']['impact']}",
        )
        check("frescor não domina o contexto diurno", FRESH_WEIGHT < DAY_CTX_WEIGHT)
        check("gosto precisa de 2 likes", _taste_target(load_feedback(), feats) is None)
        record_feedback("like", "serie-a.jpg")
        record_feedback("like", "serie-b.jpg")
        target = _taste_target(load_feedback(), feats)
        check("gosto generaliza com 2 likes", target is not None)
        if target is not None:
            check(
                "gosto aproxima imagens parecidas",
                _taste_score(feats["serie-a.jpg"], target)
                > _taste_score(feats["verde.jpg"], target),
            )
        record_feedback("unlike", "serie-a.jpg")
        record_feedback("unlike", "serie-b.jpg")

        for name in (
            "escuro.jpg",
            "claro.jpg",
            "meio.jpg",
            "serie-a.jpg",
            "serie-b.jpg",
            "verde.jpg",
            "contraste.jpg",
        ):
            record_feedback("block", name)
        table = build_sim_table(feats)
        cands = score_candidates(
            feats, load_feedback(), ["bing-anchor.jpg"], night, None, table, {}, src_map
        )
        a_src = next(c for c in cands if c["name"] == "src-a.jpg")
        b_src = next(c for c in cands if c["name"] == "src-b.jpg")
        check(
            "fonte monopólio penaliza forte",
            a_src["components"]["source"] < SRC_REPEAT_PENALTY,
            f"src={a_src['components']['source']}",
        )
        check(
            "fonte diferente não penaliza",
            b_src["components"]["source"] == 0.0,
            f"src={b_src['components']['source']}",
        )
        check(
            "diversidade de fonte muda a escolha",
            cands[0]["name"] == "src-b.jpg",
            f"top={cands[0]['name']}",
        )
        fair = score_candidates(
            feats,
            load_feedback(),
            ["bing-anchor.jpg"] + ["wallhaven-anchor.jpg"] * 7,
            night,
            None,
            table,
            {},
            src_map,
        )
        a_fair = next(c for c in fair if c["name"] == "src-a.jpg")
        check(
            "fonte na cota esperada quase não penaliza",
            a_fair["components"]["source"] > -1.0,
            f"src={a_fair['components']['source']}",
        )
        check(
            "monopólio penaliza mais que cota justa",
            a_src["components"]["source"] < a_fair["components"]["source"],
            f"mono={a_src['components']['source']} cota={a_fair['components']['source']}",
        )
        for name in (
            "escuro.jpg",
            "claro.jpg",
            "meio.jpg",
            "serie-a.jpg",
            "serie-b.jpg",
            "verde.jpg",
        ):
            record_feedback("unblock", name)

        # 9) v6: crepúsculo suave e accent de fallback do tema
        check(
            "crepúsculo: dia puro ao meio-dia",
            _night_factor(datetime(2026, 10, 22, 12, 0)) == 0.0,
        )
        check(
            "crepúsculo: noite pura às 23h",
            _night_factor(datetime(2026, 10, 22, 23, 0)) == 1.0,
        )
        tw_hour = NIGHT_START_HOUR - TWILIGHT_HOURS / 2.0
        if TWILIGHT_HOURS > 0 and NIGHT_START_HOUR - TWILIGHT_HOURS > DAY_START_HOUR:
            f_tw = _night_factor(
                datetime(2026, 10, 22, int(tw_hour), int((tw_hour % 1) * 60))
            )
            check(
                "crepúsculo: fator intermediário antes da noite",
                0.0 < f_tw < 1.0,
                f"f={f_tw:.3f}",
            )
            tgt_day, _s, _w, _f = _context_params(datetime(2026, 10, 22, 12, 0))
            tgt_night, _s, _w, _f = _context_params(datetime(2026, 10, 22, 23, 0))
            tgt_tw, _s, _w, _f = _context_params(
                datetime(2026, 10, 22, int(tw_hour), int((tw_hour % 1) * 60))
            )
            check(
                "crepúsculo: alvo de luma interpolado",
                tgt_night < tgt_tw < tgt_day,
                f"dia={tgt_day:.1f} tw={tgt_tw:.1f} noite={tgt_night:.1f}",
            )
        else:
            check("crepúsculo: desativado por configuração", TWILIGHT_HOURS <= 0)

        fake_kde = tmp / "kdeglobals-fake"
        fake_kde.write_text(
            "[Colors:Selection]\nBackgroundNormal=61,174,233\n", encoding="utf-8"
        )
        accent_blue = read_accent_hue(fake_kde)
        check(
            "accent: fallback para a cor de seleção do tema",
            accent_blue is not None and 195.0 <= accent_blue <= 210.0,
            f"hue={accent_blue}",
        )
        fake_kde.write_text(
            "[General]\nAccentColor=220,40,40\n", encoding="utf-8"
        )
        accent_red = read_accent_hue(fake_kde)
        check(
            "accent: AccentColor explícito tem prioridade",
            accent_red is not None and (accent_red < 10.0 or accent_red > 350.0),
            f"hue={accent_red}",
        )

        # 10) v6: pick sem tabela O(n²) equivale ao pick com tabela
        fb = load_feedback()
        table = build_sim_table(feats)
        with_t = score_candidates(feats, fb, ["serie-a.jpg"], night, None, table, {})
        no_t = score_candidates(feats, fb, ["serie-a.jpg"], night, None, None, {})
        check(
            "pick sem tabela de similaridade equivale à tabela",
            len(with_t) == len(no_t)
            and all(
                abs(a["total"] - b["total"]) < 1e-9 for a, b in zip(with_t, no_t)
            ),
        )

        # 11) v6: poda orientada por utilidade (like nunca sai; block primeiro)
        record_feedback("block", "escuro.jpg")
        record_feedback("like", "serie-a.jpg")
        pruned = prune_candidates(
            load_features(), load_feedback(), last_seen={}, count=5
        )
        pruned_names = [row["name"] for row in pruned]
        check(
            "poda: imagem bloqueada é a primeira candidata",
            pruned_names[0] == "escuro.jpg",
            f"ordem={pruned_names}",
        )
        check(
            "poda: imagem com like nunca é removida",
            "serie-a.jpg" not in pruned_names,
            f"ordem={pruned_names}",
        )
        record_feedback("unblock", "escuro.jpg")
        record_feedback("unlike", "serie-a.jpg")

        # 12) v6: housekeeping remove estado obsoleto e mantém o vivo
        fantasma = Feature(
            name="fantasma.jpg",
            w=320,
            h=180,
            luma=128.0,
            luma_std=40.0,
            warmth=0.0,
            hue=0.0,
            sat=0.1,
            dhash=0,
            hist=tuple([1.0 / 64.0] * 64),
        )
        with FEATURES_FILE.open("a", encoding="utf-8") as fh:
            fh.write(_serialize_feature(fantasma) + "\n")
        atomic_write(
            SOURCES_FILE,
            (SOURCES_FILE.read_text() if SOURCES_FILE.is_file() else "")
            + "fantasma.jpg\twallhaven\n",
        )
        atomic_write(
            HASHES_FILE,
            (HASHES_FILE.read_text() if HASHES_FILE.is_file() else "")
            + "fantasma.jpg\t12345\n",
        )
        old_ts = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d %H:%M:%S")
        recent_ts = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        atomic_write(
            HISTORY_FILE,
            f"{old_ts}\tescuro.jpg\n{recent_ts}\tclaro.jpg\n{old_ts}\tfantasma.jpg\n",
        )
        summary = run_housekeeping(keep_days=30, max_history=100)
        check(
            "housekeeping: features obsoletas podadas",
            "fantasma.jpg" not in FEATURES_FILE.read_text(),
            f"features={summary['features']}",
        )
        check(
            "housekeeping: sources/hashes obsoletos podados",
            "fantasma.jpg" not in SOURCES_FILE.read_text()
            and "fantasma.jpg" not in HASHES_FILE.read_text(),
        )
        hist_names = [name for _ts, name in load_history()]
        check(
            "housekeeping: histórico mantém janela e última exibição",
            "fantasma.jpg" not in hist_names
            and "claro.jpg" in hist_names
            and "escuro.jpg" in hist_names,
            f"history={hist_names}",
        )
        check(
            "housekeeping: resumo com contagens coerentes",
            summary["features"]["after"] == len(feats)
            and summary["history"]["after"] <= summary["history"]["before"],
        )
        # limpa histórico de teste para não afetar simulações seguintes
        atomic_write(HISTORY_FILE, "")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"\nSELFTEST FALHOU: {len(failures)} teste(s): {', '.join(failures)}")
        return 1
    print("\nSELFTEST OK")
    return 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="score.py", description="Seleção do carrossel de wallpapers"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("features", help="calcula/exibe o cache de features")
    sp.add_argument("--rebuild", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_features)

    sp = sub.add_parser("pick", help="imprime a próxima imagem escolhida")
    sp.add_argument("--explain", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--top", type=int, default=8)
    sp.set_defaults(func=cmd_pick)

    sp = sub.add_parser("score", help="breakdown do score de um arquivo")
    sp.add_argument("file")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_score)

    for action in ("like", "block", "unlike", "unblock"):
        sp = sub.add_parser(action, help=f"registra {action} para um arquivo")
        sp.add_argument("file")
        sp.set_defaults(func=cmd_feedback, action=action)

    sp = sub.add_parser("history", help="histórico de exibições")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("prune-candidates", help="lista imagens candidatas à poda")
    sp.add_argument("--count", type=int, default=20)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_prune_candidates)

    sp = sub.add_parser(
        "housekeeping", help="limpa features/sources/hashes/history obsoletos"
    )
    sp.add_argument("--keep-days", type=float, default=30.0)
    sp.add_argument("--max-history", type=int, default=5000)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_housekeeping)

    sp = sub.add_parser("simulate", help="simula N picks com uma política")
    sp.add_argument("--picks", type=int, default=60)
    sp.add_argument("--policy", choices=("old", "new"), required=True)
    sp.add_argument("--start")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_simulate)

    sp = sub.add_parser("eval", help="compara old vs new")
    sp.add_argument("--picks", type=int, default=60)
    sp.add_argument("--start")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_eval)

    sp = sub.add_parser("selftest", help="testes sintéticos")
    sp.set_defaults(func=lambda _a: run_selftest())
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ensure_dirs()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
