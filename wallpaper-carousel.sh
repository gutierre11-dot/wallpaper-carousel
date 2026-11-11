#!/usr/bin/env bash
#
# wallpaper-carousel.sh — Carrossel premium de wallpapers (KDE Plasma / Wayland)
#
# v6 (robustez e operação):
#   * Config validada no início (resolução WxH, numéricos e ratios) com erro claro.
#   * Cache de tela dinâmico: PREP_RESOLUTION=auto detecta o monitor primário
#     (kscreen-doctor -j, fallback xrandr), gera cache exato WxH e valida/regenera
#     cache ausente, estranho ou mais antigo que o master. Novo comando 'prep'.
#   * Poda com qualidade via 'score.py prune-candidates' (fallback por mtime),
#     teto de armazenamento (WALLPAPER_MAX_STORAGE_MB) e aviso de pouco espaço.
#   * Higiene de estado: remove master + cache -screen + linhas exatas de
#     sources.tsv/hashes.tsv (ids preservados); novo comando 'maintenance'
#     (score.py housekeeping + limpeza local + rotação de log + prep opcional).
#   * Rotação de log (carousel.log → carousel.log.1) por tamanho.
#   * Download por fonte: download [all|wallhaven|reddit|bing|picsum].
#   * Qualidade de download exige landscape com ratio entre 1.55 e 1.90.
#   * Notificações com ações opcionais (like/block via notify-send -A).
#   * systemd endurecido + setup --dry-run + WALLPAPER_SYSTEMD_DIR (testes).
#   * Novo comando 'doctor' (humano/JSON) para diagnóstico de saúde.
#   * Status v6: última troca, distribuição por fonte, disco, cache e avisos.
#   * Modo biblioteca (WALLPAPER_LIB_ONLY=1) para testes; tests/shell-selftest.sh.
#
# v5 (fontes expandidas):
#   * Nova fonte: Lorem Picsum (fotografia aleatória de alta qualidade, 2560x1440).
#   * Limite aumentado: 500 imagens (era 40) para suportar mais diversidade.
#   * Integração ao score: peso 0.75 para picsum (entre Bing 0.55 e Wallhaven 1.0).
#   * Limites de download aumentados para preencher o acervo mais rápido.
#
# v4 (seleção por sequência):
#   * Cota justa de fonte no lugar da alternância A-B-A-B.
#   * Contexto diurno realinhado, frescor menor, impacto visual e diversidade.
#   * Gosto aprendido: 2+ likes viram centróide e generalizam para imagens novas.
#
# v3 (seleção inteligente):
#   * Seleção por score (score.py): like/block, contexto dia/noite, accent do
#     KDE, frescor, anti-série (últimas 6 exibições) e jitter determinístico.
#   * Diversificação de fontes: Wallhaven rotativo + Reddit + Bing + Picsum.
#   * Feedback persistente: like/block/unlike/unblock/why/eval.
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
FEATURES_FILE="$STATE_DIR/features.tsv"
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
MIN_RATIO="${WALLPAPER_MIN_RATIO:-1.55}"      # proporção mínima (landscape)
MAX_RATIO="${WALLPAPER_MAX_RATIO:-1.90}"      # proporção máxima (rejeita 21:9)

# --- Dedup ---
DUP_HAMMING="${WALLPAPER_DUP_HAMMING:-4}"    # distância de Hamming máxima (dHash)

# --- Cache de tela (downscale p/ aplicar leve na iGPU) ---
# "auto" = detecta a resolução do monitor primário (kscreen-doctor/xrandr).
PREP_RESOLUTION="${WALLPAPER_PREP_RESOLUTION:-auto}"

# --- Higiene de estado / armazenamento / log ---
MAX_STORAGE_MB="${WALLPAPER_MAX_STORAGE_MB:-3072}" # teto de uso de images/
LOG_MAX_KB="${WALLPAPER_LOG_MAX_KB:-1024}"         # rotaciona carousel.log acima disto

# --- Bing Imagem do Dia ---
BING_ENABLED="${WALLPAPER_BING_ENABLED:-1}"
BING_DAYS="${WALLPAPER_BING_DAYS:-8}"
BING_LIMIT="${WALLPAPER_BING_LIMIT:-5}"

# --- Lorem Picsum (API pública sem chave) ---
PICSUM_ENABLED="${WALLPAPER_PICSUM_ENABLED:-1}"
PICSUM_LIMIT="${WALLPAPER_PICSUM_LIMIT:-10}"

# --- Notificações ---
NOTIFY_ENABLED="${WALLPAPER_NOTIFY_ENABLED:-0}"
NOTIFY_ACTIONS="${WALLPAPER_NOTIFY_ACTIONS:-0}" # 1 = botões Curtir/Bloquear na notificação

VERSION="6"
USER_AGENT="wallpaper-carousel/6.0 (Fedora KDE personal script; +https://wallhaven.cc)"
USER_AGENT_MOZILLA="Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
TIMER_ROTATE="wallpaper-carousel-rotate.timer"
TIMER_DOWNLOAD="wallpaper-carousel-download.timer"
SERVICE_ROTATE="wallpaper-carousel-rotate.service"
SERVICE_DOWNLOAD="wallpaper-carousel-download.service"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
SYSTEMD_OVERRIDE_DIR="${WALLPAPER_SYSTEMD_DIR:-}" # se definido: grava lá e NÃO chama systemctl
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# ==========================================================================

# Estado em memória (resolução de tela detectada uma vez por execução).
_SCREEN_RES=""
_SCREEN_RES_SOURCE=""
_LOG_ROTATED=0

_have() { command -v "$1" >/dev/null 2>&1; }

mkdir -p "$IMG_DIR" "$STATE_DIR" "$LOG_DIR" 2>/dev/null || {
    printf 'ERRO: não foi possível criar %s (verifique permissões).\n' "$DATA_DIR" >&2
    exit 1
}

log() {
    local lvl="$1"
    shift
    printf '%s [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$lvl" "$*" | tee -a "$LOG_FILE" >&2
}
die() {
    log "ERRO" "$*"
    exit 1
}

# --------------------------------------------------------------------------
# Validação de configuração (chamada no início, antes do dispatch)
# --------------------------------------------------------------------------
_validate_int() {
    local name="$1" val="$2" min="$3" max="$4"
    [[ "$val" =~ ^[0-9]+$ ]] || die "Config inválida: $name='$val' (esperado inteiro entre $min e $max)"
    (( val >= min && val <= max )) || die "Config inválida: $name=$val fora do intervalo [$min, $max]"
}

