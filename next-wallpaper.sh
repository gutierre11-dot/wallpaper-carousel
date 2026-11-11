#!/usr/bin/env bash
#
# next-wallpaper.sh — atalho de conveniência para "próximo wallpaper".
#
# Wrapper fino: SEMPRE executa o entrypoint oficial run.sh (que aplica PATH,
# HOME, DBUS, WAYLAND, umask e a trava flock). Nada de caminho hardcoded e nada
# de chamar wallpaper-carousel.sh direto.
#
# Uso:
#   ./next-wallpaper.sh            # pula para o próximo wallpaper
#   ./next-wallpaper.sh --dry-run  # mostra qual seria o próximo (não aplica)
#
# Código de saída: o mesmo de run.sh (0 = ok; != 0 = falha real).
set -euo pipefail

SELF_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
RUN="$SELF_DIR/run.sh"

if [[ ! -f "$RUN" ]]; then
    printf 'ERRO: %s não encontrado (instalação incompleta?)\n' "$RUN" >&2
    exit 1
fi
if [[ ! -x "$RUN" ]]; then
    printf 'ERRO: %s não é executável (chmod +x %s)\n' "$RUN" "$RUN" >&2
    exit 1
fi

# Repassa argumentos extras (ex.: --dry-run) para "rotate".
exec "$RUN" rotate "$@"
