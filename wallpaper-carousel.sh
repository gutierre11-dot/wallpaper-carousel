#!/usr/bin/env bash
#
# wallpaper-carousel.sh — Carrossel premium de wallpapers (KDE Plasma / Wayland)
#
# v5 (fontes expandidas):
#   * Nova fonte: Lorem Picsum (fotografia aleatória de alta qualidade, 2560x1440).
#   * Limite aumentado: 500 imagens (era 40) para suportar mais diversidade.
#   * Integração ao score: peso 0.75 para picsum (entre Bing 0.55 e Wallhaven 1.0).
#   * Limites de download aumentados para preencher o acervo mais rápido.
#
# v4 (seleção por sequência):
#   * Cota justa de fonte no lugar da alternância A-B-A-B: cada fonte tem fatia
#     esperada (peso × tamanho do acervo) e só o excesso recente é penalizado.
#   * Contexto diurno realinhado (luma 120), frescor menor (1,0/24h), impacto
#     visual (faixa tonal) e diversidade de gênero (média de similaridade).
#   * Gosto aprendido: 2+ likes viram centróide e generalizam para imagens novas.
#
# v3 (seleção inteligente):
#   * Seleção por score (score.py): like/block, contexto dia/noite, accent do
#     KDE, frescor, anti-série (últimas 6 exibições) e jitter determinístico.
#   * Diversificação de fontes: Wallhaven com estratégia rotativa + Reddit
#     r/wallpapers + Bing; toda imagem passa pelo mesmo accept_image.
#   * Feedback persistente: like/block/unlike/unblock/why/eval.
#   * Qualidade recalibrada: bpp >= 0.35 e teto de 16 MB (4K reais passam).
#
# v2 (premium):
#   * Curadoria: Wallhaven "toplist" (topRange) + temas rotativos por tags.
#   * Qualidade real: resolução mínima (atleast) + validação de dimensões reais
#     e de bits/pixel (descarta upscales e imagens de baixa qualidade).
#   * Mais fontes: Wallhaven + Bing Imagem do Dia (UHD).
#   * Deduplicação: perceptual hash (dHash) via Pillow — evita near-duplicatas.
#   * Leveza: cache "-screen" downscaled para aplicar rápido na iGPU.
#   * Aplicação nativa (plasma-apply-wallpaperimage) com fallback D-Bus.
#
# Requisitos: bash, curl, jq, file, ImageMagick (magick), python3 + Pillow,
#             systemd (--user), plasma-apply-wallpaperimage, dbus-send.
#
set -uo pipefail

# ===================== CONFIGURAÇÃO (ajuste se quiser) =====================
DATA_DIR="${WALLPAPER_DATA_DIR:-$HOME/.local/share/wallpaper-carousel}"
IMG_DIR="$DATA_DIR/images"
STATE_DIR="$DATA_DIR/state"
LOG_DIR="$DATA_DIR/logs"
LOG_FILE="$LOG_DIR/carousel.log"
INDEX_FILE="$STATE_DIR/index"
HASH_FILE="$STATE_DIR/hashes.tsv"
IDS_FILE="$STATE_DIR/ids"
THEME_IDX_FILE="$STATE_DIR/theme_idx"
SOURCE_IDX_FILE="$STATE_DIR/source_idx"
HISTORY_FILE="$STATE_DIR/history"
SOURCES_FILE="$STATE_DIR/sources.tsv"
SCORES_FILE="$STATE_DIR/scores.tsv"
DHASH="$DATA_DIR/dhash.py"
SCORE="$DATA_DIR/score.py"

ROTATE_MINUTES="${WALLPAPER_ROTATE_MINUTES:-60}"      # intervalo do carrossel (min)
DOWNLOAD_EVERY_HOURS="${WALLPAPER_DOWNLOAD_HOURS:-6}" # renova coleção a cada N h
MAX_IMAGES="${WALLPAPER_MAX_IMAGES:-500}"             # máx. de imagens mantidas

# --- Curadoria (Wallhaven) ---
# A estratégia (toplist 1w/1M/6M, date_added, rodada sem q) roda em state/source_idx.
ATLEAST="${WALLPAPER_RESOLUTION:-2560x1440}" # resolução mínima REAL (WxH)
RATIOS="${WALLPAPER_RATIOS:-16x9}"
CATEGORIES="${WALLPAPER_CATEGORIES:-100}"    # 100=general, 010=anime, 110=general+anime
PURITY="${WALLPAPER_PURITY:-100}"            # 100=SFW
PAGES_PER_RUN="${WALLPAPER_PAGES:-5}"        # páginas da API por execução
DOWNLOAD_LIMIT="${WALLPAPER_DOWNLOAD_LIMIT:-15}" # máx. novas do Wallhaven por execução
API_SLEEP="${WALLPAPER_API_SLEEP:-2}"        # pausa entre chamadas (gentileza)

