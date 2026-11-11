#!/usr/bin/env python3
"""Perceptual hash (dHash) de imagens usando apenas Pillow (sem numpy/scipy).

Modos:
  dhash.py <arquivo>                     -> imprime o hash como inteiro (64 bits, decimal)
  dhash.py --min-dist <arquivo> <tsv>    -> imprime a menor distância de Hamming entre
                                            o hash do arquivo e todos os hashes no <tsv>
                                            (formato: "nome<TAB>hash" por linha).
                                            64 se o <tsv> não existir ou estiver vazio.
"""

import sys
from PIL import Image


def dhash(path, hash_size=8):
    im = Image.open(path).convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
    px = im.tobytes()  # 1 byte por pixel em modo "L"
    bits = 0
    for r in range(hash_size):
        base = r * (hash_size + 1)
        for c in range(hash_size):
            bits = (bits << 1) | (1 if px[base + c] < px[base + c + 1] else 0)
    return bits


def hamming(a, b):
    return bin(a ^ b).count("1")


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(2)

    if args[0] == "--min-dist" and len(args) >= 3:
        fname, tsv = args[1], args[2]
        try:
            h = dhash(fname)
        except Exception as e:  # arquivo ilegível/corrompido
            print("64")
            return
        best = 64
        try:
            with open(tsv) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    # hash é o último campo (nomes podem conter tab, então rsplit)
                    try:
                        h2 = int(line.rsplit("\t", 1)[-1])
                    except (ValueError, IndexError):
                        continue
                    d = hamming(h, h2)
                    if d < best:
                        best = d
                        if best == 0:
                            break
        except FileNotFoundError:
            best = 64
        print(best)
        return

    # modo hash de um arquivo
    try:
        print(dhash(args[0]))
    except Exception as e:
        print(f"ERR:{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
