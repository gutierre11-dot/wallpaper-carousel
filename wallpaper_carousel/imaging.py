"""Imaging: features, dHash, cache de tela e validação de qualidade.

Substitui ImageMagick (`magick`) e `file` por Pillow puro. Os formatos de
features.tsv/hashes.tsv são idênticos aos da v6 — compatibilidade total.
"""

from __future__ import annotations

import base64
import colorsys
import math
import struct
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .state import IMAGE_EXTS


class ImageError(Exception):
    """Imagem ilegível ou corrompida."""


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
    hist_b64: str

    def to_tsv(self) -> str:
        return "\t".join(
            [
                self.name,
                str(self.w),
                str(self.h),
                f"{self.luma:.3f}",
                f"{self.luma_std:.3f}",
                f"{self.warmth:.3f}",
                f"{self.hue:.3f}",
                f"{self.sat:.4f}",
                str(self.dhash),
                self.hist_b64,
            ]
        )


@dataclass(frozen=True)
class Probe:
    w: int
    h: int
    size_bytes: int
    format: str


def probe(path: Path) -> Probe:
    """Dimensões + tamanho sem decodificar pixels.

    Não usar ``draft`` aqui: ele reduz ``im.size`` para JPEG e corromperia
    o quality gate e as decisões de cache de tela.
    """
    try:
        with Image.open(path) as im:
            w, h = im.size
            fmt = (im.format or "").upper()
    except Exception as exc:
        raise ImageError(f"não foi possível ler {path.name}: {exc}") from exc
    return Probe(w=w, h=h, size_bytes=path.stat().st_size, format=fmt)


def is_image_file(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTS


def _dhash_bits(px: bytes, rows: int = 8, cols: int = 8) -> int:
    bits = 0
    for r in range(rows):
        base = r * (cols + 1)
        for c in range(cols):
            bits = (bits << 1) | (1 if px[base + c] < px[base + c + 1] else 0)
    return bits


def dhash(path: Path, hash_size: int = 8) -> int:
    """dHash 64-bit idêntico ao dhash.py/score.py da v6."""
    with Image.open(path) as im:
        gray = im.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
    return _dhash_bits(gray.tobytes())


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def compute_features(path: Path) -> Feature | None:
    """Features completas — algoritmo idêntico ao score.py da v6."""
    try:
        with Image.open(path) as im:
            w, h = im.size  # tamanho real ANTES do draft (draft reduz im.size)
            im.draft("RGB", (512, 512))
            rgb = im.convert("RGB")
            small = rgb.resize((64, 64), Image.BOX)
            gray = rgb.convert("L").resize((9, 8), Image.LANCZOS)
    except Exception:
        return None

    sp = small.tobytes()  # 64*64*3 bytes RGB
    n = float(len(sp) // 3) or 1.0
    r_sum = g_sum = b_sum = 0.0
    lumas: list[float] = []
    sat_sum = 0.0
    hue_x = hue_y = 0.0

    for i in range(0, len(sp), 3):
        r, g, b = sp[i], sp[i + 1], sp[i + 2]
        r_sum += r
        g_sum += g
        b_sum += b
        lumas.append(0.299 * r + 0.587 * g + 0.114 * b)
        hh, ss, _vv = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        sat_sum += ss
        ang = hh * 2.0 * math.pi
        hue_x += math.cos(ang) * ss
        hue_y += math.sin(ang) * ss

    luma = sum(lumas) / n
    luma_std = math.sqrt(sum((v - luma) ** 2 for v in lumas) / n)
    warmth = (r_sum - b_sum) / n
    sat_mean = sat_sum / n
    hue = 0.0
    if math.hypot(hue_x, hue_y) >= 1e-9 and sat_mean >= 0.02:
        hue = (math.degrees(math.atan2(hue_y, hue_x)) + 360.0) % 360.0

    # histograma RGB 4x4x4 (2 bits por canal) — 64 bins uint16 LE
    hist = [0] * 64
    for i in range(0, len(sp), 3):
        r, g, b = sp[i] >> 6, sp[i + 1] >> 6, sp[i + 2] >> 6
        hist[(r << 4) | (g << 2) | b] += 1
    hist_b64 = base64.b64encode(struct.pack("<64H", *hist)).decode("ascii")

    d = _dhash_bits(gray.tobytes())

    return Feature(
        name=path.name,
        w=w,
        h=h,
        luma=round(luma, 3),
        luma_std=round(luma_std, 3),
        warmth=round(warmth, 3),
        hue=round(hue, 3),
        sat=round(sat_mean, 4),
        dhash=d,
        hist_b64=hist_b64,
    )


def hist_from_b64(hist_b64: str) -> list[float]:
    """Decodifica hist_b64 -> probabilidades normalizadas (soma=1)."""
    try:
        raw = base64.b64decode(hist_b64)
        vals = list(struct.unpack("<64H", raw[:128]))
    except Exception:
        return [1.0 / 64.0] * 64
    total = sum(vals)
    if total <= 0:
        return [1.0 / 64.0] * 64
    return [v / total for v in vals]


# ---------------------------------------------------------------------------
# Cache de tela (-screen)
# ---------------------------------------------------------------------------

def screen_cache_path(master: Path, images_dir: Path) -> Path:
    return images_dir / f"{master.stem}-screen{master.suffix}"


def needs_screen_cache(master: Path, target: tuple[int, int]) -> bool:
    """True se o master é maior que o alvo (precisa de cache)."""
    try:
        p = probe(master)
    except ImageError:
        return False
    return p.w > target[0] or p.h > target[1]


def screen_cache_valid(master: Path, cache: Path, target: tuple[int, int]) -> bool:
    """Cache existe, dimensões exatas e mais novo que o master."""
    if not cache.is_file():
        return False
    try:
        pm = probe(master)
        pc = probe(cache)
    except ImageError:
        return False
    if (pc.w, pc.h) != target:
        return False
    try:
        return cache.stat().st_mtime >= master.stat().st_mtime
    except OSError:
        return False


def build_screen_cache(master: Path, cache: Path, target: tuple[int, int]) -> bool:
    """Gera cache crop central WxH via Pillow. Retorna sucesso."""
    try:
        with Image.open(master) as im:
            im = im.convert("RGB")
            w, h = target
            # resize para cobrir o alvo mantendo aspecto, depois crop central
            scale = max(w / im.width, h / im.height)
            nw, nh = math.ceil(im.width * scale), math.ceil(im.height * scale)
            im = im.resize((nw, nh), Image.LANCZOS)
            left = (nw - w) // 2
            top = (nh - h) // 2
            im = im.crop((left, top, left + w, top + h))
            cache.parent.mkdir(parents=True, exist_ok=True)
            im.save(cache, quality=92)
        return True
    except Exception:
        return False


def resolve_screen_path(
    master: Path, images_dir: Path, target: tuple[int, int]
) -> tuple[Path, bool]:
    """Retorna (caminho_a_aplicar, cache_foi_regenerado).

    - master não precisa de cache -> master
    - cache válido -> cache
    - senão regenera; em falha -> master
    """
    if not needs_screen_cache(master, target):
        return master, False
    cache = screen_cache_path(master, images_dir)
    if screen_cache_valid(master, cache, target):
        return cache, False
    if build_screen_cache(master, cache, target):
        return cache, True
    return master, False
