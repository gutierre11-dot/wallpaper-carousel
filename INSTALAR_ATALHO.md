# Atalho Global para Próximo Wallpaper

Para configurar um atalho de teclado global que pula para o próximo wallpaper:

## Método 1: Interface do KDE (Recomendado)

1. Abra as **Configurações do Sistema**
2. Vá em **Atalhos** → **Atalhos Personalizados**
3. Clique em **Editar** → **Novo** → **Atalho Global**
4. Nome: `Próximo Wallpaper`
5. Comentário: `Pula para o próximo wallpaper do carrossel`
6. Comando: `/home/gutierredesouza/.local/share/wallpaper-carousel/next-wallpaper.sh`
7. Em **Gatilho**, clique e pressione a combinação de teclas desejada (ex: `Ctrl+Alt+W`)
8. Clique em **Aplicar**

## Método 2: Menu de Contexto (Já Instalado)

O menu de contexto do KDE Plasma já foi configurado! Clique com o botão direito em qualquer lugar da área de trabalho e você verá:

**Wallpaper** →
- **Próximo Wallpaper** - Pula para o próximo wallpaper
- **Curtir Wallpaper Atual** - Adiciona like ao wallpaper atual
- **Bloquear Wallpaper Atual** - Bloqueia o wallpaper atual

## Recarregar o Menu de Contexto

Se o menu não aparecer imediatamente, recarregue o cache do KDE:

```bash
kbuildsycoca6
# ou para versões mais antigas:
kbuildsycoca5
```

## Testar

Teste o menu de contexto clicando com o botão direito na área de trabalho ou em uma pasta.