_validate_num() {
    local name="$1" val="$2" min="$3" max="$4"
    [[ "$val" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "Config inválida: $name='$val' (esperado número entre $min e $max)"
    if ! awk -v v="$val" -v mn="$min" -v mx="$max" 'BEGIN{exit !(v+0 >= mn+0 && v+0 <= mx+0)}'; then
        die "Config inválida: $name=$val fora do intervalo [$min, $max]"
    fi
}

_validate_res() {
    local name="$1" val="$2" allow_auto="${3:-0}"
    if [[ "$allow_auto" == "1" && "$val" == "auto" ]]; then
        return 0
    fi
    if [[ ! "$val" =~ ^[0-9]+x[0-9]+$ ]]; then
        if [[ "$allow_auto" == "1" ]]; then
            die "Config inválida: $name='$val' (esperado WxH, ex.: 2560x1440, ou 'auto')"
        fi
        die "Config inválida: $name='$val' (esperado WxH, ex.: 2560x1440)"
    fi
    local w="${val%%x*}" h="${val##*x}"
    (( w >= 320 && h >= 200 && w <= 16384 && h <= 16384 )) ||
        die "Config inválida: $name=$val (dimensões fora do razoável)"
}

_validate_bool() {
    local name="$1" val="$2"
    [[ "$val" == "0" || "$val" == "1" ]] || die "Config inválida: $name='$val' (use 0 ou 1)"
}

_validate_wallhaven_ratios() {
    local val="$1" part
    local -a parts=()
    IFS=',' read -r -a parts <<< "$val"
    (( ${#parts[@]} > 0 )) || die "Config inválida: WALLPAPER_RATIOS vazio (ex.: 16x9,16x10)"
    for part in "${parts[@]}"; do
        [[ "$part" =~ ^[0-9]+x[0-9]+$ ]] ||
            die "Config inválida: WALLPAPER_RATIOS='$val' (cada item deve ser WxH, ex.: 16x9,16x10)"
    done
}

validate_config() {
    _validate_res "WALLPAPER_RESOLUTION" "$ATLEAST" 0
    _validate_res "WALLPAPER_PREP_RESOLUTION" "$PREP_RESOLUTION" 1
    _validate_wallhaven_ratios "$RATIOS"

    _validate_int "WALLPAPER_ROTATE_MINUTES" "$ROTATE_MINUTES" 1 1440
    _validate_int "WALLPAPER_DOWNLOAD_HOURS" "$DOWNLOAD_EVERY_HOURS" 1 24
    _validate_int "WALLPAPER_MAX_IMAGES" "$MAX_IMAGES" 1 100000
    _validate_int "WALLPAPER_PAGES" "$PAGES_PER_RUN" 1 100
    _validate_int "WALLPAPER_DOWNLOAD_LIMIT" "$DOWNLOAD_LIMIT" 0 500
    _validate_num "WALLPAPER_API_SLEEP" "$API_SLEEP" 0 600
    _validate_int "WALLPAPER_REDDIT_LIMIT" "$REDDIT_LIMIT" 0 500
    _validate_num "WALLPAPER_MIN_BPP" "$MIN_BPP" 0 64
    _validate_int "WALLPAPER_MAX_FILE_SIZE_KB" "$MAX_FILE_SIZE_KB" 0 1048576
    _validate_int "WALLPAPER_DUP_HAMMING" "$DUP_HAMMING" 0 64
    _validate_int "WALLPAPER_BING_DAYS" "$BING_DAYS" 1 31
    _validate_int "WALLPAPER_BING_LIMIT" "$BING_LIMIT" 0 500
    _validate_int "WALLPAPER_PICSUM_LIMIT" "$PICSUM_LIMIT" 0 500
    _validate_int "WALLPAPER_MAX_STORAGE_MB" "$MAX_STORAGE_MB" 1 1048576
    _validate_int "WALLPAPER_LOG_MAX_KB" "$LOG_MAX_KB" 1 1048576

    _validate_num "WALLPAPER_MIN_RATIO" "$MIN_RATIO" 1.0 4.0
    _validate_num "WALLPAPER_MAX_RATIO" "$MAX_RATIO" 1.0 4.0
    if ! awk -v a="$MIN_RATIO" -v b="$MAX_RATIO" 'BEGIN{exit !(a+0 < b+0)}'; then
        die "Config inválida: WALLPAPER_MIN_RATIO=$MIN_RATIO >= WALLPAPER_MAX_RATIO=$MAX_RATIO"
    fi
    _validate_bool "WALLPAPER_REDDIT_ENABLED" "$REDDIT_ENABLED"
    _validate_bool "WALLPAPER_BING_ENABLED" "$BING_ENABLED"
    _validate_bool "WALLPAPER_PICSUM_ENABLED" "$PICSUM_ENABLED"
    _validate_bool "WALLPAPER_NOTIFY_ENABLED" "$NOTIFY_ENABLED"
    _validate_bool "WALLPAPER_NOTIFY_ACTIONS" "$NOTIFY_ACTIONS"

    return 0
}

require_tools() {
    local miss=() t
    for t in curl jq file magick python3; do
        _have "$t" || miss+=("$t")
    done
    ((${#miss[@]})) && die "Faltam: ${miss[*]} (instale: sudo dnf install ${miss[*]})"
    [[ -f "$DHASH" ]] || die "Falta $DHASH (helper de perceptual hash)"
    python3 -c 'import PIL' 2>/dev/null || die "Falta Pillow (sudo dnf install python3-pillow)"
    return 0
}

# --------------------------------------------------------------------------
# Inventário de imagens
# --------------------------------------------------------------------------
list_masters() {
    find "$IMG_DIR" -maxdepth 1 -type f \
        \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' \) \
        ! -iname '*-screen.*' 2>/dev/null
}
count_images() { list_masters | wc -l | tr -d ' '; }
count_screens() { find "$IMG_DIR" -maxdepth 1 -type f -iname '*-screen.*' 2>/dev/null | wc -l | tr -d ' '; }

_list_masters_mtime() {
    find "$IMG_DIR" -maxdepth 1 -type f \
        \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' \) \
        ! -iname '*-screen.*' -printf '%T@\t%p\n' 2>/dev/null | sort -n
}

_count_lines() {
    local f="$1"
    if [[ -f "$f" ]]; then
        wc -l < "$f" 2>/dev/null | tr -d ' '
    else
        echo 0
    fi
}

img_bpp() {
    local w h b
    read -r w h < <(magick identify -ping -format '%w %h\n' "$1" 2>/dev/null) || {
        echo 0
        return
    }
    b=$(stat -c%s "$1" 2>/dev/null || echo 0)
    awk -v w="$w" -v h="$h" -v b="$b" 'BEGIN{ if (w*h>0) printf "%.3f", (b*8)/(w*h); else printf "0" }'
}

# Imprime "OK<TAB>" ou "NO<TAB>motivo".
# Rejeita: dimensões ilegíveis, resolução abaixo de ATLEAST, orientação retrato,
# proporção fora de [MIN_RATIO, MAX_RATIO] e bpp baixo (upscale).
check_quality() {
    local f="$1" w h b
    read -r w h < <(magick identify -ping -format '%w %h\n' "$f" 2>/dev/null) ||
        {
            printf 'NO\tdimensões ilegíveis\n'
            return
        }
    b=$(stat -c%s "$f" 2>/dev/null || echo 0)
    awk -v w="$w" -v h="$h" -v b="$b" \
        -v aw="${ATLEAST%%x*}" -v ah="${ATLEAST##*x}" \
        -v mb="$MIN_BPP" -v mxkb="$MAX_FILE_SIZE_KB" \
        -v minr="$MIN_RATIO" -v maxr="$MAX_RATIO" \
        'BEGIN{
            ok=1; why=""
            if (w==0 || h==0) { ok=0; why="dimensões inválidas" }
            else if (aw>0 && ah>0 && (w<aw || h<ah)) { ok=0; why=sprintf("res %dx%d < %dx%d", w, h, aw, ah) }
            else if (w<=h) { ok=0; why=sprintf("retrato %dx%d (exige landscape)", w, h) }
            else {
                ratio=w/h
                if (ratio < minr+0 || ratio > maxr+0) {
                    ok=0; why=sprintf("ratio %.2f fora de [%.2f, %.2f] (%dx%d)", ratio, minr+0, maxr+0, w, h)
                } else {
                    bpp=(b*8)/(w*h)
                    if (bpp < mb+0) { ok=0; why=sprintf("bpp %.2f < %.2f (upscale?)", bpp, mb+0) }
                }
            }
            if (ok && mxkb+0>0 && b > mxkb*1024) { ok=0; why=sprintf("tamanho %d > %d bytes", b, mxkb*1024) }
            printf "%s\t%s\n", (ok?"OK":"NO"), why
        }'
}

# --------------------------------------------------------------------------
# Resolução de tela e cache "-screen"
# --------------------------------------------------------------------------
# Ecoa "WxH<TAB>fonte". A fonte é env|kscreen-doctor|xrandr|fallback.
detect_screen_resolution() {
    if [[ "${PREP_RESOLUTION:-auto}" != "auto" ]]; then
        printf '%s\tenv\n' "$PREP_RESOLUTION"
        return 0
    fi
    local out=""
    if _have kscreen-doctor && _have jq; then
        out="$(kscreen-doctor -j 2>/dev/null | jq -r '
            [ .outputs[]? | select(.enabled==true) |
              { p: (.priority // 99), w: (.size.width // 0), h: (.size.height // 0) } ]
            | sort_by(.p)
            | map(select(.w > 0 and .h > 0))
            | .[0] // empty
            | "\(.w)x\(.h)"' 2>/dev/null)"
        if [[ "$out" =~ ^[0-9]+x[0-9]+$ ]]; then
            printf '%s\tkscreen-doctor\n' "$out"
            return 0
        fi
    fi
    if _have xrandr; then
        out="$(xrandr --current 2>/dev/null | awk '/ connected/ {
            for (i = 1; i <= NF; i++) {
                if ($i ~ /^[0-9]+x[0-9]+\+[0-9]+\+[0-9]+$/) {
                    sub(/\+.*/, "", $i)
                    print $i
                    exit
                }
            }
        }')"
        if [[ "$out" =~ ^[0-9]+x[0-9]+$ ]]; then
            printf '%s\txrandr\n' "$out"
            return 0
        fi
    fi
    printf '2560x1440\tfallback\n'
}

# Detecta uma vez e guarda em globais (herdadas pelos subshells das chamadas).
screen_init() {
    [[ -n "${_SCREEN_RES:-}" ]] && return 0
    local out
    out="$(detect_screen_resolution)"
    _SCREEN_RES="${out%%$'\t'*}"
    _SCREEN_RES_SOURCE="${out##*$'\t'}"
    [[ -n "$_SCREEN_RES" ]] || _SCREEN_RES="2560x1440"
    return 0
}

screen_resolution() {
    screen_init
    printf '%s\n' "$_SCREEN_RES"
}

screen_source() {
    screen_init
    printf '%s\n' "$_SCREEN_RES_SOURCE"
}

_prep_target() {
    local res
    res="$(screen_resolution)"
    printf '%s %s\n' "${res%%x*}" "${res##*x}"
}

_prep_path() {
    local f="$1" ext="${1##*.}"
    printf '%s/%s-screen.%s\n' "$IMG_DIR" "$(basename "${f%.*}")" "$ext"
}

# 0 = o master precisa de cache (é maior que o alvo); 1 = dispensa cache.
_prep_needed() {
    local f="$1" w h pw ph
    read -r w h < <(magick identify -ping -format '%w %h\n' "$f" 2>/dev/null) || return 1
    read -r pw ph < <(_prep_target)
    (( w > pw || h > ph ))
}

# 0 = cache existe, com dimensões exatas e mais novo que o master.
_prep_valid() {
    local f="$1" p="$2" w h cw ch pw ph mtime pmtime
    [[ -f "$p" ]] || return 1
    read -r w h < <(magick identify -ping -format '%w %h\n' "$f" 2>/dev/null) || return 1
    read -r cw ch < <(magick identify -ping -format '%w %h\n' "$p" 2>/dev/null) || return 1
    read -r pw ph < <(_prep_target)
    (( cw == pw && ch == ph )) || return 1
    mtime=$(stat -c %Y "$f" 2>/dev/null || echo 0)
    pmtime=$(stat -c %Y "$p" 2>/dev/null || echo 0)
    (( pmtime >= mtime ))
}

# Gera o cache exato WxH (crop central). Retorna 2 em falha.
_prep_build() {
    local f="$1" p pw ph
    p="$(_prep_path "$f")"
    read -r pw ph < <(_prep_target)
    magick "$f" -auto-orient -resize "${pw}x${ph}^" -gravity center -extent "${pw}x${ph}" \
        -quality 92 "$p" 2>/dev/null || return 2
    chmod 644 "$p" 2>/dev/null || true
    return 0
}

# 0 = cache pronto (criado ou válido), 1 = master não precisa, 2 = falha.
make_prep() {
    local f="$1" force="${2:-0}" p
    _prep_needed "$f" || return 1
    p="$(_prep_path "$f")"
    if ((force == 0)) && _prep_valid "$f" "$p"; then
        return 0
    fi
    _prep_build "$f" || return 2
    log "INFO" "Cache -screen: $(basename "$p")"
    return 0
}

# Ecoa o caminho a aplicar: cache -screen validado/regenerado, ou o master.
resolve_screen() {
    local f="$1" quiet="${2:-0}" p
    if ! _prep_needed "$f"; then
        printf '%s\n' "$f"
        return 0
    fi
    p="$(_prep_path "$f")"
    if _prep_valid "$f" "$p"; then
        printf '%s\n' "$p"
        return 0
    fi
    if _prep_build "$f"; then
        if ((quiet == 0)); then
            log "INFO" "Cache regenerado: $(basename "$p")"
        fi
        printf '%s\n' "$p"
        return 0
    fi
    log "AVISO" "Falha ao gerar cache de tela para $(basename "$f"); usando o master."
    printf '%s\n' "$f"
}

# Percorre todos os masters garantindo o cache. $1=quiet $2=force
_prep_all() {
    local quiet="${1:-0}" force="${2:-0}"
    local total=0 created=0 updated=0 ok=0 skipped=0 failed=0
    local f p existed
    while IFS= read -r f; do
        [[ -z "$f" ]] && continue
        total=$((total + 1))
        if ! _prep_needed "$f"; then
            skipped=$((skipped + 1))
            continue
        fi
        p="$(_prep_path "$f")"
        existed=0
        [[ -f "$p" ]] && existed=1
        if ((force == 0)) && _prep_valid "$f" "$p"; then
            ok=$((ok + 1))
            continue
        fi
        if _prep_build "$f"; then
            if ((existed)); then
                updated=$((updated + 1))
            else
                created=$((created + 1))
            fi
        else
            failed=$((failed + 1))
        fi
    done < <(list_masters)
    screen_init
    local res="$_SCREEN_RES"
    log "INFO" "Prep de cache ($res): $total master(s); $created criado(s), $updated atualizado(s), $ok já ok, $skipped sem necessidade, $failed erro(s)"
    if ((quiet == 0)); then
        printf 'Prep de cache de tela (%s)\n' "$res"
        printf '  masters: %d | criados: %d | atualizados: %d | já ok: %d | sem necessidade: %d | erros: %d\n' \
            "$total" "$created" "$updated" "$ok" "$skipped" "$failed"
    fi
    return 0
}

do_prep() {
    local rebuild=0 a
    for a in "$@"; do
        case "$a" in
            --rebuild) rebuild=1 ;;
            "") ;;
            *) die "Opção desconhecida para prep: $a (use --rebuild)" ;;
        esac
    done
    _prep_all 0 "$rebuild"
}

# --------------------------------------------------------------------------
# Aceitação de download (qualidade + dedup + estado + cache)
# --------------------------------------------------------------------------
accept_image() {
    local src="$1" dest="$2" id="$3" origin="${4:-unknown}"
    local res why mind h
    file --mime-type -b "$src" | grep -q '^image/' || {
        log "AVISO" "$id não é imagem. Descartado."
        return 1
    }
    read -r res why < <(check_quality "$src")
    if [[ "$res" != "OK" ]]; then
        log "AVISO" "Rejeitado $id: $why"
        return 1
    fi
    mind="$(python3 "$DHASH" --min-dist "$src" "$HASH_FILE" 2>/dev/null)"
    [[ "$mind" =~ ^[0-9]+$ ]] || mind=64
    if ((mind <= DUP_HAMMING)); then
        log "AVISO" "Near-duplicata (dHash=$mind): descartado $id"
        return 1
    fi
    mv -f "$src" "$dest"
    chmod 644 "$dest" 2>/dev/null || true
    h="$(python3 "$DHASH" "$dest" 2>/dev/null)"
    [[ "$h" =~ ^[0-9]+$ ]] || h=0
    printf '%s\t%s\n' "$(basename "$dest")" "$h" >> "$HASH_FILE"
    [[ -n "${id:-}" ]] && echo "$id" >> "$IDS_FILE"
    printf '%s\t%s\t%s\n' "$(basename "$dest")" "$origin" "$(date '+%Y-%m-%d %H:%M:%S')" >> "$SOURCES_FILE"
    make_prep "$dest" >/dev/null 2>&1 || true
    log "OK" "Aceito $id ($origin) → $(basename "$dest")"
    return 0
}

# --------------------------------------------------------------------------
# Fontes de download
# --------------------------------------------------------------------------
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
    src_idx=$((src_idx % 5))

    local n_themes=${#THEMES[@]}
    local theme=""
    ((n_themes > 0)) && theme="${THEMES[$((theme_idx % n_themes))]}"
    local wd_sort wd_range broad wd_desc
    IFS='|' read -r wd_sort wd_range broad wd_desc < <(wh_strategy "$src_idx")

    local q_label="${theme:-—}"
    ((broad)) && q_label="—"
    [[ "$theme" == "-" ]] && q_label="—"
    log "INFO" "=== Wallhaven (${wd_desc} | >=${ATLEAST} ${RATIOS} | SFW | tema: '${q_label}') ==="

    local tmp candidates page code url attempt total
    tmp="$(mktemp)"
    candidates="$(mktemp)"
    total=0
    for attempt in 0 1; do
        : > "$candidates"
        for ((page = 1; page <= PAGES_PER_RUN; page++)); do
            url="${api}?sorting=${wd_sort}&order=desc&atleast=${ATLEAST}&ratios=${RATIOS}&categories=${CATEGORIES}&purity=${PURITY}"
            [[ "$wd_sort" == "toplist" && -n "$wd_range" ]] && url+="&topRange=${wd_range}"
            if ((broad == 0)) && [[ -n "$theme" && "$theme" != "-" ]]; then
                url+="&q=${theme// /+}"
            fi
            url+="&page=${page}"
            log "INFO" "Página $page ..."
            code="$(curl -s -m 30 -A "$USER_AGENT" -o "$tmp" -w '%{http_code}' "$url")" || {
                log "AVISO" "Rede falhou p.$page"
                sleep "$API_SLEEP"
                continue
            }
            [[ "$code" != "200" ]] && {
                log "AVISO" "HTTP $code p.$page"
                sleep "$API_SLEEP"
                continue
            }
            jq -r '.data[]? | [.id, (.favorites|tostring), .path, .file_type] | @tsv' "$tmp" >> "$candidates" 2>/dev/null
            sleep "$API_SLEEP"
        done
        total=$(wc -l < "$candidates" | tr -d ' ')
        ((total > 0)) && break
        if ((attempt == 0 && broad == 0)) && [[ -n "$theme" && "$theme" != "-" ]]; then
            log "AVISO" "Sem resultados com o tema '${theme}'; repetindo a estratégia sem 'q'."
            broad=1
        else
            break
        fi
    done
    if [[ "$total" -eq 0 ]]; then
        log "AVISO" "Wallhaven sem resultados."
        rm -f "$tmp" "$candidates"
        echo "$(((src_idx + 1) % 5))" > "$SOURCE_IDX_FILE"
        ((n_themes > 0)) && echo "$(((theme_idx + 1) % n_themes))" > "$THEME_IDX_FILE"
        return 0
    fi
    log "INFO" "$total candidatos. Baixando até $DOWNLOAD_LIMIT novos..."
    local new_count=0 id path ftype ext dest td
    while IFS=$'\t' read -r id _ path ftype; do
        ((new_count >= DOWNLOAD_LIMIT)) && break
        [[ -z "${path:-}" ]] && continue
        [[ -f "$IDS_FILE" ]] && grep -qxF "$id" "$IDS_FILE" && continue
        case "$ftype" in
            image/jpeg) ext=jpg ;;
            image/png) ext=png ;;
            image/webp) ext=webp ;;
            *) continue ;;
        esac
        dest="$IMG_DIR/wallhaven-${id}.${ext}"
        [[ -f "$dest" ]] && continue
        td="$(mktemp --suffix=".${ext}")"
        if curl -fsSL -m 60 -A "$USER_AGENT" -o "$td" "$path"; then
            if accept_image "$td" "$dest" "$id" "wallhaven"; then
                new_count=$((new_count + 1))
            else
                rm -f "$td"
            fi
        else
            log "AVISO" "Falha download $id"
            rm -f "$td"
        fi
        sleep 1
    done < <(sort -t $'\t' -k2 -nr "$candidates")
    log "INFO" "Wallhaven: $new_count nova(s)."
    echo "$(((src_idx + 1) % 5))" > "$SOURCE_IDX_FILE"
    ((n_themes > 0)) && echo "$(((theme_idx + 1) % n_themes))" > "$THEME_IDX_FILE"
    rm -f "$tmp" "$candidates"
    return 0
}

do_download_reddit() {
    ((REDDIT_ENABLED)) || return 0
    require_tools
    log "INFO" "=== Reddit r/wallpapers (top/month, até $REDDIT_LIMIT) ==="
    local url="https://www.reddit.com/r/wallpapers/top.json?t=month&limit=50"
    local json rss candidates code
    json="$(mktemp)"
    rss="$(mktemp)"
    candidates="$(mktemp)"
    code="$(curl -s -m 30 -A "$USER_AGENT" -o "$json" -w '%{http_code}' "$url")" || {
        log "AVISO" "Rede falhou (Reddit)."
        rm -f "$json" "$rss" "$candidates"
        return 0
    }
    local re='^https://(i\.redd\.it|i\.imgur\.com)/.+\.(jpe?g|png|webp)(\?.*)?$'
    if [[ "$code" == "200" ]]; then
        jq -r --arg re "$re" '.data.children[]?.data | select(.url | test($re; "i")) | [.id, (.ups//0|tostring), .url] | @tsv' \
            "$json" 2>/dev/null | sort -t $'\t' -k2 -nr > "$candidates"
    else
        log "AVISO" "Reddit JSON HTTP $code; tentando RSS."
        local rss_url="https://www.reddit.com/r/wallpapers/top.rss?t=month"
        local rss_code
        sleep 2
        rss_code="$(curl -s -m 30 --retry 2 --retry-delay 5 --retry-all-errors \
            -A "$USER_AGENT_MOZILLA" -o "$rss" -w '%{http_code}' "$rss_url")" || rss_code=000
        if [[ "$rss_code" != "200" ]]; then
            log "AVISO" "Reddit RSS HTTP $rss_code"
            rm -f "$json" "$rss" "$candidates"
            return 0
        fi
        python3 - "$rss" <<'PY' > "$candidates"
import re
import sys
import xml.etree.ElementTree as ET

NS = {"a": "http://www.w3.org/2005/Atom"}
IMG_RE = re.compile(
    r"https://(?:i\.redd\.it|i\.imgur\.com)/[^\s\"'<>]+?\.(?:jpe?g|png|webp)(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)
try:
    root = ET.parse(sys.argv[1]).getroot()
except Exception:
    sys.exit(0)
for entry in root.findall("a:entry", NS):
    link = entry.find("a:link", NS)
    href = link.get("href", "") if link is not None else ""
    if "/comments/" not in href:
        continue
    mid = href.split("/comments/", 1)[1].split("/", 1)[0]
    content = entry.find("a:content", NS)
    text = content.text if content is not None and content.text else ""
    m = IMG_RE.search(text)
    if mid and m:
        print(f"{mid}\t0\t{m.group(0)}")
PY
    sort -t $'\t' -k1 "$candidates" -o "$candidates"
    rm -f "$rss" "$json"
fi
local total
if [[ "$code" == "200" ]]; then
    log "INFO" "Reddit via JSON."
else
    log "INFO" "Reddit via RSS (JSON indisponível)."
fi
total="$(wc -l < "$candidates" | tr -d ' ')"
if ((total == 0)); then
    log "AVISO" "Reddit sem links diretos de imagem."
    rm -f "$json" "$rss" "$candidates"
    return 0
fi
log "INFO" "$total link(s) diretos. Baixando até $REDDIT_LIMIT..."
local new_count=0 id u ext dest td
while IFS=$'\t' read -r id _ u; do
    ((new_count >= REDDIT_LIMIT)) && break
    [[ -z "${u:-}" || -z "${id:-}" ]] && continue
    id="reddit-${id}"
    [[ -f "$IDS_FILE" ]] && grep -qxF "$id" "$IDS_FILE" && continue
    u="${u%%\?*}"
    ext="${u##*.}"
    ext="${ext,,}"
    case "$ext" in
        jpg | jpeg) ext=jpg ;;
        png) ext=png ;;
        webp) ext=webp ;;
        *) continue ;;
    esac
    dest="$IMG_DIR/${id}.${ext}"
    [[ -f "$dest" ]] && continue
    td="$(mktemp --suffix=".${ext}")"
    if curl -fsSL -m 60 -A "$USER_AGENT" -o "$td" "$u"; then
        if accept_image "$td" "$dest" "$id" "reddit"; then
            new_count=$((new_count + 1))
        else
            rm -f "$td"
        fi
    else
        log "AVISO" "Falha download $id"
        rm -f "$td"
    fi
    sleep 1
