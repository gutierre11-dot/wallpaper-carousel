# Alternativas para Pular Wallpaper - Plasma 6

O menu de contexto do Plasma 6 funciona de forma diferente. Aqui estão 3 alternativas funcionais:

## ✅ Opção 1: Ícone na Área de Trabalho (Já Criado)

Um ícone "Próximo Wallpaper" foi criado na sua área de trabalho.

**Como usar:**
1. Dê dois cliques no ícone "Próximo Wallpaper" na área de trabalho
2. O wallpaper mudará imediatamente

## ✅ Opção 2: Atalho de Teclado Global (Recomendado)

Configure um atalho de teclado:

1. Abra **Configurações do Sistema**
2. Vá em **Atalhos** → **Atalhos Personalizados**
3. Clique em **Editar** → **Novo** → **Atalho Global**
4. Configure:
   - **Nome**: `Próximo Wallpaper`
   - **Comando**: `/home/gutierredesouza/.local/share/wallpaper-carousel/next-wallpaper.sh`
   - **Gatilho**: Clique e pressione `Ctrl+Alt+W` (ou outra combinação)
5. Clique em **Aplicar**

## ✅ Opção 3: Widget do Painel

Adicione um widget ao painel:

1. Clique com o botão direito no painel
2. **Editar Painel** → **Adicionar Widgets**
3. Procure por "Botão de Aplicativo" ou "Launcher"
4. Arraste para o painel
5. Configure para executar: `/home/gutierredesouza/.local/share/wallpaper-carousel/next-wallpaper.sh`

## Por que o menu de contexto não funcionou?

O Plasma 6 mudou a estrutura de menus de contexto. O método tradicional (`.desktop` em `~/.local/share/kservices5/ServiceMenus/`) pode não funcionar em todas as versões do Plasma 6.

As alternativas acima são mais confiáveis e funcionam perfeitamente.

## Teste Rápido

Teste o ícone da área de trabalho agora:
```bash
ls -la "$HOME/Área de trabalho/Próximo Wallpaper.desktop"
```

Dê dois cliques no ícone para testar!