# --- Temas rotativos (curadoria + variedade); use '|' para separar ---
# Tema vazio = rodada sem 'q' (pool amplo). Use '-' ou vazio para isso.
IFS='|' read -r -a THEMES <<< "${WALLPAPER_THEMES:-nature landscape|space nebula stars|city night skyline|mountain snow peaks|ocean sea waves|minimal abstract|forest fog trees|architecture building|sunset dusk golden|desert dunes|aurora polar|cherry blossom japan|-}"

# --- Reddit r/wallpapers ---
REDDIT_ENABLED="${WALLPAPER_REDDIT_ENABLED:-1}"
REDDIT_LIMIT="${WALLPAPER_REDDIT_LIMIT:-8}"  # máx. novas aceitas por execução

# --- Qualidade ---
MIN_BPP="${WALLPAPER_MIN_BPP:-0.35}"          # bits/pixel mínimo (descarta upscale)
MAX_FILE_SIZE_KB="${WALLPAPER_MAX_FILE_SIZE_KB:-16384}" # teto de sanidade (não de qualidade)
if [[ "$MAX_FILE_SIZE_KB" == "0" ]]; then MAX_FILE_SIZE_BYTES=-1; else MAX_FILE_SIZE_BYTES=$(( MAX_FILE_SIZE_KB * 1024 )); fi

# --- Dedup ---
DUP_HAMMING="${WALLPAPER_DUP_HAMMING:-4}"    # distância de Hamming máxima (dHash)

# --- Cache de tela (downscale p/ aplicar leve na iGPU) ---
PREP_RESOLUTION="${WALLPAPER_PREP_RESOLUTION:-2560x1440}"

# --- Bing Imagem do Dia ---
BING_ENABLED="${WALLPAPER_BING_ENABLED:-1}"
BING_DAYS="${WALLPAPER_BING_DAYS:-8}"
BING_LIMIT="${WALLPAPER_BING_LIMIT:-5}"

# --- Lorem Picsum (API pública sem chave) ---
PICSUM_ENABLED="${WALLPAPER_PICSUM_ENABLED:-1}"
PICSUM_LIMIT="${WALLPAPER_PICSUM_LIMIT:-10}"

# --- Notificações ---
NOTIFY_ENABLED="${WALLPAPER_NOTIFY_ENABLED:-0}"

USER_AGENT="wallpaper-carousel/5.0 (Fedora KDE personal script; +https://wallhaven.cc)"
TIMER_ROTATE="wallpaper-carousel-rotate.timer"
TIMER_DOWNLOAD="wallpaper-carousel-download.timer"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# ==========================================================================

mkdir -p "$IMG_DIR" "$STATE_DIR" "$LOG_DIR"