done < "$candidates"
rm -f "$json" "$rss" "$candidates"
log "INFO" "Reddit: $new_count nova(s)."
return 0
}

do_download_bing() {
    ((BING_ENABLED)) || return 0
    require_tools
    log "INFO" "=== Bing Imagem do Dia (UHD, $BING_DAYS dias) ==="
    local url="https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=${BING_DAYS}&mkt=en-US&uhd=1&uhdwidth=3840&uhdheight=2160"
    local json code
    json="$(mktemp)"
    code="$(curl -s -m 30 -A "$USER_AGENT" -o "$json" -w '%{http_code}' "$url")"
    [[ "$code" != "200" ]] && {
        log "AVISO" "Bing HTTP $code"
        rm -f "$json"
        return 0
    }
    local new_count=0 date base full ext dest td
    while IFS=$'\t' read -r date base; do
        ((new_count >= BING_LIMIT)) && break
        [[ -z "$base" ]] && continue
        full="https://www.bing.com${base}"
        case "$base" in
            *.png) ext=png ;;
            *) ext=jpg ;;
        esac
        dest="$IMG_DIR/bing-${date}.${ext}"
        [[ -f "$dest" ]] && continue
        td="$(mktemp --suffix=".${ext}")"
        if curl -fsSL -m 60 -A "$USER_AGENT" -o "$td" "$full"; then
            if accept_image "$td" "$dest" "bing-$date" "bing"; then
                new_count=$((new_count + 1))
            else
                rm -f "$td"
            fi
        else
            log "AVISO" "Falha Bing $date"
            rm -f "$td"
        fi
        sleep 1
    done < <(jq -r '.images[]? | [.startdate, .url] | @tsv' "$json" 2>/dev/null)
    rm -f "$json"
    log "INFO" "Bing: $new_count nova(s)."
    return 0
}

