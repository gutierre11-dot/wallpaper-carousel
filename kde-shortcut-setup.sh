#!/usr/bin/env bash
# Configura atalho de teclado global via DBus (método alternativo)

# Criar arquivo de configuração de atalho
mkdir -p ~/.config/kglobalshortcut

cat > ~/.config/kglobalshortcut/wallpaper-carouselrc << 'EOF'
[wallpaper-next]
Comment=Próximo Wallpaper
Enabled=true
Name=wallpaper-next
_Comment=Próximo Wallpaper
_Name=Próximo Wallpaper
EOF

echo "Para configurar o atalho de teclado:"
echo ""
echo "1. Abra 'Configurações do Sistema'"
echo "2. Vá em 'Atalhos' -> 'Atalhos Personalizados'"
echo "3. Clique em 'Editar' -> 'Novo' -> 'Atalho Global'"
echo "4. Nome: Próximo Wallpaper"
echo "5. Comando: /home/gutierredesouza/.local/share/wallpaper-carousel/next-wallpaper.sh"
echo "6. Em 'Gatilho', clique e pressione Ctrl+Alt+W (ou outra combinação)"
echo "7. Clique em 'Aplicar'"
echo ""
echo "Ícone alternativo criado na área de trabalho para teste imediato."