log() { local lvl="$1"; shift; printf '%s [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$lvl" "$*" | tee -a "$LOG_FILE"; }
die() { log "ERRO" "$*"; exit 1; }

require_tools() {
    local miss=() t
    for t in curl jq file magick python3; do command -v "$t" >/dev/null 2>&1 || miss+=("$t"); done
    ((${#miss[@]})) && die "Faltam: ${miss[*]} (instale: sudo dnf install ${miss[*]})"
    [[ -f "$DHASH" ]] || die "Falta $DHASH (helper de perceptual hash)"
    python3 -c 'import PIL' 2>/dev/null || die "Falta Pillow (sudo dnf install python3-pillow)"
    return 0
}

list_masters() {
    find "$IMG_DIR" -maxdepth 1 -type f \
        \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' \) \
        ! -iname '*-screen.*' 2>/dev/null
}
count_images() { list_masters | wc -l | tr -d ' '; }

img_bpp() {
    local w h b
    read -r w h < <(magick identify -ping -format '%w %h\n' "$1" 2>/dev/null) || { echo 0; return; }
    b=$(stat -c%s "$1" 2>/dev/null || echo 0)
    awk -v w="$w" -v h="$h" -v b="$b" 'BEGIN{ if (w*h>0) printf "%.3f", (b*8)/(w*h); else printf "0" }'
}

# Imprime "OK<TAB>" ou "NO<TAB>motivo"
check_quality() {
    local f="$1" w h b
    read -r w h < <(magick identify -ping -format '%w %h\n' "$f" 2>/dev/null) || { printf 'NO\tdimensões ilegíveis\n'; return; }
    b=$(stat -c%s "$f" 2>/dev/null || echo 0)
    awk -v w="$w" -v h="$h" -v b="$b" \
        -v aw="${ATLEAST%%x*}" -v ah="${ATLEAST##*x}" \
        -v mb="$MIN_BPP" -v mxb="$MAX_FILE_SIZE_BYTES" \
    'BEGIN{
        ok=1; why=""
        if (w==0 || h==0) { ok=0; why="dimensões inválidas" }
        else if (aw>0 && ah>0 && (w<aw || h<ah)) { ok=0; why=sprintf("res %dx%d < %dx%d", w, h, aw, ah) }
        else { bpp=(b*8)/(w*h); if (bpp < mb) { ok=0; why=sprintf("bpp %.2f < %.2f (upscale?)", bpp, mb) } }
        if (ok && mxb>0 && b>mxb) { ok=0; why=sprintf("tamanho %d > %d bytes", b, mxb) }
        printf "%s\t%s\n", (ok?"OK":"NO"), why
    }'
}

# Cache de tela: downscale do master se for maior que PREP_RESOLUTION
make_prep() {
    local f="$1" w h pw ph ext p
    read -r w h < <(magick identify -ping -format '%w %h\n' "$f" 2>/dev/null) || return 0
    pw="${PREP_RESOLUTION%%x*}"; ph="${PREP_RESOLUTION##*x}"
    (( pw>0 && ph>0 && (w>pw || h>ph) )) || return 0
    ext="${f##*.}"; p="${f%.*}-screen.${ext}"
    if magick "$f" -auto-orient -resize "${pw}x${ph}>" -quality 90 "$p" 2>/dev/null; then
        chmod 644 "$p"; log "INFO" "Cache -screen: $(basename "$p")"
    fi
    return 0
}
resolve_screen() {
    local f="$1" ext p
    ext="${1##*.}"; p="${1%.*}-screen.${ext}"
    [[ -f "$p" ]] && { echo "$p"; return; }
    echo "$f"
}

# Aceita (move p/ dest) se for imagem, passar na qualidade e não ser near-duplicata.
accept_image() {
    local src="$1" dest="$2" id="$3" origin="${4:-unknown}"
    file --mime-type -b "$src" | grep -q '^image/' || { log "AVISO" "$id não é imagem. Descartado."; return 1; }
    local res why
    read -r res why < <(check_quality "$src")
    if [[ "$res" != "OK" ]]; then log "AVISO" "Rejeitado $id: $why"; return 1; fi
    local mind h
    mind="$(python3 "$DHASH" --min-dist "$src" "$HASH_FILE" 2>/dev/null)"
    [[ "$mind" =~ ^[0-9]+$ ]] || mind=64
    if (( mind <= DUP_HAMMING )); then
        log "AVISO" "Near-duplicata (dHash=$mind): descartado $id"; return 1
    fi
    mv -f "$src" "$dest"; chmod 644 "$dest"
    h="$(python3 "$DHASH" "$dest" 2>/dev/null)"
    [[ "$h" =~ ^[0-9]+$ ]] || h=0
    printf '%s\t%s\n' "$(basename "$dest")" "$h" >> "$HASH_FILE"
    [[ -n "${id:-}" ]] && echo "$id" >> "$IDS_FILE"
    printf '%s\t%s\t%s\n' "$(basename "$dest")" "$origin" "$(date '+%Y-%m-%d %H:%M:%S')" >> "$SOURCES_FILE"
    make_prep "$dest"
    log "OK" "Aceito $id ($origin) → $(basename "$dest")"
    return 0
}

# Estratégias rotativas do Wallhaven (sem chave de API).
# Saída: sorting|topRange|broad|descrição  (broad=1 → sem 'q')
wh_strategy() {
    case "$1" in
        0) echo "toplist|1w|0|toplist 1w" ;;
        1) echo "toplist|1M|0|toplist 1M" ;;
        2) echo "toplist|6M|0|toplist 6M" ;;
        3) echo "date_added||0|date_added" ;;
        4) echo "toplist|1M|1|toplist 1M (sem q)" ;;
        *) echo "toplist|6M|0|toplist 6M" ;;
    esac
}