do_download_picsum() {
    ((PICSUM_ENABLED)) || return 0
    require_tools
    log "INFO" "=== Lorem Picsum (aleatório, até $PICSUM_LIMIT) ==="
    local new_count=0 dest td random_id i
    for ((i = 0; i < PICSUM_LIMIT; i++)); do
        ((new_count >= PICSUM_LIMIT)) && break
        random_id=$RANDOM
        dest="$IMG_DIR/picsum-${random_id}.jpg"
        [[ -f "$dest" ]] && continue
        td="$(mktemp --suffix=".jpg")"
        if curl -fsSL -m 60 -A "$USER_AGENT" -o "$td" "https://picsum.photos/2560/1440?random=${random_id}"; then
            if accept_image "$td" "$dest" "picsum-${random_id}" "picsum"; then
                new_count=$((new_count + 1))
            else
                rm -f "$td"
            fi
        else
            log "AVISO" "Falha Picsum download"
            rm -f "$td"
        fi
        sleep 1
    done
    log "INFO" "Lorem Picsum: $new_count nova(s)."
    return 0
}

do_download() {
    local source="${1:-all}"
    case "$source" in
        all | wallhaven | reddit | bing | picsum) ;;
        *) die "Fonte inválida: '$source' (use: all|wallhaven|reddit|bing|picsum)" ;;
    esac
    rotate_log
    log "INFO" "=== Download ($source) ==="
    if [[ "$source" == "all" || "$source" == "wallhaven" ]]; then do_download_wallhaven; fi
    if [[ "$source" == "all" || "$source" == "reddit" ]]; then do_download_reddit; fi
    if [[ "$source" == "all" || "$source" == "bing" ]]; then do_download_bing; fi
    if [[ "$source" == "all" || "$source" == "picsum" ]]; then do_download_picsum; fi
    prune_images
    do_maintenance
    log "INFO" "Total na coleção: $(count_images)."
}

# --------------------------------------------------------------------------
# Disco / armazenamento
# --------------------------------------------------------------------------
_dir_used_mb() {
    local v
    v="$(du -sm "$IMG_DIR" 2>/dev/null | awk 'NR==1{print $1+0}')"
    printf '%s\n' "${v:-0}"
}

_free_mb() {
    local v
    v="$(df -Pm "$DATA_DIR" 2>/dev/null | awk 'NR==2{print $4+0}')"
    printf '%s\n' "${v:-0}"
}

_check_free_space() {
    local free
    free="$(_free_mb)"
    [[ "$free" =~ ^[0-9]+$ ]] || return 0
    if ((free < 500)); then
        log "AVISO" "Espaço livre baixo em $DATA_DIR: ${free} MB (< 500 MB)."
    fi
    return 0
}

# --------------------------------------------------------------------------
# Rotação de log
# --------------------------------------------------------------------------
rotate_log() {
    _LOG_ROTATED=0
    [[ -f "$LOG_FILE" ]] || return 0
    local maxb size
    maxb=$((LOG_MAX_KB * 1024))
    size=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
    ((size > maxb)) || return 0
    rm -f "$LOG_FILE.1"
    if mv "$LOG_FILE" "$LOG_FILE.1" 2>/dev/null; then
        : > "$LOG_FILE"
        chmod 644 "$LOG_FILE" 2>/dev/null || true
        _LOG_ROTATED=1
        log "INFO" "Log rotacionado: carousel.log → carousel.log.1 (${size} bytes)."
    fi
    return 0
}

# --------------------------------------------------------------------------
# Remoção de imagens e higiene de estado
# --------------------------------------------------------------------------
# Remove a primeira linha (campo 1 exato) do arquivo de estado, via awk + mv atômico.
_drop_state_line() {
    local file="$1" name="$2" tmp
    [[ -f "$file" ]] || return 0
    tmp="$(mktemp "${file}.XXXXXX" 2>/dev/null)" || return 1
    if awk -F'\t' -v n="$name" '$1 != n' "$file" > "$tmp" 2>/dev/null; then
        chmod --reference="$file" "$tmp" 2>/dev/null || chmod 644 "$tmp" 2>/dev/null || true
        mv -f "$tmp" "$file" || {
            rm -f "$tmp"
            return 1
        }
    else
        rm -f "$tmp"
        return 1
    fi
    return 0
}

# Remove master + cache -screen + linhas de sources/hashes. Preserva ids e features.
_remove_master() {
    local f="$1" name ext
    name="$(basename "$f")"
    [[ "$name" == *-screen.* ]] && return 1
    ext="${name##*.}"
    rm -f "$IMG_DIR/$name" "$IMG_DIR/${name%.*}-screen.${ext}"
    _drop_state_line "$HASH_FILE" "$name" || true
    _drop_state_line "$SOURCES_FILE" "$name" || true
    return 0
}

# Preenche o array associativo passado por nome (nameref) com imagens protegidas.
# $2=all (like + últimas 168h) ou likes (só like efetivo, sem block).
_prune_protected() {
    local -n _out="$1"
    local _mode="${2:-all}"
    local _f _ts _name _cutoff _epoch
    if [[ -f "$SCORES_FILE" ]]; then
        while IFS= read -r _f; do
            [[ -n "$_f" ]] && _out["$_f"]=1
        done < <(awk -F'\t' '
            $3=="like"{l[$1]++}
            $3=="unlike"{if(l[$1]>0)l[$1]--}
            $3=="block"{b[$1]=1}
            $3=="unblock"{b[$1]=0}
            {seen[$1]=1}
            END{for(f in seen) if(l[f]>0 && !b[f]) print f}
        ' "$SCORES_FILE" 2>/dev/null)
    fi
    [[ "$_mode" == "likes" ]] && return 0
    if [[ -f "$HISTORY_FILE" ]]; then
        _cutoff=$(( $(date +%s) - 168 * 3600 ))
        while IFS=$'\t' read -r _ts _name; do
            [[ -z "${_name:-}" ]] && continue
            _epoch=$(date -d "$_ts" +%s 2>/dev/null) || continue
            if ((_epoch >= _cutoff)); then
                _out["$_name"]=1
            fi
        done < "$HISTORY_FILE"
    fi
    return 0
}

# Imprime nomes do mais descartável ao menos descartável (score.py). Falha=1.
_prune_candidate_names() {
    local count="$1" out rc=0
    [[ -f "$SCORE" ]] || return 1
    out="$(python3 "$SCORE" prune-candidates --count "$count" 2>>"$LOG_FILE")" || rc=$?
    ((rc == 0)) || return 1
    [[ -n "$out" ]] || return 1
    printf '%s\n' "$out"
}

prune_images() {
    local total used_mb target over=0 reason="" floor
    total=$(count_images)
    if ((total == 0)); then
        _check_free_space
        return 0
    fi
    used_mb="$(_dir_used_mb)"
    target=$MAX_IMAGES
    ((total < target)) && target=$total

    if ((total > MAX_IMAGES)); then
        reason="limite de imagens: $total > $MAX_IMAGES"
    fi
    if ((used_mb > MAX_STORAGE_MB)); then
        local avg over_mb extra
        over_mb=$((used_mb - MAX_STORAGE_MB))
        avg=$((used_mb / total))
        ((avg < 1)) && avg=1
        extra=$(((over_mb + avg - 1) / avg))
        ((extra < 1)) && extra=1
        target=$((target - extra))
        reason="${reason:+$reason; }armazenamento: ${used_mb} MB > ${MAX_STORAGE_MB} MB"
    fi
    floor=10
    ((MAX_IMAGES < floor)) && floor=$MAX_IMAGES
    ((total < floor)) && floor=$total
    ((target < floor)) && target=$floor
    over=$((total - target))
    if ((over <= 0)); then
        _check_free_space
        return 0
    fi

    log "INFO" "Poda necessária: $reason (alvo $target, remover $over)."
    local -A prot=()
    _prune_protected prot all
    local -a cand=()
    mapfile -t cand < <(_prune_candidate_names $((over * 4)))
    local via="score.py prune-candidates"
    if ((${#cand[@]} == 0)); then
        via="fallback mtime"
        log "AVISO" "prune-candidates indisponível/vazio; poda por mtime."
    fi

    local removed=0 name
    local -A done_names=()
    local c
    for c in "${cand[@]}"; do
        ((removed >= over)) && break
        [[ -z "$c" ]] && continue
        name="$(basename "${c%$'\r'}")"
        [[ -z "$name" ]] && continue
        [[ -e "$IMG_DIR/$name" ]] || continue
        [[ -n "${prot[$name]:-}" ]] && continue
        [[ -n "${done_names[$name]:-}" ]] && continue
        if _remove_master "$IMG_DIR/$name"; then
            done_names["$name"]=1
            removed=$((removed + 1))
        fi
    done

    if ((removed < over)); then
        local mt fp
        while IFS=$'\t' read -r mt fp; do
            ((removed >= over)) && break
            [[ -z "$fp" ]] && continue
            name="$(basename "$fp")"
            [[ -n "${done_names[$name]:-}" ]] && continue
            [[ -n "${prot[$name]:-}" ]] && continue
            if _remove_master "$fp"; then
                done_names["$name"]=1
                removed=$((removed + 1))
            fi
        done < <(_list_masters_mtime)
    fi

    local alive removed2=0
    alive=$(count_images)
    if ((alive > target)); then
        local -A likes=()
        _prune_protected likes likes
        log "AVISO" "Ainda $alive > alvo $target após 1ª passada; removendo as mais antigas (likes preservados)."
        local mt fp
        while IFS=$'\t' read -r mt fp; do
            ((alive <= target)) && break
            [[ -z "$fp" ]] && continue
            name="$(basename "$fp")"
            [[ -n "${likes[$name]:-}" ]] && continue
            if _remove_master "$fp"; then
                alive=$((alive - 1))
                removed2=$((removed2 + 1))
            fi
        done < <(_list_masters_mtime)
        ((removed2 > 0)) && log "AVISO" "Segunda passada: $removed2 removida(s) apesar de exibição recente."
    fi

    local final
    final=$(count_images)
    log "INFO" "Poda concluída ($via): $((removed + removed2)) de $total removida(s); restam $final (alvo $target)."
    if ((final > target)); then
        log "AVISO" "Alvo não atingido: $final > $target (likes preservados)."
    fi
    _check_free_space
    return 0
}

# Filtra um arquivo de estado (campo 1 = nome) mantendo só masters vivos.
# Ecoa o número de linhas removidas.
_filter_state_alive() {
    local file="$1" map="$2" tmp before after
    [[ -f "$file" ]] || {
        echo 0
        return 0
    }
    before=$(wc -l < "$file" 2>/dev/null | tr -d ' ') || before=0
    tmp="$(mktemp "${file}.XXXXXX" 2>/dev/null)" || {
        echo 0
        return 0
    }
    if ! awk -F'\t' -v mapfile="$map" '
            BEGIN { while ((getline l < mapfile) > 0) if (l != "") alive[l] = 1 }
            $1 in alive
        ' "$file" > "$tmp" 2>/dev/null; then
        rm -f "$tmp"
        echo 0
        return 0
    fi
    chmod --reference="$file" "$tmp" 2>/dev/null || chmod 644 "$tmp" 2>/dev/null || true
    mv -f "$tmp" "$file" || {
        rm -f "$tmp"
        echo 0
        return 0
    }
    after=$(wc -l < "$file" 2>/dev/null | tr -d ' ') || after=0
    echo $((before - after))
}

do_maintenance() {
    local want_prep=0 a
    for a in "$@"; do
        case "$a" in
            --prep) want_prep=1 ;;
            "") ;;
            *) die "Opção desconhecida para maintenance: $a (use --prep)" ;;
        esac
    done
    rotate_log
    local hk_ok=0 hk_summary="" removed_src=0 removed_hash=0
    if [[ -f "$SCORE" ]]; then
        local out rc=0
        out="$(python3 "$SCORE" housekeeping --json 2>>"$LOG_FILE")" || rc=$?
        if ((rc == 0)) && [[ -n "$out" ]] && jq -e . >/dev/null 2>&1 <<< "$out"; then
            hk_ok=1
            hk_summary="$(jq -c . <<< "$out" 2>/dev/null | cut -c1-400)"
        else
            log "AVISO" "housekeeping indisponível (rc=$rc); limpando sources/hashes localmente."
        fi
    fi
    local map
    map="$(mktemp)" || die "Não foi possível criar arquivo temporário."
    list_masters | sed 's|.*/||' > "$map" 2>/dev/null || true
    removed_src="$(_filter_state_alive "$SOURCES_FILE" "$map")"
    removed_hash="$(_filter_state_alive "$HASH_FILE" "$map")"
    rm -f "$map"
    if ((want_prep)); then
        _prep_all 1 0
    fi
    local rot="nao"
    ((_LOG_ROTATED)) && rot="sim"
    local prep_txt="nao"
    ((want_prep)) && prep_txt="sim"
    log "INFO" "Maintenance: housekeeping=$([[ $hk_ok -eq 1 ]] && echo sim || echo nao); sources -${removed_src} linha(s); hashes -${removed_hash} linha(s); log_rotacionado=$rot; prep=$prep_txt."
    ((hk_ok)) && log "INFO" "Housekeeping (score.py): ${hk_summary}"
    return 0
}

# --------------------------------------------------------------------------
# Aplicação, rolagem e notificações
# --------------------------------------------------------------------------
apply_wallpaper() {
    local img="$1"
    [[ -z "${img:-}" ]] && return 1
    if _have plasma-apply-wallpaperimage; then
        if plasma-apply-wallpaperimage "$img" >/dev/null 2>&1; then
            return 0
        fi
        log "AVISO" "plasma-apply-wallpaperimage falhou; tentando D-Bus."
    fi
    if _have dbus-send && [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
        local js js_img
        js_img="${img//\\/\\\\}"
        js_img="${js_img//\'/\\\'}"
        js="string:
var allDesktops = desktops();
for (i=0;i<allDesktops.length;i++) {
    d = allDesktops[i];
    d.wallpaperPlugin = 'org.kde.image';
    d.currentConfigGroup = Array('Wallpaper', 'org.kde.image', 'General');
    d.writeConfig('Image', 'file://${js_img}');
}"
        if dbus-send --session --dest=org.kde.plasmashell --type=method_call \
            /PlasmaShell org.kde.PlasmaShell.evaluateScript "$js" >/dev/null 2>&1; then
            return 0
        fi
        log "AVISO" "Fallback D-Bus também falhou."
    fi
    return 1
}

current_image() {
    local n
    if [[ -f "$HISTORY_FILE" ]]; then
        n="$(tail -n 1 "$HISTORY_FILE" 2>/dev/null | cut -f2)"
        if [[ -n "$n" && -f "$IMG_DIR/$n" ]]; then
            echo "$n"
            return 0
        fi
    fi
    local -a imgs=()
    mapfile -t imgs < <(list_masters | sort)
    ((${#imgs[@]} == 0)) && return 1
    local idx=0
    [[ -f "$INDEX_FILE" ]] && idx="$(cat "$INDEX_FILE" 2>/dev/null || echo 0)"
    [[ "$idx" =~ ^[0-9]+$ ]] || idx=0
    basename "${imgs[$((idx % ${#imgs[@]}))]}"
}

_source_name() {
    case "$1" in
        wallhaven-*) printf 'Wallhaven' ;;
        reddit-*) printf 'Reddit' ;;
        bing-*) printf 'Bing' ;;
        picsum-*) printf 'Lorem Picsum' ;;
        *) printf 'Outro' ;;
    esac
}

