#!/usr/bin/env bash
# Mostra notificação sobre o wallpaper atual
DATA_DIR="${WALLPAPER_DATA_DIR:-$HOME/.local/share/wallpaper-carousel}"
HISTORY_FILE="$DATA_DIR/state/history"

if [[ -f "$HISTORY_FILE" ]]; then
    last_line=$(tail -n 1 "$HISTORY_FILE")
    if [[ -n "$last_line" ]]; then
        timestamp=$(echo "$last_line" | cut -f1)
        filename=$(echo "$last_line" | cut -f2)
        source=$(echo "$filename" | cut -d'-' -f1)

        case "$source" in
            wallhaven) source_name="Wallhaven" ;;
            reddit) source_name="Reddit" ;;
            bing) source_name="Bing" ;;
            picsum) source_name="Lorem Picsum" ;;
            *) source_name="Outro" ;;
        esac

        notify-send "Wallpaper Carrossel" "Wallpaper atual: $filename\nFonte: $source_name" \
            --icon=preferences-desktop-wallpaper \
            --app-name=wallpaper-carousel \
            --expire-time=3000
    fi
fi