do_download_wallhaven() {
    require_tools
    local api="https://wallhaven.cc/api/v1/search"
    local theme_idx=0 src_idx=0
    [[ -f "$THEME_IDX_FILE" ]] && theme_idx="$(cat "$THEME_IDX_FILE" 2>/dev/null || echo 0)"
    [[ "$theme_idx" =~ ^[0-9]+$ ]] || theme_idx=0
    [[ -f "$SOURCE_IDX_FILE" ]] && src_idx="$(cat "$SOURCE_IDX_FILE" 2>/dev/null || echo 0)"
    [[ "$src_idx" =~ ^[0-9]+$ ]] || src_idx=0
    src_idx=$(( src_idx % 5 ))

    local n_themes=${#THEMES[@]}
    local theme=""
    (( n_themes > 0 )) && theme="${THEMES[$((theme_idx % n_themes))]}"
    local wd_sort wd_range broad wd_desc
    IFS='|' read -r wd_sort wd_range broad wd_desc < <(wh_strategy "$src_idx")

    local q_label="${theme:-—}"
    (( broad )) && q_label="—"
    [[ "$theme" == "-" ]] && q_label="—"
    log "INFO" "=== Wallhaven (${wd_desc} | >=${ATLEAST} ${RATIOS} | SFW | tema: '${q_label}') ==="

    local tmp candidates page code url attempt total
    tmp="$(mktemp)"; candidates="$(mktemp)"
    total=0
    for attempt in 0 1; do
        : > "$candidates"
        for ((page=1; page<=PAGES_PER_RUN; page++)); do
            url="${api}?sorting=${wd_sort}&order=desc&atleast=${ATLEAST}&ratios=${RATIOS}&categories=${CATEGORIES}&purity=${PURITY}"
            [[ "$wd_sort" == "toplist" && -n "$wd_range" ]] && url+="&topRange=${wd_range}"
            if (( broad == 0 )) && [[ -n "$theme" && "$theme" != "-" ]]; then
                url+="&q=${theme// /+}"
            fi
            url+="&page=${page}"
            log "INFO" "Página $page ..."
            code="$(curl -s -m 30 -A "$USER_AGENT" -o "$tmp" -w '%{http_code}' "$url")" || { log "AVISO" "Rede falhou p.$page"; sleep "$API_SLEEP"; continue; }
            [[ "$code" != "200" ]] && { log "AVISO" "HTTP $code p.$page"; sleep "$API_SLEEP"; continue; }
            jq -r '.data[]? | [.id, (.favorites|tostring), .path, .file_type] | @tsv' "$tmp" >> "$candidates" 2>/dev/null
            sleep "$API_SLEEP"
        done
        total=$(wc -l < "$candidates" | tr -d ' ')
        (( total > 0 )) && break
        if (( attempt == 0 && broad == 0 )) && [[ -n "$theme" && "$theme" != "-" ]]; then
            log "AVISO" "Sem resultados com o tema '${theme}'; repetindo a estratégia sem 'q'."
            broad=1
        else
            break
        fi
    done
    if [[ "$total" -eq 0 ]]; then
        log "AVISO" "Wallhaven sem resultados."
        rm -f "$tmp" "$candidates"
        echo "$(( (src_idx + 1) % 5 ))" > "$SOURCE_IDX_FILE"
        (( n_themes > 0 )) && echo "$(( (theme_idx + 1) % n_themes ))" > "$THEME_IDX_FILE"
        return 0
    fi
    log "INFO" "$total candidatos. Baixando até $DOWNLOAD_LIMIT novos..."
    local new_count=0 id favs path ftype ext dest td
    while IFS=$'\t' read -r id favs path ftype; do
        (( new_count >= DOWNLOAD_LIMIT )) && break
        [[ -z "${path:-}" ]] && continue
        [[ -f "$IDS_FILE" ]] && grep -qxF "$id" "$IDS_FILE" && continue
        case "$ftype" in image/jpeg) ext=jpg;; image/png) ext=png;; image/webp) ext=webp;; *) continue;; esac
        dest="$IMG_DIR/wallhaven-${id}.${ext}"
        [[ -f "$dest" ]] && continue
        td="$(mktemp --suffix=".${ext}")"
        if curl -fsSL -m 60 -A "$USER_AGENT" -o "$td" "$path"; then
            if accept_image "$td" "$dest" "$id" "wallhaven"; then ((new_count++)); else rm -f "$td"; fi
        else
            log "AVISO" "Falha download $id"; rm -f "$td"; fi
        sleep 1
    done < <(sort -t $'\t' -k2 -nr "$candidates")
    log "INFO" "Wallhaven: $new_count nova(s)."
    echo "$(( (src_idx + 1) % 5 ))" > "$SOURCE_IDX_FILE"
    (( n_themes > 0 )) && echo "$(( (theme_idx + 1) % n_themes ))" > "$THEME_IDX_FILE"
    rm -f "$tmp" "$candidates"
    return 0
}

do_download_reddit() {
    (( REDDIT_ENABLED )) || return 0
    require_tools
    log "INFO" "=== Reddit r/wallpapers (top/month, até $REDDIT_LIMIT) ==="
    local url="https://www.reddit.com/r/wallpapers/top.json?t=month&limit=50"
    local json code
    json="$(mktemp)"
    code="$(curl -s -m 30 -A "$USER_AGENT" -o "$json" -w '%{http_code}' "$url")" || { log "AVISO" "Rede falhou (Reddit)."; rm -f "$json"; return 0; }
    [[ "$code" != "200" ]] && { log "AVISO" "Reddit HTTP $code"; rm -f "$json"; return 0; }
    local re='^https://(i\.redd\.it|i\.imgur\.com)/.+\.(jpe?g|png|webp)(\?.*)?$'
    local total
    total="$(jq -r --arg re "$re" '[.data.children[]?.data | select(.url | test($re; "i"))] | length' "$json" 2>/dev/null)"
    [[ "$total" =~ ^[0-9]+$ ]] || total=0
    if (( total == 0 )); then
        log "AVISO" "Reddit sem links diretos de imagem."
        rm -f "$json"
        return 0
    fi
    log "INFO" "$total link(s) diretos. Baixando até $REDDIT_LIMIT..."
    local new_count=0 id ups u ext dest td
    while IFS=$'\t' read -r id ups u; do
        (( new_count >= REDDIT_LIMIT )) && break
        [[ -z "${u:-}" || -z "${id:-}" ]] && continue
        id="reddit-${id}"
        [[ -f "$IDS_FILE" ]] && grep -qxF "$id" "$IDS_FILE" && continue
        u="${u%%\?*}"
        ext="${u##*.}"; ext="${ext,,}"
        case "$ext" in jpg|jpeg) ext=jpg;; png) ext=png;; webp) ext=webp;; *) continue;; esac
        dest="$IMG_DIR/${id}.${ext}"
        [[ -f "$dest" ]] && continue
        td="$(mktemp --suffix=".${ext}")"
        if curl -fsSL -m 60 -A "$USER_AGENT" -o "$td" "$u"; then
            if accept_image "$td" "$dest" "$id" "reddit"; then ((new_count++)); else rm -f "$td"; fi
        else
            log "AVISO" "Falha download $id"; rm -f "$td"
        fi
        sleep 1
    done < <(jq -r --arg re "$re" '.data.children[]?.data | select(.url | test($re; "i")) | [.id, (.ups//0|tostring), .url] | @tsv' "$json" 2>/dev/null | sort -t $'\t' -k2 -nr)
    rm -f "$json"
    log "INFO" "Reddit: $new_count nova(s)."
    return 0
}

