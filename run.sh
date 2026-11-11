#!/usr/bin/env bash
#
# run.sh — wrapper usado pelos timers do systemd (usuário) e como entrypoint único.
# Garante PATH completo, trava de concorrência (flock), HOME/DBus/Wayland corretos
# e um help de erro claro. A interface é a mesma: run.sh <comando> [args...].
#
# v6: respeita WALLPAPER_DATA_DIR (lock/log no data dir correto), umask 022 e
#     mensagens de erro melhores. Nada aqui aplica wallpaper por conta própria.
#
set -uo pipefail

umask 022

SELF_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
PKG="$SELF_DIR/wallpaper_carousel"
MAIN="$SELF_DIR/wallpaper-carousel.sh"   # fallback legado (v6)
DATA_DIR="${WALLPAPER_DATA_DIR:-$SELF_DIR}"
export WALLPAPER_DATA_DIR="$DATA_DIR"
LOCK="$DATA_DIR/state/.run.lock"
LOG="$DATA_DIR/logs/carousel.log"

if [[ -d "$PKG" ]]; then
    ENTRY=(python3 -m wallpaper_carousel)
    export PYTHONPATH="$SELF_DIR${PYTHONPATH:+:$PYTHONPATH}"
elif [[ -f "$MAIN" ]]; then
    ENTRY=(bash "$MAIN")
else
    printf 'ERRO: pacote wallpaper_carousel não encontrado em %s\n' "$PKG" >&2
    exit 1
fi
if ! mkdir -p "$DATA_DIR/state" "$DATA_DIR/logs" "$DATA_DIR/images" 2>/dev/null; then
    printf 'ERRO: não foi possível criar %s (verifique permissões)\n' "$DATA_DIR" >&2
    exit 1
fi

# PATH completo para achar plasma-apply-wallpaperimage, jq, curl, file, flock etc.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$HOME/.local/bin:$PATH"

# Garante HOME correto (systemd pode injetar %U=UID em vez do nome; derivamos do passwd).
REAL_HOME="$(getent passwd "$(id -u)" | cut -d: -f6)"
if [[ -n "${REAL_HOME:-}" && -d "${REAL_HOME:-}" ]]; then
    export HOME="$REAL_HOME"
fi
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$HOME/.local/bin:$PATH"

# Descobre o ambiente gráfico caso o systemd não tenha propagado (segurança extra)
if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" && -S "/run/user/$(id -u)/bus" ]]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
fi
if [[ -z "${XDG_RUNTIME_DIR:-}" ]]; then
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
fi
if [[ -z "${WAYLAND_DISPLAY:-}" || ! -e "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ]]; then
    WAYLAND_DISPLAY="$(find "$XDG_RUNTIME_DIR" -maxdepth 1 -name 'wayland-*' ! -name '*.lock' -printf '%f\n' | sort -V | head -n1)"
    if [[ -n "$WAYLAND_DISPLAY" ]]; then
        export WAYLAND_DISPLAY
    fi
fi
if [[ -z "${DISPLAY:-}" && -d "$XDG_RUNTIME_DIR/.X11-unix" ]]; then
    DISPLAY=":$(ls "$XDG_RUNTIME_DIR/.X11-unix" 2>/dev/null | head -n1 | tr -d 'X')"
    [[ "$DISPLAY" =~ ^:[0-9]+$ ]] && export DISPLAY
fi

# Trava: só uma execução por vez (evita sobreposição download/rotate).
# Se não for possível criar o lock, seguimos sem travar em vez de falhar no timer.
if exec 9>"$LOCK" 2>/dev/null; then
    if ! flock -n 9; then
        printf '%s [AVISO] Outra execução em andamento; abortando (%s).\n' \
            "$(date '+%Y-%m-%d %H:%M:%S')" "${1:-?}" >> "$LOG" 2>/dev/null
        exit 0
    fi
    export WALLPAPER_FLOCK_HELD=1
else
    printf '%s [AVISO] Sem permissão para criar %s; seguindo sem flock.\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$LOCK" >&2
fi

exec "${ENTRY[@]}" "$@"
