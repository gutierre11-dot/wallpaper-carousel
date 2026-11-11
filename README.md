# Wallpaper Carousel (v5) — Carrossel premium de wallpapers com seleção inteligente

Carrossel de wallpapers **curado** para **Fedora + KDE Plasma 6 (Wayland)**. Baixa imagens de
quatro fontes (**Wallhaven**, **Reddit r/wallpapers**, **Bing Imagem do Dia** e **Lorem Picsum**),
valida qualidade real e escolhe a próxima imagem por **score** — não mais por índice alfabético.
Troca a cada **60 minutos** via **systemd user timer** (sem daemon em loop).

## O que mudou na v5

Adicionada **Lorem Picsum** como quarta fonte (fotografia aleatória de alta qualidade).
Aumentado limite de imagens de 40 para 500 para suportar mais diversidade de fontes.
Lorem Picsum usa API pública sem chave e oferece fotos aleatórias em 2560x1440.
Aumentados limites de download por fonte para preencher o acervo mais rápido.

## O que mudou na v4

A v3 escolhia bem a imagem isolada, mas errava a **sequência**: `SRC_REPEAT_PENALTY = -2.5`
tornava qualquer troca de fonte mais barata que repetir a fonte, então o carrossel caía num
**A-B-A-B** rígido (Wallhaven, Bing, Wallhaven, Bing…). Medido em 60 picks: 53% Wallhaven /
47% Bing, com o Bing a cada duas trocas. Além disso o alvo de luma diurno era 150, o que
premiava só o que era muito claro, e "nunca vista" valia +2,0 — quase todo o peso do contexto.

- **Cota justa de fonte** em vez de alternância: cada fonte tem uma fatia esperada
  (peso × nº de imagens no acervo) e só o **excesso na janela recente** é penalizado.
- **Contexto diurno 120** (era 150): o alvo agora fica perto do acervo, não no topo do brilho.
- **Frescor 1,0 / 24h** (era 2,0 / 48h): ordena desempate, não domina a escolha.
- **Impacto visual**: `luma_std` já estava no cache de features e não entrava no score.
- **Diversidade de gênero**: a média de similaridade com as últimas 6 exibições pega "outra
  foto do mesmo tipo", coisa que a anti-série (máximo) não via.
- **Gosto aprendido**: a partir de 2 likes, um centróide normalizado generaliza o gosto para
  imagens parecidas que você nunca viu (antes o like só protegia aquela imagem).
- **`why`/`--explain`** mostra as colunas novas: `imp`, `taste`, `gen`.

Medido no acervo atual (`score.py eval --picks 60`, início 10:00):

| Métrica | alfabético | v4 |
|---|---|---|
| Pares consecutivos similares (>60) | 8,5% | 3,4% |
| Desvio da fatia de fonte | 23,8 pp | 8,8 pp |
| Fatia do Bing | 46,7% | 31,7% |
| Picks em desacordo dia/noite | 46,7% | 8,3% |
| Imagens distintas em 60 picks | 40 | 36 |

## Compatibilidade (calibrado para esta máquina)

- **SO/DE:** Fedora 44, KDE Plasma 6, sessão Wayland.
- **Tela:** 1366x768 (16:9) → baixa em **≥2560x1440** e o Plasma reduz.
- **Conteúdo:** somente **SFW**; categoria **general** por padrão.

## Como o score decide

Para cada imagem elegível (block nunca entra), somam-se componentes:

