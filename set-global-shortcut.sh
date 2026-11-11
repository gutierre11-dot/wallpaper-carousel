#!/usr/bin/env bash
# Configura atalho global via kwriteconfig5 (métode alternativo)

SERVICE_NAME="wallpaper-next"
COMMAND="/home/gutierredesouza/.local/share/wallpaper-carousel/next-wallpaper.sh"
SHORTCUT="Ctrl+Alt+W"

# Tentar configurar via kwriteconfig5
if command -v kwriteconfig5 >/dev/null 2>&1; then
    kwriteconfig5 --file ~/.config/kglobalshortcutsrc \
        --group "$SERVICE_NAME" \
        --key "_k_friendly_name" \
        "Próximo Wallpaper"

    kwriteconfig5 --file ~/.config/kglobalshortcutsrc \
        --group "$SERVICE_NAME" \
        --key "Comment" \
        "Pula para o próximo wallpaper"

    kwriteconfig5 --file ~/.config/kglobalshortcutsrc \
        --group "$SERVICE_NAME" \
        --key "Enabled" \
        "true"

    kwriteconfig5 --file ~/.config/kglobalshortcutsrc \
        --group "$SERVICE_NAME" \
        --key "Exec" \
        "$COMMAND"

    kwriteconfig5 --file ~/.config/kglobalshortcutsrc \
        --group "$SERVICE_NAME" \
        --key "Name" \
        "$SERVICE_NAME"

    echo "Atalho configurado em ~/.config/kglobalshortcutsrc"
    echo "Recarregue o Plasma ou execute: kquitapp6 plasmashell && kstart6 plasmashell"
else
    echo "kwriteconfig5 não encontrado. Use a interface gráfica:"
    echo "Configurações do Sistema -> Atalhos -> Atalhos Personalizados"
fi