_master_resolution() {
    local r
    r="$(magick identify -ping -format '%wx%h' "$IMG_DIR/$1" 2>/dev/null)" || r=""
    [[ -n "$r" ]] || r="?"
    printf '%s\n' "$r"
}

_notify_supports_actions() {
    local help
    help="$(notify-send --help 2>&1)" || return 1
    [[ "$help" == *"-A"* || "$help" == *"--action"* ]]
}

_notify_plain() {
    local expire="${1:-3000}" msg="${2:-}"
    notify-send --app-name=wallpaper-carousel --icon=preferences-desktop-wallpaper \
        --expire-time="$expire" "Wallpaper Carrossel" "$msg" >/dev/null 2>&1
}

# Executado em background pelo do_notify; nunca bloqueia o rotate.
_notify_with_actions() {
    local name="$1" msg="$2" out rc=0
    out="$(notify-send --app-name=wallpaper-carousel --icon=preferences-desktop-wallpaper \
        --expire-time=15000 -A like=Curtir -A block=Bloquear \
        "Wallpaper Carrossel" "$msg" 2>/dev/null)" || rc=$?
    if ((rc != 0)); then
        _notify_plain 3000 "$msg"
        return 0
    fi
    case "${out:-}" in
        like)
            if python3 "$SCORE" like "$name" >>"$LOG_FILE" 2>&1; then
                log "OK" "Feedback pela notificação: like $name"
            fi
            ;;
        block)
            if python3 "$SCORE" block "$name" >>"$LOG_FILE" 2>&1; then
                log "OK" "Feedback pela notificação: block $name"
            fi
            ;;
    esac
    return 0
}

do_notify() {
    local name="$1" msg
    ((NOTIFY_ENABLED)) || return 0
    if ! _have notify-send; then
        log "AVISO" "notify-send ausente; notificação ignorada."
        return 0
    fi
    msg="$(printf 'Wallpaper: %s\nFonte: %s\nResolução: %s' \
        "$name" "$(_source_name "$name")" "$(_master_resolution "$name")")"
    if ((NOTIFY_ACTIONS)) && _notify_supports_actions; then
        _notify_with_actions "$name" "$msg" >/dev/null 2>&1 &
    else
        _notify_plain 3000 "$msg" >/dev/null 2>&1 &
    fi
    return 0
}