| Componente | Peso | Regra |
|---|---|---|
| like | até +6 | +3 por like (máx. 2 likes contam) |
| contexto | até +7 (noite) / +3 (dia) | gaussiana em torno de **luma 45** (noite, 19h–6h) ou **120** (dia) |
| ctx_extra | −3 / −1 | degrau duro: clara à noite (luma > 110) ou escura de dia (luma < 55) |
| accent | +0,6 | hue da imagem a ≤25° do `AccentColor` do `~/.config/kdeglobals` |
| frescor | até +1 | `min(1, horas_desde_exibição/24)`; nunca exibida = +1 |
| impacto | até +0,8 | faixa tonal (`luma_std`) de 30 a 55; abaixo de 25 leva −0,5 |
| taste | até +1,5 | proximidade do centróide dos likes (só com ≥2 likes) |
| anti-série | até −8 | `−4 · similar/100` com a mais parecida das últimas 6; `−3` extra se `similar > 60` e `−2` se `> 75` |
| genre | até −1,5 | `−0,06 · (média de similaridade − 45)`, só quando a média passa de 45 |
| fonte | até −4 | `−0,8` se repete a fonte do último pick; menos `8 · excesso` sobre a cota esperada |
| jitter | 0–0,35 | desempate determinístico (`SCORE_SEED`, padrão fixo) |

`similar(a,b)` é 0–100: `55%` dHash (Hamming/64) + `35%` histograma RGB 4×4×4 (L1
normalizada) + `10%` diferença de luma. Imagens idênticas dão 100; séries do mesmo artista
ficam altas e são evitadas.

`block` é exclusão: a imagem nunca é escolhida até `unblock`. O `like` também protege a
imagem do prune.

**Limite conhecido:** à noite o acervo escuro é um único agrupamento parecido. Quando a
anti-série barra a repetição, sobra pouca imagem escura e o score prefere repetir um par
similar a escolher uma imagem clara. A saída é de **acervo**, não de score: rodar o download
em horários noturnos com temas escuros aumenta a variedade disponível.

## Uso