do_download_bing() {
    require_tools
    (( BING_ENABLED )) || return 0
    log "INFO" "=== Bing Imagem do Dia (UHD, $BING_DAYS dias) ==="
    local url="https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=${BING_DAYS}&mkt=en-US&uhd=1&uhdwidth=3840&uhdheight=2160"
    local json code
    json="$(mktemp)"
    code="$(curl -s -m 30 -A "$USER_AGENT" -o "$json" -w '%{http_code}' "$url")"
    [[ "$code" != "200" ]] && { log "AVISO" "Bing HTTP $code"; rm -f "$json"; return 0; }
    local new_count=0 date base full ext dest td
    while IFS=$'\t' read -r date base; do
        (( new_count >= BING_LIMIT )) && break
        [[ -z "$base" ]] && continue
        full="https://www.bing.com${base}"
        case "$base" in *.png) ext=png;; *) ext=jpg;; esac
        dest="$IMG_DIR/bing-${date}.${ext}"
        [[ -f "$dest" ]] && continue
        td="$(mktemp --suffix=".${ext}")"
        if curl -fsSL -m 60 -A "$USER_AGENT" -o "$td" "$full"; then
            if accept_image "$td" "$dest" "bing-$date" "bing"; then ((new_count++)); else rm -f "$td"; fi
        else
            log "AVISO" "Falha Bing $date"; rm -f "$td"; fi
        sleep 1
    done < <(jq -r '.images[]? | [.startdate, .url] | @tsv' "$json" 2>/dev/null)
    rm -f "$json"
    log "INFO" "Bing: $new_count nova(s)."
    return 0
}

do_download_picsum() {
    (( PICSUM_ENABLED )) || return 0
    require_tools
    log "INFO" "=== Lorem Picsum (aleatório, até $PICSUM_LIMIT) ==="
    local new_count=0 dest td
    for ((i=0; i<PICSUM_LIMIT; i++)); do
        (( new_count >= PICSUM_LIMIT )) && break
        local random_id=$RANDOM
        dest="$IMG_DIR/picsum-${random_id}.jpg"
        [[ -f "$dest" ]] && continue
        td="$(mktemp --suffix=".jpg")"
        if curl -fsSL -m 60 -A "$USER_AGENT" -o "$td" "https://picsum.photos/2560/1440?random=${random_id}"; then
            if accept_image "$td" "$dest" "picsum-${random_id}" "picsum"; then ((new_count++)); else rm -f "$td"; fi
        else
            log "AVISO" "Falha Picsum download"; rm -f "$td"
        fi
        sleep 1
    done
    log "INFO" "Lorem Picsum: $new_count nova(s)."
    return 0
}

do_download() {
    do_download_wallhaven
    do_download_reddit
    do_download_bing
    do_download_picsum
    prune_images
    log "INFO" "Total na coleção: $(count_images)."
}

_remove_master() {
    local f="$1" ext
    rm -f "$f"
    ext="${f##*.}"; rm -f "${f%.*}-screen.${ext}"
    if [[ -f "$HASH_FILE" ]]; then
        grep -vF "$(basename "$f")" "$HASH_FILE" > "$HASH_FILE.tmp" 2>/dev/null && mv "$HASH_FILE.tmp" "$HASH_FILE"
    fi
    log "INFO" "Removida: $(basename "$f")"
}

