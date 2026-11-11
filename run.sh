#!/usr/bin/env bash
#
# run.sh — wrapper usado pelos timers do systemd (usuário).
# Garante PATH completo, trava de concorrência (flock) e logging do ambiente.
#
set -uo pipefail

SELF_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
MAIN="$SELF_DIR/wallpaper-carousel.sh"
LOCK="$SELF_DIR/state/.run.lock"
LOG="$SELF_DIR/logs/carousel.log"

mkdir -p "$SELF_DIR/state" "$SELF_DIR/logs"

# PATH completo para achar plasma-apply-wallpaperimage, jq, curl, file, flock etc.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$HOME/.local/bin:$PATH"

# Garante HOME correto (systemd pode injetar %U=UID em vez do nome; derivamos do passwd).
REAL_HOME="$(getent passwd "$(id -u)" | cut -d: -f6)"
[[ -n "$REAL_HOME" && -d "$REAL_HOME" ]] && export HOME="$REAL_HOME"
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$HOME/.local/bin:$PATH"

# Descobre o ambiente gráfico caso o systemd não tenha propagado (segurança extra)
if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" && -S "/run/user/$(id -u)/bus" ]]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
fi
if [[ -z "${XDG_RUNTIME_DIR:-}" ]]; then
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
fi
if [[ -z "${WAYLAND_DISPLAY:-}" && -e "$XDG_RUNTIME_DIR/wayland-0" ]]; then
    export WAYLAND_DISPLAY="wayland-0"
fi

# Trava: só uma execução por vez (evita sobreposição download/rotate)
exec 9>"$LOCK"
if ! flock -n 9; then
    printf '%s [AVISO] Outra execução em andamento; abortando (%s).\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "${1:-?}" >> "$LOG"
    exit 0
fi

exec bash "$MAIN" "$@"
