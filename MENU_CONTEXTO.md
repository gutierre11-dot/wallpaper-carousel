# Menu de Contexto para Wallpaper Carrossel

O menu de contexto do KDE Plasma foi configurado para facilitar o controle do carrossel de wallpapers diretamente da área de trabalho.

## Funcionalidades Adicionadas

### 1. Menu de Contexto (Clique Direito)

Clique com o botão direito em qualquer lugar da área de trabalho ou em uma pasta e você verá:

**Wallpaper** →
- **Próximo Wallpaper** - Pula imediatamente para o próximo wallpaper
- **Curtir Wallpaper Atual** - Adiciona like ao wallpaper atual (aumenta prioridade)
- **Bloquear Wallpaper Atual** - Bloqueia o wallpaper atual (nunca mais aparece)

### 2. Atalho Global (Opcional)

Você pode configurar um atalho de teclado global para pular wallpapers:

1. Abra **Configurações do Sistema** → **Atalhos** → **Atalhos Personalizados**
2. Clique em **Editar** → **Novo** → **Atalho Global**
3. Nome: `Próximo Wallpaper`
4. Comando: `/home/gutierredesouza/.local/share/wallpaper-carousel/next-wallpaper.sh`
5. Defina o gatilho (ex: `Ctrl+Alt+W`)

### 3. Notificações (Opcional)

Para ativar notificações quando o wallpaper muda:

```bash
export WALLPAPER_NOTIFY_ENABLED=1
~/.local/share/wallpaper-carousel/wallpaper-carousel.sh rotate
```

Ou adicione ao seu `~/.bashrc`:
```bash
export WALLPAPER_NOTIFY_ENABLED=1
```

## Arquivos Criados

- `~/.local/share/kservices5/ServiceMenus/wallpaper-next.desktop` - Configuração do menu de contexto
- `~/.local/share/wallpaper-carousel/next-wallpaper.sh` - Script para atalho global
- `~/.local/share/wallpaper-carousel/notify-wallpaper.sh` - Script de notificação manual

## Recarregar o Menu

Se o menu não aparecer imediatamente, recarregue o cache do KDE:

```bash
kbuildsycoca6
```

## Teste

1. Clique com o botão direito na área de trabalho
2. Procure por "Wallpaper" no menu
3. Clique em "Próximo Wallpaper" para testar
