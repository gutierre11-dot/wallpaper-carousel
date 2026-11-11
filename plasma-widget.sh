#!/usr/bin/env bash
# Script para criar um widget/menu rápido no Plasma 6
# Este script cria um atalho na área de trabalho ou no painel

DATA_DIR="${WALLPAPER_DATA_DIR:-$HOME/.local/share/wallpaper-carousel}"
SCRIPT_DIR="$DATA_DIR"

# Criar arquivo .desktop na área de trabalho
cat > "$HOME/Área de trabalho/Próximo Wallpaper.desktop" << 'EOF'
[Desktop Entry]
Version=1.0
Type=Application
Name=Próximo Wallpaper
Name[pt_BR]=Próximo Wallpaper
Comment=Pula para o próximo wallpaper
Comment[pt_BR]=Pula para o próximo wallpaper
Exec=/home/gutierredesouza/.local/share/wallpaper-carousel/wallpaper-carousel.sh rotate
Icon=preferences-desktop-wallpaper
Terminal=false
Categories=Utility;
EOF

chmod +x "$HOME/Área de trabalho/Próximo Wallpaper.desktop"

echo "Ícone criado na área de trabalho: Próximo Wallpaper"
echo "Dê dois cliques para pular para o próximo wallpaper"
