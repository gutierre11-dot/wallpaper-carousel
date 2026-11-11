#!/usr/bin/env python3
"""Perceptual hash (dHash) de imagens — v6.

Usa apenas Pillow (sem numpy/scipy). O hash é um inteiro de 64 bits (decimal)
calculado a partir das diferenças de luminância entre pixels vizinhos, o que
torna o dHash robusto a redimensionamento/compressão e adequado à
deduplicação de near-duplicatas do carrossel.

Modos:
  dhash.py <arquivo>                     -> imprime o hash como inteiro (64 bits, decimal)
  dhash.py --min-dist <arquivo> <tsv>    -> imprime a menor distância de Hamming entre
                                            o hash do arquivo e todos os hashes do <tsv>
                                            (formato: "nome<TAB>hash" por linha).
                                            64 se o <tsv> não existir, estiver vazio ou
                                            não tiver nenhuma linha válida.

Robustez (v6): linhas inválidas, vazias, comentários (#) e campos extras são
ignorados; o hash é sempre o último campo da linha (rsplit), então nomes de
arquivo com TAB também funcionam.
"""

import sys
from PIL import Image


def dhash(path, hash_size=8):
    """Calcula o dHash de `path` com `hash_size` bits por linha (64 bits no total)."""
    im = Image.open(path).convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
    px = im.tobytes()  # 1 byte por pixel em modo "L"
    bits = 0
    for r in range(hash_size):
        base = r * (hash_size + 1)
        for c in range(hash_size):
            bits = (bits << 1) | (1 if px[base + c] < px[base + c + 1] else 0)
    return bits


def hamming(a, b):
    """Distância de Hamming entre dois hashes inteiros."""
    return bin(a ^ b).count("1")


def parse_hash_line(line):
    """Extrai o hash (último campo TAB) de uma linha do TSV; None se inválida."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split("\t")
    if len(parts) < 2:
        return None
    try:
        return int(parts[-1])
    except (ValueError, IndexError):
        return None


def min_distance(fname, tsv):
    """Menor distância de Hamming entre o dHash de `fname` e os hashes do TSV."""
    try:
        h = dhash(fname)
    except Exception:  # arquivo ilegível/corrompido: tratado como "sem par"
        return 64
    best = 64
    try:
        with open(tsv, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                h2 = parse_hash_line(line)
                if h2 is None:
                    continue
                d = hamming(h, h2)
                if d < best:
                    best = d
                    if best == 0:
                        break
    except (FileNotFoundError, OSError):
        return 64
    return best


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(2)

    if args[0] == "--min-dist" and len(args) >= 3:
        print(min_distance(args[1], args[2]))
        return

    # modo hash de um arquivo
    try:
        print(dhash(args[0]))
    except Exception as e:  # arquivo ilegível/corrompido
        print(f"ERR:{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