# Preenche o array associativo passado por nome com imagens protegidas:
# like efetivo (score>0) ou exibida nas últimas 168h.
_prune_protected() {
    local -n out="$1"
    local f ts name cutoff epoch
    if [[ -f "$SCORES_FILE" ]]; then
        while IFS= read -r f; do
            [[ -n "$f" ]] && out["$f"]=1
        done < <(awk -F'\t' '
            $3=="like"{l[$1]++}
            $3=="unlike"{if(l[$1]>0)l[$1]--}
            $3=="block"{b[$1]=1}
            $3=="unblock"{b[$1]=0}
            {seen[$1]=1}
            END{for(f in seen) if(l[f]>0 && !b[f]) print f}
        ' "$SCORES_FILE" 2>/dev/null)
    fi
    if [[ -f "$HISTORY_FILE" ]]; then
        cutoff=$(( $(date +%s) - 168 * 3600 ))
        while IFS=$'\t' read -r ts name; do
            [[ -z "${name:-}" ]] && continue
            epoch=$(date -d "$ts" +%s 2>/dev/null) || continue
            (( epoch >= cutoff )) && out["$name"]=1
        done < "$HISTORY_FILE"
    fi
}

prune_images() {
    local total; total=$(count_images)
    (( total <= MAX_IMAGES )) && return 0
    local -A prot=()
    _prune_protected prot
    local over=$(( total - MAX_IMAGES )) removed=0 f
    log "INFO" "$total > limite $MAX_IMAGES. Removendo até $over (protegidas: like/últimas 168h)."
    while IFS= read -r f; do
        (( removed >= over )) && break
        [[ -z "$f" ]] && continue
        [[ -n "${prot[$(basename "$f")]:-}" ]] && continue
        _remove_master "$f"; ((removed++))
    done < <(find "$IMG_DIR" -maxdepth 1 -type f \
        \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' \) \
        ! -iname '*-screen.*' -printf '%T@\t%p\n' 2>/dev/null \
        | sort -n | cut -f2-)

    total=$(count_images)
    if (( total > MAX_IMAGES + 10 )); then
        log "AVISO" "Ainda $total > $((MAX_IMAGES + 10)); removendo as mais antigas mesmo protegidas."
        over=$(( total - MAX_IMAGES )); removed=0
        while IFS= read -r f; do
            (( removed >= over )) && break
            [[ -z "$f" ]] && continue
            _remove_master "$f"; ((removed++))
        done < <(find "$IMG_DIR" -maxdepth 1 -type f \
            \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' \) \
            ! -iname '*-screen.*' -printf '%T@\t%p\n' 2>/dev/null \
            | sort -n | cut -f2-)
    fi
    return 0
}

apply_wallpaper() {
    local img="$1"
    [[ -z "${img:-}" ]] && return 1
    if command -v plasma-apply-wallpaperimage >/dev/null 2>&1; then
        if plasma-apply-wallpaperimage "$img" >/dev/null 2>&1; then return 0; fi
        log "AVISO" "plasma-apply-wallpaperimage falhou; tentando D-Bus."
    fi
    if command -v dbus-send >/dev/null 2>&1 && [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
        local js
        js="string:
var allDesktops = desktops();
for (i=0;i<allDesktops.length;i++) {
    d = allDesktops[i];
    d.wallpaperPlugin = 'org.kde.image';
    d.currentConfigGroup = Array('Wallpaper', 'org.kde.image', 'General');
    d.writeConfig('Image', 'file://${img}');
}"
        if dbus-send --session --dest=org.kde.plasmashell --type=method_call \
            /PlasmaShell org.kde.PlasmaShell.evaluateScript "$js" >/dev/null 2>&1; then return 0; fi
        log "AVISO" "Fallback D-Bus também falhou."
    fi
    return 1
}

current_image() {
    local n
    if [[ -f "$HISTORY_FILE" ]]; then
        n="$(tail -n 1 "$HISTORY_FILE" 2>/dev/null | cut -f2)"
        [[ -n "$n" && -f "$IMG_DIR/$n" ]] && { echo "$n"; return 0; }
    fi
    local -a imgs=()
    mapfile -t imgs < <(list_masters | sort)
    (( ${#imgs[@]} == 0 )) && return 1
    local idx=0
    [[ -f "$INDEX_FILE" ]] && idx="$(cat "$INDEX_FILE" 2>/dev/null || echo 0)"
    [[ "$idx" =~ ^[0-9]+$ ]] || idx=0
    basename "${imgs[$((idx % ${#imgs[@]}))]}"
}

do_rotate() {
    local dry=0
    [[ "${1:-}" == "--dry-run" ]] && dry=1
    local total; total=$(count_images)
    (( total == 0 )) && { log "AVISO" "Sem imagens. Rode 'download' primeiro."; return 0; }

    local choice="" rc=0
    if [[ -f "$SCORE" ]]; then
        choice="$(python3 "$SCORE" pick 2>>"$LOG_FILE")" || rc=$?
    fi
    if (( rc == 4 )); then
        log "ERRO" "Todas as imagens estão bloqueadas (use 'unblock <arquivo>')."
        return 1
    fi
    if [[ -z "$choice" || ! -f "$IMG_DIR/$choice" ]]; then
        log "AVISO" "score.py indisponível; usando fallback por índice alfabético."
        local -a imgs=()
        mapfile -t imgs < <(list_masters | sort)
        (( ${#imgs[@]} == 0 )) && { log "AVISO" "Sem imagens."; return 0; }
        local idx=0
        [[ -f "$INDEX_FILE" ]] && idx="$(cat "$INDEX_FILE" 2>/dev/null || echo 0)"
        [[ "$idx" =~ ^[0-9]+$ ]] || idx=0
        (( idx >= ${#imgs[@]} )) && idx=0
        choice="$(basename "${imgs[$idx]}")"
        idx=$(( (idx + 1) % ${#imgs[@]} ))
        echo "$idx" > "$INDEX_FILE"
    fi

    local current; current="$(resolve_screen "$IMG_DIR/$choice")"
    if (( dry )); then
        log "DRY-RUN" "Escolheria: $choice (nada aplicado/registrado)"
        if [[ -f "$SCORE" ]]; then
            python3 "$SCORE" pick --explain --top 5 2>/dev/null | sed 's/^/  /' || true
        fi
        return 0
    fi

    if apply_wallpaper "$current"; then
        log "OK" "Carrossel: $(basename "$choice")"
        printf '%s\t%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$choice" >> "$HISTORY_FILE"

        # Notificação opcional
        if (( NOTIFY_ENABLED )) && command -v notify-send >/dev/null 2>&1; then
            local source=$(echo "$choice" | cut -d'-' -f1)
            local source_name
            case "$source" in
                wallhaven) source_name="Wallhaven" ;;
                reddit) source_name="Reddit" ;;
                bing) source_name="Bing" ;;
                picsum) source_name="Lorem Picsum" ;;
                *) source_name="Outro" ;;
            esac
            notify-send "Wallpaper Carrossel" "Wallpaper: $(basename "$choice")\nFonte: $source_name" \
                --icon=preferences-desktop-wallpaper \
                --app-name=wallpaper-carousel \
                --expire-time=3000 &
        fi
    else
        log "ERRO" "Não apliquei $(basename "$choice"). Sessão Plasma ativa?"
        return 1
    fi
    return 0
}

do_feedback() {
    local action="$1" target="${2:-}"
    [[ -f "$SCORE" ]] || die "Falta $SCORE"
    if [[ -z "$target" ]]; then
        target="$(current_image)" || die "Nenhum wallpaper atual; informe o arquivo."
    fi
    [[ -z "$target" ]] && die "Nenhum wallpaper atual; informe o arquivo."
    python3 "$SCORE" "$action" "$(basename "$target")"
}

do_why() {
    [[ -f "$SCORE" ]] || die "Falta $SCORE"
    python3 "$SCORE" pick --explain --top 5
}

do_setup() {
    require_tools
    mkdir -p "$SYSTEMD_USER_DIR"
    local runner="$SELF_DIR/run.sh"
    cat > "$SYSTEMD_USER_DIR/wallpaper-carousel-rotate.service" <<EOF
[Unit]
Description=Wallpaper carousel: alterna o papel de parede
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=oneshot
ExecStart=$runner rotate
Environment=XDG_RUNTIME_DIR=%t
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus
Environment=WAYLAND_DISPLAY=wayland-0
Environment=HOME=%h
EOF
    cat > "$SYSTEMD_USER_DIR/$TIMER_ROTATE" <<EOF
[Unit]
Description=Alterna o papel de parede a cada ${ROTATE_MINUTES} min

[Timer]
OnBootSec=30s
OnCalendar=*:0/${ROTATE_MINUTES}
AccuracySec=10s
Persistent=true

[Install]
WantedBy=timers.target
EOF
    cat > "$SYSTEMD_USER_DIR/wallpaper-carousel-download.service" <<EOF
[Unit]
Description=Wallpaper carousel: baixa novos wallpapers (curadoria premium)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$runner download
Environment=HOME=%h
EOF
    cat > "$SYSTEMD_USER_DIR/$TIMER_DOWNLOAD" <<EOF
[Unit]
Description=Renova a colecao de wallpapers a cada ${DOWNLOAD_EVERY_HOURS}h

[Timer]
OnBootSec=2min
OnCalendar=0/${DOWNLOAD_EVERY_HOURS}:15
AccuracySec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now "$TIMER_ROTATE" >/dev/null 2>&1
    systemctl --user enable --now "$TIMER_DOWNLOAD" >/dev/null 2>&1
    if (( $(count_images) == 0 )); then
        log "INFO" "Coleção vazia — baixando lote inicial..."
        do_download
    fi
    do_rotate
    log "OK" "Setup concluído. Rotação ${ROTATE_MINUTES}min; download ${DOWNLOAD_EVERY_HOURS}h."
    log "OK" "Imagens em: $IMG_DIR"
    echo
    do_status
}

do_status() {
    echo "============ Wallpaper Carousel (v4) — Status ============"
    echo "Diretório de imagens  : $IMG_DIR"
    echo "Imagens na coleção    : $(count_images) (limite: $MAX_IMAGES)"
    echo "Intervalo carrossel   : $ROTATE_MINUTES min"
    echo "Intervalo download    : $DOWNLOAD_EVERY_HOURS h"
    echo "Preferência diurna    : Bing priorizado (06:00–18:59)"
    echo "Fontes                : Wallhaven rotativo + Reddit + Bing | >=${ATLEAST} ${RATIOS} SFW"
    echo "Qualidade             : bpp >= ${MIN_BPP}; teto ${MAX_FILE_SIZE_KB}KB"
    echo "Dedup                 : dHash Hamming <= ${DUP_HAMMING}; Bing $([[ ${BING_ENABLED:-1} -eq 1 ]] && echo ON || echo OFF); Reddit $([[ ${REDDIT_ENABLED:-1} -eq 1 ]] && echo ON || echo OFF)"
    echo "Temas                 : ${THEMES[*]}"
    echo

    if [[ -f "$SCORE" ]]; then
        local json
        json="$(python3 "$SCORE" pick --json --top 3 2>/dev/null)" || json=""
        if [[ -n "$json" ]]; then
            echo "--- Próximo pick (top 3 do score) ---"
            jq -r '.top[] | "  \(.total)  \(.name)  (luma \(.luma), sim6 \(.similar_max))"' <<< "$json" 2>/dev/null
            echo "Feedback: $(jq -r '.counts.likes' <<< "$json" 2>/dev/null) like(s), $(jq -r '.counts.blocks' <<< "$json" 2>/dev/null) block(s)"
            echo "Série   : $(jq -r '.counts.history' <<< "$json" 2>/dev/null) exibição(ões); contexto agora: $(jq -r 'if .night then "noite" else "dia" end' <<< "$json" 2>/dev/null)"
            echo
        fi
    fi

    echo "--- Timers systemd (usuário) ---"
    systemctl --user list-timers 'wallpaper-carousel-*' --all --no-pager 2>/dev/null || echo "(systemd user indisponível)"
    echo
    echo "--- Últimas linhas do log ---"
    tail -n 8 "$LOG_FILE" 2>/dev/null || echo "(sem log ainda)"
}

do_clean() {
    local n; n=$(count_images)
    rm -f "$IMG_DIR"/*.jpg "$IMG_DIR"/*.jpeg "$IMG_DIR"/*.png "$IMG_DIR"/*.webp 2>/dev/null
    rm -f "$INDEX_FILE" "$HASH_FILE" "$IDS_FILE" "$THEME_IDX_FILE" "$SOURCE_IDX_FILE" \
          "$HISTORY_FILE" "$SOURCES_FILE" "$SCORES_FILE" "$STATE_DIR/features.tsv" "$STATE_DIR/accent"
    log "OK" "Removidas $n imagens e estado de seleção/dedup."
}

do_uninstall() {
    systemctl --user disable --now "$TIMER_ROTATE" >/dev/null 2>&1
    systemctl --user disable --now "$TIMER_DOWNLOAD" >/dev/null 2>&1
    rm -f "$SYSTEMD_USER_DIR"/wallpaper-carousel-rotate.service \
          "$SYSTEMD_USER_DIR"/wallpaper-carousel-rotate.timer \
          "$SYSTEMD_USER_DIR"/wallpaper-carousel-download.service \
          "$SYSTEMD_USER_DIR"/wallpaper-carousel-download.timer
    systemctl --user daemon-reload
    log "OK" "Timers removidos. Imagens mantidas em $IMG_DIR (use 'clean' p/ removê-las)."
}

usage() {
    cat <<EOF
Uso: $(basename "$0") <comando>

  download         Baixa novos wallpapers (Wallhaven rotativo + Reddit + Bing)
  rotate           Aplica a próxima imagem escolhida pelo score
  rotate --dry-run Mostra a escolha e o breakdown, sem aplicar/registrar
  status           Status, top 3 do próximo pick, timers e log
  why              Breakdown do score (top 5 candidatos)
  like [arquivo]   Marca like (sem arquivo: wallpaper atual)
  block [arquivo]  Bloqueia a imagem (nunca mais é escolhida)
  unlike [arquivo] Remove um like
  unblock [arquivo] Desbloqueia a imagem
  eval             Compara a seleção antiga (alfabética) com a nova (score)
  setup            Instala/ativa os timers (rotação + download) e aplica um wallpaper
  clean            Remove todas as imagens baixadas e o estado de seleção
  uninstall        Desativa e remove os timers do systemd (mantém as imagens)
EOF
}

case "${1:-}" in
    download)  do_download ;;
    rotate)
        if [[ "${2:-}" == "--dry-run" ]]; then do_rotate --dry-run; else do_rotate; fi ;;
    setup)     do_setup ;;
    status)    do_status ;;
    why)       do_why ;;
    like|block|unlike|unblock) do_feedback "$1" "${2:-}" ;;
    eval)      python3 "$SCORE" eval "${@:2}" ;;
    clean)     do_clean ;;
    uninstall) do_uninstall ;;
    ""|-h|--help|help) usage ;;
    *) echo "Comando desconhecido: $1"; usage; exit 1 ;;
esac