do_rotate() {
    local dry=0 a
    for a in "$@"; do
        case "$a" in
            --dry-run) dry=1 ;;
            "") ;;
            *) die "Opção desconhecida para rotate: $a (use --dry-run)" ;;
        esac
    done
    rotate_log
    local total
    total=$(count_images)
    if ((total == 0)); then
        log "AVISO" "Sem imagens. Rode 'download' primeiro."
        return 0
    fi

    local choice="" rc=0
    if [[ -f "$SCORE" ]]; then
        choice="$(python3 "$SCORE" pick 2>>"$LOG_FILE")" || rc=$?
    fi
    if ((rc == 4)); then
        log "ERRO" "Todas as imagens estão bloqueadas (use 'unblock <arquivo>')."
        return 1
    fi
    if [[ -z "$choice" || ! -f "$IMG_DIR/$choice" ]]; then
        log "AVISO" "score.py indisponível; usando fallback por índice alfabético."
        local -a imgs=()
        mapfile -t imgs < <(list_masters | sort)
        ((${#imgs[@]} == 0)) && {
            log "AVISO" "Sem imagens."
            return 0
        }
        local idx=0
        [[ -f "$INDEX_FILE" ]] && idx="$(cat "$INDEX_FILE" 2>/dev/null || echo 0)"
        [[ "$idx" =~ ^[0-9]+$ ]] || idx=0
        ((idx >= ${#imgs[@]})) && idx=0
        choice="$(basename "${imgs[$idx]}")"
        idx=$(((idx + 1) % ${#imgs[@]}))
        echo "$idx" > "$INDEX_FILE"
    fi

    local current
    current="$(resolve_screen "$IMG_DIR/$choice")"
    if ((dry)); then
        log "DRY-RUN" "Escolheria: $choice (nada aplicado/registrado)"
        if [[ -f "$SCORE" ]]; then
            python3 "$SCORE" pick --explain --top 5 2>/dev/null | sed 's/^/  /' || true
        fi
        return 0
    fi

    if apply_wallpaper "$current"; then
        log "OK" "Carrossel: $(basename "$choice")"
        printf '%s\t%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$choice" >> "$HISTORY_FILE"
        do_notify "$choice"
    else
        log "ERRO" "Não apliquei $(basename "$choice"). Sessão Plasma ativa?"
        return 1
    fi
    return 0
}

# --------------------------------------------------------------------------
# Feedback
# --------------------------------------------------------------------------
# Normaliza referência de imagem: aceita caminho absoluto/relativo e cache
# "-screen" (images/foo-screen.jpg → foo.jpg). Ecoa o nome do master.
_normalize_image_ref() {
    local t="$1" base ext
    [[ -z "$t" ]] && return 1
    base="$(basename "$t")"
    if [[ "$base" == *-screen.* ]]; then
        ext="${base##*.}"
        base="${base%.*}"
        base="${base%-screen}.${ext}"
    elif [[ "$base" == *-screen ]]; then
        base="${base%-screen}"
    fi
    [[ -n "$base" ]] || return 1
    printf '%s\n' "$base"
}

do_feedback() {
    local action="$1" target="${2:-}" name
    [[ -f "$SCORE" ]] || die "Falta $SCORE"
    if [[ -z "$target" ]]; then
        target="$(current_image)" || die "Nenhum wallpaper atual; informe o arquivo."
    fi
    [[ -z "$target" ]] && die "Nenhum wallpaper atual; informe o arquivo."
    name="$(_normalize_image_ref "$target")" || die "Referência de imagem inválida: $target"
    if [[ ! -f "$IMG_DIR/$name" ]]; then
        die "Imagem não encontrada: $name (feedback não registrado)."
    fi
    python3 "$SCORE" "$action" "$name"
}

do_why() {
    [[ -f "$SCORE" ]] || die "Falta $SCORE"
    python3 "$SCORE" pick --explain --top 5
}

# --------------------------------------------------------------------------
# systemd: unidades endurecidas, setup --dry-run e WALLPAPER_SYSTEMD_DIR
# --------------------------------------------------------------------------
_rotate_timer_schedule() {
    local n="$ROTATE_MINUTES"
    if ((n == 60)); then
        printf 'OnCalendar=*:00:00\nPersistent=true\n'
    elif ((n > 0 && n < 60 && 60 % n == 0)); then
        printf 'OnCalendar=*:0/%s\nPersistent=true\n' "$n"
    else
        printf 'OnBootSec=30s\nOnUnitActiveSec=%smin\n' "$n"
    fi
}

_unit_rotate_service() {
    local runner="$SELF_DIR/run.sh"
    cat <<EOF
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
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=%h/.local/share/wallpaper-carousel
ReadWritePaths=$DATA_DIR
TimeoutStartSec=300
Nice=10
IOSchedulingClass=idle
KillMode=process
EOF
}

_unit_rotate_timer() {
    cat <<EOF
[Unit]
Description=Alterna o papel de parede a cada ${ROTATE_MINUTES} min

[Timer]
$(_rotate_timer_schedule)
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF
}

_unit_download_service() {
    local runner="$SELF_DIR/run.sh"
    cat <<EOF
[Unit]
Description=Wallpaper carousel: baixa novos wallpapers (curadoria premium)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$runner download
Environment=HOME=%h
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=%h/.local/share/wallpaper-carousel
ReadWritePaths=$DATA_DIR
TimeoutStartSec=300
Nice=10
IOSchedulingClass=idle
EOF
}

_unit_download_timer() {
    cat <<EOF
[Unit]
Description=Renova a colecao de wallpapers a cada ${DOWNLOAD_EVERY_HOURS}h

[Timer]
OnCalendar=0/${DOWNLOAD_EVERY_HOURS}:15
RandomizedDelaySec=120
AccuracySec=1min
Persistent=true

[Install]
WantedBy=timers.target
EOF
}

_units_target_dir() {
    if [[ -n "$SYSTEMD_OVERRIDE_DIR" ]]; then
        printf '%s\n' "$SYSTEMD_OVERRIDE_DIR"
    else
        printf '%s\n' "$SYSTEMD_USER_DIR"
    fi
}

do_setup() {
    local dry=0 a
    for a in "$@"; do
        case "$a" in
            --dry-run) dry=1 ;;
            "") ;;
            *) die "Opção desconhecida para setup: $a (use --dry-run)" ;;
        esac
    done
    local runner="$SELF_DIR/run.sh"
    local target_dir
    target_dir="$(_units_target_dir)"
    [[ -f "$runner" ]] || die "Falta $runner (entrypoint dos timers)."

    local svc_rotate timer_rotate svc_download timer_download
    svc_rotate="$(_unit_rotate_service)"
    timer_rotate="$(_unit_rotate_timer)"
    svc_download="$(_unit_download_service)"
    timer_download="$(_unit_download_timer)"

    if ((dry)); then
        echo "=== (dry-run) Unidades systemd de usuário — nada será gravado/ativado ==="
        echo "Diretório alvo: $target_dir"
        echo "ExecStart    : $runner"
        echo "Dados        : $DATA_DIR"
        echo
        printf '%s\n' "--- $SERVICE_ROTATE ---" "$svc_rotate"
        echo
        printf '%s\n' "--- $TIMER_ROTATE ---" "$timer_rotate"
        echo
        printf '%s\n' "--- $SERVICE_DOWNLOAD ---" "$svc_download"
        echo
        printf '%s\n' "--- $TIMER_DOWNLOAD ---" "$timer_download"
        echo
        echo "Validação de caminhos:"
        if [[ -x "$runner" ]]; then
            echo "  run.sh: ok (executável)"
        else
            echo "  run.sh: AVISO (não executável)"
        fi
        if [[ -f "$SELF_DIR/wallpaper-carousel.sh" ]]; then
            echo "  wallpaper-carousel.sh: ok"
        else
            echo "  wallpaper-carousel.sh: AVISO (ausente)"
        fi
        if [[ -w "$DATA_DIR" ]]; then
            echo "  data dir: ok (gravável)"
        else
            echo "  data dir: AVISO (sem permissão de escrita)"
        fi
        echo
        echo "Nada foi gravado nem ativado (dry-run)."
        return 0
    fi

    if ! mkdir -p "$target_dir" 2>/dev/null; then
        die "Não foi possível criar $target_dir."
    fi
    printf '%s\n' "$svc_rotate" > "$target_dir/$SERVICE_ROTATE"
    printf '%s\n' "$timer_rotate" > "$target_dir/$TIMER_ROTATE"
    printf '%s\n' "$svc_download" > "$target_dir/$SERVICE_DOWNLOAD"
    printf '%s\n' "$timer_download" > "$target_dir/$TIMER_DOWNLOAD"
    chmod 644 "$target_dir/$SERVICE_ROTATE" "$target_dir/$TIMER_ROTATE" \
        "$target_dir/$SERVICE_DOWNLOAD" "$target_dir/$TIMER_DOWNLOAD" 2>/dev/null || true
    log "OK" "Unidades gravadas em $target_dir."

    if [[ -n "$SYSTEMD_OVERRIDE_DIR" ]]; then
        log "OK" "WALLPAPER_SYSTEMD_DIR definido: systemctl NÃO foi chamado (modo teste)."
        echo "Unidades gravadas em $target_dir (systemctl não chamado)."
        return 0
    fi

    require_tools
    systemctl --user daemon-reload
    systemctl --user enable --now "$TIMER_ROTATE" >/dev/null 2>&1
    systemctl --user enable --now "$TIMER_DOWNLOAD" >/dev/null 2>&1
    if (($(count_images) == 0)); then
        log "INFO" "Coleção vazia — baixando lote inicial..."
        do_download
    fi
    do_rotate
    log "OK" "Setup concluído. Rotação ${ROTATE_MINUTES}min; download ${DOWNLOAD_EVERY_HOURS}h."
    log "OK" "Imagens em: $IMG_DIR"
    echo
    do_status
}

do_uninstall() {
    local dir
    dir="$(_units_target_dir)"
    if [[ -z "$SYSTEMD_OVERRIDE_DIR" ]]; then
        systemctl --user disable --now "$TIMER_ROTATE" >/dev/null 2>&1
        systemctl --user disable --now "$TIMER_DOWNLOAD" >/dev/null 2>&1
    fi
    rm -f "$dir/$SERVICE_ROTATE" "$dir/$TIMER_ROTATE" \
        "$dir/$SERVICE_DOWNLOAD" "$dir/$TIMER_DOWNLOAD"
    if [[ -z "$SYSTEMD_OVERRIDE_DIR" ]]; then
        systemctl --user daemon-reload >/dev/null 2>&1
    fi
    log "OK" "Timers removidos. Imagens mantidas em $IMG_DIR (use 'clean' p/ removê-las)."
}

# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------
_last_rotation() {
    [[ -f "$HISTORY_FILE" ]] || return 1
    tail -n 1 "$HISTORY_FILE" 2>/dev/null | awk -F'\t' 'NF>=2 && $2!=""{print $2"\t"$1; exit}'
}

_age_from_ts() {
    local ts="$1" epoch now
    epoch=$(date -d "$ts" +%s 2>/dev/null) || return 1
    now=$(date +%s)
    local secs=$((now - epoch))
    ((secs < 0)) && secs=0
    if ((secs < 60)); then
        printf 'há %ds' "$secs"
    elif ((secs < 3600)); then
        printf 'há %dmin' "$((secs / 60))"
    elif ((secs < 86400)); then
        printf 'há %dh %dmin' "$((secs / 3600))" "$(((secs % 3600) / 60))"
    else
        printf 'há %dd %dh' "$((secs / 86400))" "$(((secs % 86400) / 3600))"
    fi
}

_source_distribution() {
    list_masters | awk -F/ '{
        n=$NF
        if (n ~ /^wallhaven-/) s="wallhaven"
        else if (n ~ /^reddit-/) s="reddit"
        else if (n ~ /^bing-/) s="bing"
        else if (n ~ /^picsum-/) s="picsum"
        else s="outro"
        c[s]++
    } END { for (k in c) print k"\t"c[k] }'
}

# Ecoa "total validos desatualizados" para os caches -screen.
_screen_cache_stats() {
    local target pw ph total=0 valid=0 outdated=0
    target="$(screen_resolution)"
    pw="${target%%x*}"
    ph="${target##*x}"
    local -A mm=() cm=()
    local name mt f
    while IFS=$'\t' read -r name mt; do
        [[ -n "$name" ]] && mm["$name"]="$mt"
    done < <(find "$IMG_DIR" -maxdepth 1 -type f \
        \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' \) \
        ! -iname '*-screen.*' -printf '%f\t%T@\n' 2>/dev/null)
    local -a caches=()
    while IFS= read -r f; do
        [[ -n "$f" ]] && caches+=("$f")
    done < <(find "$IMG_DIR" -maxdepth 1 -type f -iname '*-screen.*' 2>/dev/null)
    total=${#caches[@]}
    if ((total == 0)); then
        printf '0 0 0\n'
        return 0
    fi
    while IFS=$'\t' read -r name mt; do
        [[ -n "$name" ]] && cm["$name"]="$mt"
    done < <(find "$IMG_DIR" -maxdepth 1 -type f -iname '*-screen.*' -printf '%f\t%T@\n' 2>/dev/null)
    local out fname w h base cmt mmt
    out="$(magick identify -ping -format '%f\t%w\t%h\n' "${caches[@]}" 2>/dev/null)"
    while IFS=$'\t' read -r fname w h; do
        [[ -z "$fname" ]] && continue
        base="${fname%-screen.*}"
        base="${base}.${fname##*.}"
        cmt="${cm[$fname]:-0}"
        mmt="${mm[$base]:-0}"
        if [[ "$w" == "$pw" && "$h" == "$ph" ]] && awk -v a="$cmt" -v b="$mmt" 'BEGIN{exit !(a+0 >= b+0)}'; then
            valid=$((valid + 1))
        else
            outdated=$((outdated + 1))
        fi
    done <<< "$out"
    printf '%s %s %s\n' "$total" "$valid" "$outdated"
}

do_status() {
    screen_init
    local screen="$_SCREEN_RES" screen_src="$_SCREEN_RES_SOURCE"
    echo "============ Wallpaper Carousel (v${VERSION}) — Status ============"
    echo "Diretório de imagens  : $IMG_DIR"
    echo "Imagens na coleção    : $(count_images) (limite: $MAX_IMAGES)"
    echo "Intervalo carrossel   : $ROTATE_MINUTES min"
    echo "Intervalo download    : $DOWNLOAD_EVERY_HOURS h"
    echo "Contexto dia/noite    : score.py (alvo de luma por horário)"
    echo "Fontes                : Wallhaven rotativo + Reddit + Bing + Lorem Picsum | >=${ATLEAST} ${RATIOS} SFW"
    echo "Qualidade             : bpp >= ${MIN_BPP}; ratio ${MIN_RATIO}-${MAX_RATIO}; teto ${MAX_FILE_SIZE_KB}KB"
    echo "Cache de tela         : $screen ($screen_src) | teto ${MAX_STORAGE_MB}MB | log ${LOG_MAX_KB}KB"
    echo "Dedup                 : dHash Hamming <= ${DUP_HAMMING}; Bing $( [[ ${BING_ENABLED:-1} -eq 1 ]] && echo ON || echo OFF); Reddit $( [[ ${REDDIT_ENABLED:-1} -eq 1 ]] && echo ON || echo OFF); Picsum $( [[ ${PICSUM_ENABLED:-1} -eq 1 ]] && echo ON || echo OFF)"
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

    echo "--- Última troca ---"
    local last name ts
    last="$(_last_rotation)" || last=""
    if [[ -n "$last" ]]; then
        name="${last%%$'\t'*}"
        ts="${last##*$'\t'}"
        echo "  $name ($(_age_from_ts "$ts") — $ts)"
    else
        echo "  (sem histórico de rotação)"
    fi
    echo

    echo "--- Distribuição por fonte ---"
    local src n
    while IFS=$'\t' read -r src n; do
        [[ -z "$src" ]] && continue
        printf '  %-12s %s\n' "$src" "$n"
    done < <(_source_distribution | sort -t $'\t' -k2 -nr)
    echo

    echo "--- Disco e caches ---"
    local used free ctotal cvalid coutd
    used="$(_dir_used_mb)"
    free="$(_free_mb)"
    read -r ctotal cvalid coutd < <(_screen_cache_stats)
    printf '  imagens: %s MB de %s MB (livre no FS: %s MB)\n' "$used" "$MAX_STORAGE_MB" "$free"
    printf '  cache -screen: %s (ok %s, desatualizado(s) %s)\n' "${ctotal:-0}" "${cvalid:-0}" "${coutd:-0}"
    echo

    echo "--- Saúde ---"
    local -a warns=()
    local masters feat_lines src_lines hash_lines hist_lines errs
    masters=$(count_images)
    feat_lines=$(_count_lines "$FEATURES_FILE")
    src_lines=$(_count_lines "$SOURCES_FILE")
    hash_lines=$(_count_lines "$HASH_FILE")
    hist_lines=$(_count_lines "$HISTORY_FILE")
    ((feat_lines > masters)) && warns+=("features.tsv com $((feat_lines - masters)) linha(s) obsoleta(s) — rode maintenance")
    ((src_lines > masters)) && warns+=("sources.tsv com $((src_lines - masters)) linha(s) obsoleta(s) — rode maintenance")
    ((hash_lines > masters)) && warns+=("hashes.tsv com $((hash_lines - masters)) linha(s) obsoleta(s) — rode maintenance")
    ((masters == 0)) && warns+=("nenhuma imagem na coleção — rode download")
    ((free < 500)) && warns+=("espaço livre baixo: ${free} MB")
    ((used > MAX_STORAGE_MB)) && warns+=("uso ${used} MB acima do teto ${MAX_STORAGE_MB} MB — poda no próximo download")
    ((coutd > 0)) && warns+=("${coutd} cache(s) -screen desatualizado(s) — rode prep")
    if command -v systemctl >/dev/null 2>&1; then
        if ! systemctl --user is-active --quiet "$TIMER_ROTATE" 2>/dev/null; then
            warns+=("timer de rotação INATIVO — rode setup")
        fi
        if ! systemctl --user is-active --quiet "$TIMER_DOWNLOAD" 2>/dev/null; then
            warns+=("timer de download INATIVO — rode setup")
        fi
    else
        warns+=("systemctl indisponível — timers não verificados")
    fi
    errs="$(awk -v c="$(date -d '24 hours ago' '+%Y-%m-%d %H:%M:%S')" \
        'index($0,"[ERRO]") && ($1" "$2) >= c' "$LOG_FILE" 2>/dev/null | wc -l | tr -d ' ')"
    [[ "$errs" =~ ^[0-9]+$ ]] || errs=0
    ((errs > 0)) && warns+=("$errs erro(s) no log nas últimas 24h (veja logs/carousel.log)")
    if ((${#warns[@]} == 0)); then
        echo "  ok — sem avisos"
    else
        local w
        for w in "${warns[@]}"; do
            echo "  [AVISO] $w"
        done
    fi
    echo

    echo "--- Timers systemd (usuário) ---"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user list-timers 'wallpaper-carousel-*' --all --no-pager 2>/dev/null || echo "(systemd user indisponível)"
    else
        echo "(systemctl indisponível)"
    fi
    echo
    echo "--- Últimas linhas do log ---"
    tail -n 8 "$LOG_FILE" 2>/dev/null || echo "(sem log ainda)"
}

# --------------------------------------------------------------------------
# Doctor
# --------------------------------------------------------------------------
_json_bool() {
    if [[ "$1" == "1" ]]; then
        printf 'true'
    else
        printf 'false'
    fi
}

do_doctor() {
    local as_json=0 full=0 a
    for a in "$@"; do
        case "$a" in
            --json) as_json=1 ;;
            --full) full=1 ;;
            "") ;;
            *)
                echo "Opção desconhecida para doctor: $a (use --json/--full)" >&2
                return 2
                ;;
        esac
    done
    if ! _have jq; then
        if ((as_json)); then
            printf '{"version":"%s","ok":false,"criticos":1,"avisos":0,"erro":"jq ausente"}\n' "$VERSION"
        else
            echo "CRÍTICO: jq ausente (sudo dnf install jq)"
        fi
        return 1
    fi

    local -a _checks=()
    local criticos=0 avisos=0
    _doctor_chk() {
        local st="$1" id="$2" msg="$3"
        _checks+=("$(jq -cn --arg s "$st" --arg i "$id" --arg m "$msg" '{id:$i,status:$s,msg:$m}')")
        if [[ "$st" == "critico" ]]; then
            criticos=$((criticos + 1))
        elif [[ "$st" == "aviso" ]]; then
            avisos=$((avisos + 1))
        fi
        return 0
    }

    # --- ferramentas ---
    local tools_json="{" first=1 t st
    for t in curl jq file magick python3 flock notify-send plasma-apply-wallpaperimage kscreen-doctor systemctl; do
        st="falta"
        _have "$t" && st="ok"
        ((first)) || tools_json+=","
        tools_json+="\"$t\":\"$st\""
        first=0
    done
    if python3 -c 'import PIL' 2>/dev/null; then
        tools_json+=",\"Pillow\":\"ok\""
    else
        tools_json+=",\"Pillow\":\"falta\""
    fi
    tools_json+="}"
    local core_missing=()
    for t in curl jq file magick python3; do
        _have "$t" || core_missing+=("$t")
    done
    python3 -c 'import PIL' 2>/dev/null || core_missing+=("Pillow")
    if ((${#core_missing[@]} > 0)); then
        _doctor_chk critico "tools" "Ferramentas essenciais ausentes: ${core_missing[*]}"
    else
        _doctor_chk ok "tools" "Ferramentas essenciais presentes"
    fi
    _have plasma-apply-wallpaperimage || _doctor_chk aviso "apply" "plasma-apply-wallpaperimage ausente (rotate pode falhar)"
    _have notify-send || _doctor_chk aviso "notify" "notify-send ausente (notificações desativadas na prática)"

    # --- dados ---
    local writable=0 free_mb used_mb
    [[ -w "$DATA_DIR" ]] && writable=1
    free_mb="$(_free_mb)"
    used_mb="$(_dir_used_mb)"
    if ((writable == 0)); then
        _doctor_chk critico "data_dir" "Sem permissão de escrita em $DATA_DIR"
    else
        _doctor_chk ok "data_dir" "Diretório de dados gravável"
    fi
    if [[ "$free_mb" =~ ^[0-9]+$ ]] && ((free_mb < 500)); then
        _doctor_chk aviso "disk_free" "Espaço livre baixo em $DATA_DIR: ${free_mb} MB"
    fi
    if ((used_mb > MAX_STORAGE_MB)); then
        _doctor_chk aviso "storage" "images/ com ${used_mb} MB acima do teto ${MAX_STORAGE_MB} MB"
    fi

    # --- imagens ---
    local masters=0 master_bytes=0
    local f
    while IFS= read -r f; do
        [[ -z "$f" ]] && continue
        masters=$((masters + 1))
        master_bytes=$((master_bytes + $(stat -c%s "$f" 2>/dev/null || echo 0)))
    done < <(list_masters)
    local -a sample=()
    if ((full)); then
        mapfile -t sample < <(list_masters)
    else
        mapfile -t sample < <(list_masters | head -n 25)
    fi
    local illeg=0 zero=0 sampled=${#sample[@]} dims w h
    for f in "${sample[@]}"; do
        dims="$(magick identify -ping -format '%w %h' "$f" 2>/dev/null)" || dims=""
        if [[ -z "$dims" ]]; then
            illeg=$((illeg + 1))
            continue
        fi
        read -r w h <<< "$dims"
        if ((w == 0 || h == 0)); then
            zero=$((zero + 1))
        fi
    done
    if ((masters == 0)); then
        _doctor_chk aviso "images" "Nenhuma imagem na coleção (rode download)"
    else
        _doctor_chk ok "images" "$masters master(s), $sampled verificado(s)"
    fi
    ((illeg > 0)) && _doctor_chk aviso "images_illegible" "$illeg imagem(ns) ilegível(is) na amostra"
    ((zero > 0)) && _doctor_chk aviso "images_zero" "$zero imagem(ns) com dimensão zero na amostra"

    # --- state ---
    local feat_lines src_lines hash_lines hist_lines feat_obs src_obs hash_obs
    feat_lines=$(_count_lines "$FEATURES_FILE")
    src_lines=$(_count_lines "$SOURCES_FILE")
    hash_lines=$(_count_lines "$HASH_FILE")
    hist_lines=$(_count_lines "$HISTORY_FILE")
    feat_obs=$((feat_lines > masters ? feat_lines - masters : 0))
    src_obs=$((src_lines > masters ? src_lines - masters : 0))
    hash_obs=$((hash_lines > masters ? hash_lines - masters : 0))
    if ((feat_obs > 0)); then
        _doctor_chk aviso "state_features" "features.tsv com $feat_obs linha(s) obsoleta(s) (rode maintenance)"
    fi
    if ((src_obs > 0)); then
        _doctor_chk aviso "state_sources" "sources.tsv com $src_obs linha(s) obsoleta(s) (rode maintenance)"
    fi
    if ((hash_obs > 0)); then
        _doctor_chk aviso "state_hashes" "hashes.tsv com $hash_obs linha(s) obsoleta(s) (rode maintenance)"
    fi
    if ((feat_obs == 0 && src_obs == 0 && hash_obs == 0)); then
        _doctor_chk ok "state" "features/sources/hashes consistentes com $masters master(s)"
    fi

    # --- tela/cache ---
    screen_init
    local screen="$_SCREEN_RES" screen_src="$_SCREEN_RES_SOURCE"
    local ctotal cvalid coutd
    read -r ctotal cvalid coutd < <(_screen_cache_stats)
    ctotal=${ctotal:-0}
    cvalid=${cvalid:-0}
    coutd=${coutd:-0}
    _doctor_chk ok "screen" "Resolução detectada: $screen ($screen_src)"
    if ((coutd > 0)); then
        _doctor_chk aviso "cache" "$coutd cache(s) -screen desatualizado(s)/estranho(s) (rode prep)"
    else
        _doctor_chk ok "cache" "$ctotal cache(s) -screen válidos"
    fi

    # --- sessão ---
    local dbus=0 wayland=0 xdg=0 plasma=0
    [[ -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]] && dbus=1
    [[ -n "${WAYLAND_DISPLAY:-}" ]] && wayland=1
    [[ -n "${XDG_RUNTIME_DIR:-}" ]] && xdg=1
    _have plasma-apply-wallpaperimage && plasma=1
    if ((dbus == 0)); then
        _doctor_chk aviso "session_dbus" "DBUS_SESSION_BUS_ADDRESS não definido (rotate pode falhar)"
    fi
    if ((wayland == 0)); then
        _doctor_chk aviso "session_wayland" "WAYLAND_DISPLAY não definido (sessão Wayland não detectada)"
    fi
    ((dbus == 1 && wayland == 1)) && _doctor_chk ok "session" "Sessão gráfica detectada (D-Bus + Wayland)"

    # --- systemd ---
    local sysd=0 rot=0 dl=0 next_rotate="" last_name="" last_age="null"
    if _have systemctl; then
        if timeout 5 systemctl --user is-active --quiet "$TIMER_ROTATE" 2>/dev/null; then
            rot=1
        fi
        if timeout 5 systemctl --user is-active --quiet "$TIMER_DOWNLOAD" 2>/dev/null; then
            dl=1
        fi
        if timeout 5 systemctl --user list-timers "$TIMER_ROTATE" --no-pager >/dev/null 2>&1; then
            sysd=1
            next_rotate="$(timeout 5 systemctl --user list-timers 'wallpaper-carousel-*' --no-pager 2>/dev/null |
                awk '$5=="wallpaper-carousel-rotate.timer"{print $1" "$2" "$3; exit}')"
        fi
    fi
    if ((sysd == 0)); then
        _doctor_chk aviso "systemd" "systemd/DBus de usuário indisponível; timers não verificados"
    elif ((rot == 0)); then
        _doctor_chk aviso "systemd_rotate" "timer de rotação INATIVO (timers parados?)"
    else
        _doctor_chk ok "systemd_rotate" "timer de rotação ativo"
    fi
    if ((sysd == 1 && dl == 0)); then
        _doctor_chk aviso "systemd_download" "timer de download INATIVO"
    fi
    local last
    last="$(_last_rotation)" || last=""
    if [[ -n "$last" ]]; then
        last_name="${last%%$'\t'*}"
        local last_ts="${last##*$'\t'}" last_epoch now
        last_epoch=$(date -d "$last_ts" +%s 2>/dev/null) || last_epoch=0
        now=$(date +%s)
        if ((last_epoch > 0)); then
            last_age=$(((now - last_epoch) / 60))
            ((last_age < 0)) && last_age=0
        fi
    else
        _doctor_chk aviso "rotation" "sem histórico de rotação (state/history vazio)"
    fi

    # --- log ---
    local errs=0
    local -a err_samples=()
    if [[ -f "$LOG_FILE" ]]; then
        local cutoff line
        cutoff="$(date -d '24 hours ago' '+%Y-%m-%d %H:%M:%S')"
        while IFS= read -r line; do
            errs=$((errs + 1))
            ((${#err_samples[@]} < 3)) && err_samples+=("$(printf '%s' "$line" | cut -c1-140)")
        done < <(awk -v c="$cutoff" 'index($0,"[ERRO]") && ($1" "$2) >= c' "$LOG_FILE" 2>/dev/null)
    fi
    if ((errs > 0)); then
        _doctor_chk aviso "log_errors" "$errs erro(s) no log nas últimas 24h"
    else
        _doctor_chk ok "log_errors" "sem erros no log nas últimas 24h"
    fi

    local checks_json
    checks_json="$(printf '%s\n' "${_checks[@]}" | jq -s '.' 2>/dev/null)"
    [[ -n "$checks_json" ]] || checks_json='[]'
    local samples_json='[]'
    if ((${#err_samples[@]} > 0)); then
        samples_json="$(printf '%s\n' "${err_samples[@]}" | jq -R . | jq -s '.')"
    fi

    if ((as_json)); then
        jq -n \
            --arg version "$VERSION" \
            --argjson criticos "$criticos" \
            --argjson avisos "$avisos" \
            --argjson full "$(_json_bool "$full")" \
            --argjson tools "$tools_json" \
            --argjson checks "$checks_json" \
            --arg data_dir "$DATA_DIR" \
            --argjson writable "$(_json_bool "$writable")" \
            --argjson free_mb "$free_mb" \
            --argjson used_mb "$used_mb" \
            --argjson max_storage_mb "$MAX_STORAGE_MB" \
            --argjson masters "$masters" \
            --argjson master_bytes "$master_bytes" \
            --argjson sampled "$sampled" \
            --argjson illegible "$illeg" \
            --argjson zero "$zero" \
            --argjson features "$feat_lines" \
            --argjson sources "$src_lines" \
            --argjson hashes "$hash_lines" \
            --argjson history "$hist_lines" \
            --argjson features_obsolete "$feat_obs" \
            --argjson sources_obsolete "$src_obs" \
            --argjson hashes_obsolete "$hash_obs" \
            --arg screen "$screen" \
            --arg screen_source "$screen_src" \
            --argjson caches_total "$ctotal" \
            --argjson caches_valid "$cvalid" \
            --argjson caches_outdated "$coutd" \
            --argjson session_dbus "$(_json_bool "$dbus")" \
            --argjson session_wayland "$(_json_bool "$wayland")" \
            --argjson session_xdg "$(_json_bool "$xdg")" \
            --argjson session_plasma "$(_json_bool "$plasma")" \
            --argjson systemd_available "$(_json_bool "$sysd")" \
            --argjson rotate_active "$(_json_bool "$rot")" \
            --argjson download_active "$(_json_bool "$dl")" \
            --arg next_rotate "$next_rotate" \
            --arg last_rotation "$last_name" \
            --argjson last_rotation_age_min "$last_age" \
            --argjson log_errors "$errs" \
            --argjson log_samples "$samples_json" \
            '{
                version: ($version | tonumber),
                ok: ($criticos == 0),
                criticos: $criticos,
                avisos: $avisos,
                full: $full,
                tools: $tools,
                data_dir: {
                    path: $data_dir,
                    writable: $writable,
                    free_mb: $free_mb,
                    used_mb: $used_mb,
                    max_storage_mb: $max_storage_mb
                },
                images: {
                    masters: $masters,
                    bytes: $master_bytes,
                    checked: $sampled,
                    illegible: $illegible,
                    zero: $zero
                },
                state: {
                    features: $features,
                    sources: $sources,
                    hashes: $hashes,
                    history: $history,
                    features_obsolete: $features_obsolete,
                    sources_obsolete: $sources_obsolete,
                    hashes_obsolete: $hashes_obsolete
                },
                screen: {
                    resolution: $screen,
                    source: $screen_source,
                    caches: {
                        total: $caches_total,
                        valid: $caches_valid,
                        outdated: $caches_outdated
                    }
                },
                session: {
                    dbus: $session_dbus,
                    wayland: $session_wayland,
                    xdg_runtime_dir: $session_xdg,
                    plasma_apply: $session_plasma
                },
                systemd: {
                    available: $systemd_available,
                    rotate_timer_active: $rotate_active,
                    download_timer_active: $download_active,
                    next_rotate: $next_rotate,
                    last_rotation: {
                        name: $last_rotation,
                        age_min: $last_rotation_age_min
                    }
                },
                log: {
                    errors_24h: $log_errors,
                    samples: $log_samples
                },
                checks: $checks
            }'
        if ((criticos > 0)); then
            return 1
        fi
        return 0
    fi

    # --- saída humana ---
    echo "============ Doctor — Wallpaper Carousel v${VERSION} ============"
    echo "Diretório: $DATA_DIR"
    echo
    echo "--- Ferramentas ---"
    local k v
    echo "$tools_json" | jq -r 'to_entries[] | "  [\(.value)] \(.key)"' 2>/dev/null
    echo
    echo "--- Dados e estado ---"
    local full_txt=""
    ((full)) && full_txt=" (full)"
    printf '  imagens: %s master(s), %s MB, verificado(s): %s%s\n' \
        "$masters" "$used_mb" "$sampled" "$full_txt"
    printf '  state: features %s | sources %s | hashes %s | history %s\n' \
        "$feat_lines" "$src_lines" "$hash_lines" "$hist_lines"
    printf '  espaço livre: %s MB | teto imagens: %s MB\n' "$free_mb" "$MAX_STORAGE_MB"
    echo
    echo "--- Tela e cache ---"
    printf '  resolução: %s (%s)\n' "$screen" "$screen_src"
    printf '  cache -screen: %s (ok %s, desatualizado(s) %s)\n' "$ctotal" "$cvalid" "$coutd"
    echo
    echo "--- Sessão ---"
    printf '  D-Bus: %s | Wayland: %s | XDG_RUNTIME_DIR: %s | plasma-apply: %s\n' \
        "$(_json_bool "$dbus")" "$(_json_bool "$wayland")" "$(_json_bool "$xdg")" "$(_json_bool "$plasma")"
    echo
    echo "--- systemd ---"
    if ((sysd == 0)); then
        echo "  (indisponível — timers não verificados)"
    else
        printf '  rotação ativa: %s | download ativo: %s\n' "$(_json_bool "$rot")" "$(_json_bool "$dl")"
        [[ -n "$next_rotate" ]] && echo "  próximo disparo: $next_rotate"
        if [[ -n "$last_name" ]]; then
            echo "  última rotação: $last_name (${last_age}min atrás)"
        else
            echo "  última rotação: (sem histórico)"
        fi
    fi
    echo
    echo "--- Log (24h) ---"
    if ((errs > 0)); then
        echo "  $errs erro(s):"
        for k in "${err_samples[@]}"; do
            echo "    $k"
        done
    else
        echo "  sem erros nas últimas 24h"
    fi
    echo
    echo "--- Alertas ---"
    local alerts
    alerts="$(jq -r '.[] | select(.status != "ok") | "  [\(.status|ascii_upcase)] \(.msg)"' <<< "$checks_json" 2>/dev/null)"
    if [[ -n "$alerts" ]]; then
        printf '%s\n' "$alerts"
    else
        echo "  nenhum"
    fi
    echo
    echo "--- Resumo ---"
    if ((criticos > 0)); then
        echo "  CRÍTICO: $criticos problema(s) crítico(s), $avisos aviso(s)."
        return 1
    fi
    echo "  OK: 0 crítico(s), $avisos aviso(s)."
    return 0
}

# --------------------------------------------------------------------------
# Clean
# --------------------------------------------------------------------------
do_clean() {
    local n
    n=$(count_images)
    local -a files=()
    local old_nullglob
    old_nullglob="$(shopt -p nullglob 2>/dev/null || true)"
    shopt -s nullglob
    files=("$IMG_DIR"/*.jpg "$IMG_DIR"/*.jpeg "$IMG_DIR"/*.png "$IMG_DIR"/*.webp)
    shopt -u nullglob
    [[ -n "$old_nullglob" ]] && eval "$old_nullglob" >/dev/null 2>&1 || true
    if ((${#files[@]} > 0)); then
        rm -f "${files[@]}"
    fi
    rm -f "$INDEX_FILE" "$HASH_FILE" "$IDS_FILE" "$THEME_IDX_FILE" "$SOURCE_IDX_FILE" \
        "$HISTORY_FILE" "$SOURCES_FILE" "$SCORES_FILE" "$FEATURES_FILE" "$STATE_DIR/accent"
    log "OK" "Removidas $n imagens e estado de seleção/dedup."
}

# --------------------------------------------------------------------------
# Uso
# --------------------------------------------------------------------------
usage() {
    cat <<EOF
Wallpaper Carousel v${VERSION} — carrossel premium de wallpapers (KDE Plasma/Wayland)

Uso: $(basename "$0") <comando>

  download [fonte]   Baixa novos wallpapers: all|wallhaven|reddit|bing|picsum (default all)
  rotate             Aplica a próxima imagem escolhida pelo score
  rotate --dry-run   Mostra a escolha e o breakdown, sem aplicar/registrar
  prep [--rebuild]   Garante os caches -screen de todos os masters (--rebuild refaz)
  maintenance [--prep]  Higiene de estado (features/sources/hashes/history), rotação de log
  status             Status: próximo pick, última troca, fontes, disco, saúde, timers e log
  doctor [--json] [--full]  Diagnóstico de ferramentas, dados, estado, tela, timers e log
  why                Breakdown do score (top 5 candidatos)
  like [arquivo]     Marca like (sem arquivo: wallpaper atual; aceita caminho e -screen)
  block [arquivo]    Bloqueia a imagem (nunca mais é escolhida)
  unlike [arquivo]   Remove um like
  unblock [arquivo]  Desbloqueia a imagem
  eval               Compara a seleção antiga (alfabética) com a nova (score)
  setup [--dry-run]  Instala/ativa os timers (rotação + download); --dry-run só mostra
  clean              Remove todas as imagens baixadas e o estado de seleção
  uninstall          Desativa e remove os timers do systemd (mantém as imagens)
EOF
}

# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------
if [[ "${WALLPAPER_LIB_ONLY:-0}" == "1" ]]; then
    if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
        return 0
    fi
    exit 0
fi

validate_config

case "${1:-}" in
    download) do_download "${2:-all}" ;;
    rotate)
        if [[ "${2:-}" == "--dry-run" ]]; then
            do_rotate --dry-run
        else
            do_rotate
        fi
        ;;
    prep) do_prep "${@:2}" ;;
    maintenance) do_maintenance "${@:2}" ;;
    setup) do_setup "${@:2}" ;;
    status) do_status ;;
    doctor) do_doctor "${@:2}" ;;
    why) do_why ;;
    like | block | unlike | unblock) do_feedback "$1" "${2:-}" ;;
    eval)
        [[ -f "$SCORE" ]] || die "Falta $SCORE"
        python3 "$SCORE" eval "${@:2}"
        ;;
    clean) do_clean ;;
    uninstall) do_uninstall ;;
    "" | -h | --help | help) usage ;;
    *)
        echo "Comando desconhecido: $1"
        usage
        exit 1
        ;;
esac