```
wallpaper-carousel download         # baixa novos wallpapers (Wallhaven + Reddit + Bing + Picsum)
wallpaper-carousel rotate           # aplica a próxima imagem escolhida pelo score
wallpaper-carousel rotate --dry-run # mostra a escolha/breakdown sem aplicar nem registrar
wallpaper-carousel status           # status, top 3 do próximo pick, timers e log
wallpaper-carousel why              # breakdown do score (top 5)
wallpaper-carousel like [arquivo]   # like no wallpaper atual (ou no arquivo informado)
wallpaper-carousel block [arquivo]  # bloqueia a imagem para sempre
wallpaper-carousel unlike [arquivo] # remove um like
wallpaper-carousel unblock [arquivo] # desbloqueia
wallpaper-carousel eval             # compara seleção antiga (alfabética) vs nova (score)
wallpaper-carousel setup            # (re)instala/ativa os timers e aplica um wallpaper
wallpaper-carousel clean            # remove imagens e estado de seleção
wallpaper-carousel uninstall        # desativa/remove os timers (mantém as imagens)

## Menu de Contexto (KDE Plasma)

Clique com o botão direito na área de trabalho para acessar:

**Wallpaper** → **Próximo Wallpaper**

Pula imediatamente para o próximo wallpaper com um clique. Recarregue o cache se necessário:
```bash
kbuildsycoca6
```

## Atalho Global (Opcional)

Configure um atalho de teclado global em Configurações do Sistema → Atalhos → Atalhos Personalizados:
- Comando: `~/.local/share/wallpaper-carousel/next-wallpaper.sh`
- Gatilho: ex: `Ctrl+Alt+W`

Sem argumento, `like/block/unlike/unblock` usam o **wallpaper atual** (última entrada de
`state/history`). Também é possível usar o `score.py` direto:

```
python3 score.py pick --explain          # top 8 com componentes
python3 score.py score <arquivo>         # score detalhado de uma imagem
python3 score.py history --limit 10      # últimas exibições
python3 score.py simulate --picks 60 --policy new --start 2026-10-22T08:00
python3 score.py eval --picks 60         # tabela old vs new + critérios
python3 score.py selftest                # testes sintéticos (exit 0/1)
```

## Personalização (variáveis de ambiente)

```
WALLPAPER_RESOLUTION=2560x1440       # resolução mínima real
WALLPAPER_RATIOS=16x9
WALLPAPER_CATEGORIES=100             # 100=general, 010=anime, 110=general+anime
WALLPAPER_THEMES='nature|space|-'    # temas rotativos; '-' = rodada sem q (pool amplo)
WALLPAPER_PAGES=5                    # páginas por execução do Wallhaven
WALLPAPER_DOWNLOAD_LIMIT=15          # máx. novas do Wallhaven por execução
WALLPAPER_MIN_BPP=0.35               # bits/pixel mínimo (abaixo = upscale)
WALLPAPER_MAX_FILE_SIZE_KB=16384     # teto de sanidade; 0 = sem teto
WALLPAPER_DUP_HAMMING=4              # Hamming máx. p/ near-duplicata
WALLPAPER_REDDIT_ENABLED=1           # 0 desativa o Reddit
WALLPAPER_REDDIT_LIMIT=8             # máx. novas aceitas do Reddit
WALLPAPER_BING_ENABLED=1
WALLPAPER_BING_DAYS=8
WALLPAPER_BING_LIMIT=5
WALLPAPER_PREP_RESOLUTION=2560x1440  # cache -screen (downscale p/ aplicar leve)
WALLPAPER_PICSUM_ENABLED=1            # Lorem Picsum (fotos aleatórias)
WALLPAPER_PICSUM_LIMIT=10             # máx. novas do Picsum por execução
WALLPAPER_NOTIFY_ENABLED=0            # notificações quando o wallpaper muda (1=ativado)
SCORE_SEED=20261022                  # seed do jitter (determinismo)
```

Ajuste de **gosto** (lidas pelo `score.py`):

```
SCORE_SOURCE_WEIGHTS='wallhaven=1,reddit=1,bing=0.55,other=0.8'
SCORE_DAY_LUMA=120                   # luma alvo de dia (menor = prefere imagem mais escura)
SCORE_DAY_WEIGHT=3.0                 # força do contexto diurno
SCORE_DAY_SIGMA=45.0                 # tolerância em torno do alvo
SCORE_FRESH_WEIGHT=1.0               # bônus máximo por frescor
SCORE_FRESH_HOURS=24                 # horas até o frescor máximo
SCORE_SRC_OVER_PENALTY=8.0           # força da cota justa de fonte
```

Para puxar menos Bing sem bloquear imagem nenhuma, baixe o peso: `bing=0.3`. Para desligar a
cota justa e voltar a preferir só o score da imagem, `SCORE_SRC_OVER_PENALTY=0`.

`WALLPAPER_ROTATE_MINUTES` e `WALLPAPER_DOWNLOAD_HOURS` mudam os intervalos; depois rode
`wallpaper-carousel setup` para regravar os timers.

## Arquivos

- `wallpaper-carousel.sh` — CLI principal (configuração no topo).
- `score.py` — features, score, feedback, simulação e selftest (só Pillow).
- `dhash.py` — perceptual hash usado na deduplicação do download.
- `run.sh` — wrapper dos timers (PATH, HOME e trava `flock`).
- `images/` — wallpapers (masters + cache `-screen`).
- `state/` — `features.tsv`, `scores.tsv` (feedback), `history` (exibições), `sources.tsv`
  (origem de cada imagem), `source_idx`/`theme_idx` (rotação), `hashes.tsv`, `ids`, `accent`
  (opcional).
- `logs/` — `carousel.log`.

## Reddit: falha não é fatal

A API pública do Reddit pode responder **403** dependendo da rede/UA. Nesse caso o download
registra um **aviso** e segue com Wallhaven e Bing; nenhuma imagem é perdida. O parsing aceita
apenas links diretos `i.redd.it` / `i.imgur.com` com extensão de imagem (jpg/png/webp).

## Desinstalar

```
wallpaper-carousel uninstall   # remove timers
wallpaper-carousel clean       # (opcional) remove imagens e estado
```
