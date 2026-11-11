#!/usr/bin/env bash
# Configura atalho global via DBus (Plasma 6)

# Verificar se o DBus do kglobalaccel está disponível
if ! qdbus org.kde.kglobalaccel /org/kde/kglobalaccel org.kde.kglobalaccel.Component.reconfigure >/dev/null 2>&1; then
    echo "Serviço kglobalaccel não disponível via DBus"
    echo "Use a interface gráfica:"
    echo "Configurações do Sistema -> Atalhos -> Atalhos Personalizados"
    exit 1
fi

# O arquivo kglobalshortcutrc já foi atualizado
# Precisamos recarregar a configuração
qdbus org.kde.kglobalaccel /org/kde/kglobalaccel org.kde.kglobalaccel.Component.reconfigure

echo "Configuração recarregada. Agora:"
echo ""
echo "1. Abra 'Configurações do Sistema'"
echo "2. Vá em 'Atalhos' -> 'Atalhos Personalizados'"
echo "3. Procure por 'Próximo Wallpaper' na lista"
echo "4. Clique nele e defina o gatilho (ex: Ctrl+Alt+W)"
echo "5. Clique em 'Aplicar'"
