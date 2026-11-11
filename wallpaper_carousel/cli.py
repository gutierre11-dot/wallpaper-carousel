"""CLI do wallpaper-carousel (v7).

stdout é reservado para dados (nomes de arquivo, JSON, relatórios).
Logs vão para stderr + logs/carousel.log via logging.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, pipeline, systemd
from .apply import apply_wallpaper, notify
from .config import ConfigError, Settings
from .display import detect_resolution, kde_accent_hue, screen_target
from .imaging import (
    ImageError,
    compute_features,
    is_image_file,
    needs_screen_cache,
    probe,
    resolve_screen_path,
    screen_cache_path,
    screen_cache_valid,
)
from .log import get_logger, rotate_if_needed, setup as setup_logging
from .state import (
    IMAGE_EXTS,
    State,
    atomic_write_text,
    is_master_name,
    master_name_from_any,
    read_lines,
)

log = get_logger("wallpaper.cli")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _masters(settings: Settings) -> list[Path]:
    try:
        return sorted(
            p
            for p in settings.images_dir.iterdir()
            if p.is_file() and is_master_name(p.name)
        )
    except OSError:
        return []


def _count_images(settings: Settings) -> int:
    return len(_masters(settings))


def _source_label(name: str) -> str:
    if name.startswith("wallhaven-"):
        return "Wallhaven"
    if name.startswith("wallpaperscom-"):
        return "Wallpapers.com"
    if name.startswith("wikimedia-"):
        return "Wikimedia"
    if name.startswith("nasa-"):
        return "NASA"
    if name.startswith("openverse-"):
        return "Openverse"
    if name.startswith(("reddit-", "r-")):
        return "Reddit"
    if name.startswith("bing-"):
        return "Bing"
    if name.startswith("picsum-"):
        return "Lorem Picsum"
    return "Outro"


def _disk_usage_mb(path: Path) -> float:
    try:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / (
            1024 * 1024
        )
    except OSError:
        return 0.0


def _screen_cache_stats(settings: Settings) -> tuple[int, int, int]:
    """(total caches, válidos, desatualizados)."""
    total = valid = outdated = 0
    target = screen_target(settings)
    try:
        caches = [p for p in settings.images_dir.iterdir() if p.stem.endswith("-screen")]
    except OSError:
        return 0, 0, 0
    for c in caches:
        total += 1
        master = settings.images_dir / f"{c.stem[:-7]}{c.suffix}"
        if screen_cache_valid(master, c, target):
            valid += 1
        else:
            outdated += 1
    return total, valid, outdated


def _recent_errors(hours: int = 24, log_file: Path | None = None) -> int:
    try:
        lines = read_lines(log_file or log_path_global())[-5000:]
    except Exception:
        return 0
    cutoff = datetime.now().timestamp() - hours * 3600
    n = 0
    for ln in lines:
        if "[ERRO]" not in ln and "[ERROR]" not in ln:
            continue
        try:
            ts = datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S").timestamp()
            if ts >= cutoff:
                n += 1
        except ValueError:
            continue
    return n


_log_file: Path | None = None


def log_path_global() -> Path:
    global _log_file
    if _log_file is None:
        _log_file = Path.home() / ".local" / "share" / "wallpaper-carousel" / "logs" / "carousel.log"
    return _log_file


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------

def cmd_rotate(settings: Settings, state: State, dry_run: bool = False) -> int:
    from .scoring import PickEmpty, pick, pick_explain

    if _count_images(settings) == 0:
        log.warning("Sem imagens. Rode 'download' primeiro.")
        return 0

    try:
        result = pick(settings, top=5, state=state)
    except PickEmpty as exc:
        if exc.reason == "all_blocked":
            log.error("Todas as imagens estão bloqueadas (use 'unblock <arquivo>').")
        else:
            log.error("Nenhuma imagem com features; rode 'features --rebuild' ou 'download'.")
        return 1

    choice = result.choice
    if not (settings.images_dir / choice).is_file():
        # fallback alfabético
        imgs = _masters(settings)
        idx = state.index
        choice = imgs[idx % len(imgs)].name
        state.index = (idx + 1) % len(imgs)

    master = settings.images_dir / choice
    target = screen_target(settings)
    apply_path, regenerated = resolve_screen_path(master, settings.images_dir, target)
    if regenerated:
        log.info("Cache regenerado: %s", apply_path.name)

    if dry_run:
        log.info("DRY-RUN: escolheria %s (nada aplicado/registrado)", choice)
        try:
            expl = pick_explain(settings, top=5, state=state)
            ctx = expl.context
            print(
                f"Contexto: {'noite' if ctx.get('night') else 'dia'} | "
                f"accent: {ctx.get('accent_hue')} | "
                f"likes: {expl.counts.get('likes')} | blocks: {expl.counts.get('blocks')} | "
                f"candidatos: {expl.counts.get('candidates')}"
            )
            print(f"Escolha: {expl.choice}  (score {expl.score:.2f})")
            if expl.reason:
                print(f"\nPor quê: {expl.reason}")
        except Exception:
            pass
        return 0

    if apply_wallpaper(apply_path):
        log.info("Carrossel: %s", choice)
        state.append_history(choice)
        notify(settings, choice, target)
        return 0
    log.error("Não apliquei %s. Sessão Plasma ativa?", choice)
    return 1


def cmd_download(settings: Settings, state: State, source: str = "all") -> int:
    from .sources import ALL_SOURCES

    valid = {"all", *ALL_SOURCES}
    if source not in valid:
        log.error("Fonte inválida: '%s' (use: %s)", source, "|".join(sorted(valid)))
        return 2
    results = pipeline.download(source, settings, state)
    removed = pipeline.prune(settings, state)
    cmd_maintenance(settings, state, prep=False)
    total = _count_images(settings)
    log.info("Total na coleção: %d.", total)
    for r in results:
        if r.errors:
            log.warning("%s: erros: %s", r.source, "; ".join(r.errors))
    return 0 if all(not r.errors for r in results) else 1


def cmd_prep(settings: Settings, state: State, rebuild: bool = False) -> int:
    target = screen_target(settings)
    stats = {"created": 0, "updated": 0, "ok": 0, "skipped": 0, "failed": 0}
    for master in _masters(settings):
        cache = screen_cache_path(master, settings.images_dir)
        if not rebuild and screen_cache_valid(master, cache, target):
            stats["ok"] += 1
            continue
        if not needs_screen_cache(master, target):
            stats["skipped"] += 1
            continue
        existed = cache.exists()
        if rebuild or not screen_cache_valid(master, cache, target):
            from .imaging import build_screen_cache

            if build_screen_cache(master, cache, target):
                stats["updated" if existed else "created"] += 1
            else:
                stats["failed"] += 1
    print(f"Prep de cache de tela ({target[0]}x{target[1]})")
    print(
        f"  criados: {stats['created']} | atualizados: {stats['updated']} | "
        f"já ok: {stats['ok']} | sem necessidade: {stats['skipped']} | erros: {stats['failed']}"
    )
    return 1 if stats["failed"] else 0


def cmd_maintenance(settings: Settings, state: State, prep: bool = False) -> int:
    rotate_if_needed(settings.log_file, settings.log_max_kb)
    summary = {}
    try:
        from .scoring import housekeeping

        summary = housekeeping(settings, state=state)
    except Exception as exc:
        log.warning("housekeeping falhou: %s", exc)
    # higieniza hashes/sources contra masters vivos
    alive = {p.name for p in _masters(settings)}
    for path in (state.hashes_file, state.sources_file):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        kept = [ln for ln in lines if ln.split("\t")[0].strip() in alive]
        if len(kept) != len(lines):
            atomic_write_text(path, "\n".join(kept) + ("\n" if kept else ""))
    if prep:
        cmd_prep(settings, state)
    log.info("Maintenance: %s", json.dumps(summary, ensure_ascii=False) if summary else "sem scoring")
    return 0


def cmd_status(settings: Settings, state: State) -> int:
    masters = _masters(settings)
    total = len(masters)
    sources: dict[str, int] = {}
    for p in masters:
        src = state.source_of(p.name)
        sources[src] = sources.get(src, 0) + 1
    c_total, c_valid, c_out = _screen_cache_stats(settings)
    res, res_src = detect_resolution()
    accent = kde_accent_hue(settings)
    last = state.last_rotation()

    print("=== Wallpaper Carousel v%s ===" % __version__)
    print(f"Imagens            : {total} (máx {settings.max_images})")
    print(f"Resolução tela     : {res[0]}x{res[1]} ({res_src}) | cache alvo: {screen_target(settings)}")
    print(f"Cache -screen      : {c_total} total | {c_valid} válidos | {c_out} desatualizados")
    print(f"Accent KDE         : {f'{accent:.1f}°' if accent is not None else 'n/a'}")
    if last:
        print(f"Última troca       : {last[0].strftime('%Y-%m-%d %H:%M:%S')} ({last[1]})")
    else:
        print("Última troca       : nunca")
    print("Fontes             : " + (", ".join(f"{k}={v}" for k, v in sorted(sources.items())) or "nenhuma"))
    print(f"Disco images/      : {_disk_usage_mb(settings.images_dir):.0f} MB (teto {settings.max_storage_mb} MB)")
    try:
        st = shutil.disk_usage(settings.data_dir)
        print(f"Disco livre        : {st.free / (1024**3):.1f} GB")
    except OSError:
        pass
    errs = _recent_errors(24, settings.log_file)
    if errs:
        print(f"Avisos             : {errs} erro(s) nas últimas 24h")
    return 0


def cmd_doctor(settings: Settings, state: State, as_json: bool = False, full: bool = False) -> int:
    checks: list[dict] = []

    def chk(level: str, name: str, ok: bool, detail: str = "") -> None:
        checks.append({"level": level, "name": name, "ok": ok, "detail": detail})

    for tool in ("python3",):
        chk("critico" if not shutil.which(tool) else "ok", f"tool:{tool}", bool(shutil.which(tool)))
    chk("critico", "data_dir", settings.data_dir.is_dir(), str(settings.data_dir))
    try:
        probe_file = settings.images_dir
        probe_file.mkdir(parents=True, exist_ok=True)
        test = probe_file / ".write-test"
        test.touch()
        test.unlink()
        chk("critico", "gravavel", True)
    except OSError:
        chk("critico", "gravavel", False)
    for mod in ("PIL", "requests"):
        try:
            __import__(mod)
            chk("ok", f"mod:{mod}", True)
        except ImportError:
            chk("critico", f"mod:{mod}", False)
    masters = _masters(settings)
    chk("aviso" if not masters else "ok", "images", bool(masters), f"{len(masters)} imagens")
    for req in (state.ids_file, state.hashes_file, state.sources_file, state.history_file):
        chk("aviso", f"state:{req.name}", req.exists(), "" if req.exists() else "ausente")
    res, src = detect_resolution()
    chk("ok" if src != "fallback" else "aviso", "display", src != "fallback", f"{res[0]}x{res[1]} via {src}")
    dbus = bool(__import__("os").environ.get("DBUS_SESSION_BUS_ADDRESS"))
    wayland = bool(__import__("os").environ.get("WAYLAND_DISPLAY"))
    chk("ok" if dbus else "aviso", "dbus", dbus, "")
    chk("ok" if wayland else "aviso", "wayland", wayland, "")
    chk("aviso", "plasma-apply", shutil.which("plasma-apply-wallpaperimage") is not None, "")

    # saúde de estado
    if masters:
        alive = {p.name for p in masters}
        feats = 0
        try:
            feats = sum(1 for ln in read_lines(state.features_file) if ln.strip())
        except OSError:
            pass
        if feats < len(alive):
            chk("aviso", "features", False, f"{feats}/{len(alive)} com features (serão calculadas sob demanda)")
        else:
            chk("ok", "features", True, f"{feats} features")

    critical = [c for c in checks if c["level"] == "critico" and not c["ok"]]
    warnings = [c for c in checks if c["level"] == "aviso" and not c["ok"]]

    if as_json:
        print(json.dumps({
            "version": __version__,
            "checks": checks,
            "critical": len(critical),
            "warnings": len(warnings),
            "images": len(masters),
        }, indent=2, ensure_ascii=False))
    else:
        print(f"=== doctor v{__version__} ===")
        for c in checks:
            mark = "OK " if c["ok"] else ("CRIT" if c["level"] == "critico" else "AVISO")
            print(f"  [{mark}] {c['name']:<22} {c['detail']}")
        print(f"Críticos: {len(critical)} | Avisos: {len(warnings)}")
    return 1 if critical else 0


def cmd_why(settings: Settings, state: State) -> int:
    from .scoring import PickEmpty, pick_explain

    try:
        result = pick_explain(settings, top=5, state=state)
    except PickEmpty as exc:
        log.error("Sem candidatos: %s", exc.reason)
        return 1
    ctx = result.context
    print(
        f"Contexto: {'noite' if ctx.get('night') else 'dia'} | "
        f"accent: {ctx.get('accent_hue')} | "
        f"likes: {result.counts.get('likes')} | blocks: {result.counts.get('blocks')} | "
        f"candidatos: {result.counts.get('candidates')}"
    )
    print(f"Escolha: {result.choice}  (score {result.score:.2f})")
    for cand in result.top[:5]:
        print(f"  {cand['name']:<32} {cand['total']:>7.2f}  {cand.get('source', '')}")
    if result.reason:
        print(f"\nPor quê: {result.reason}")
    return 0


def cmd_feedback(settings: Settings, state: State, action: str, target: str | None) -> int:
    name = target
    if name is None:
        last = state.last_rotation()
        name = last[1] if last else None
    if not name:
        log.error("Nenhum wallpaper atual; informe o arquivo.")
        return 2
    name = master_name_from_any(name)
    if not (settings.images_dir / name).is_file():
        log.error("Imagem não encontrada: %s", name)
        return 2
    try:
        from .scoring import feedback as record

        fb = record(settings, name, action, state=state)
    except (ValueError, FileNotFoundError) as exc:
        log.error("feedback falhou: %s", exc)
        return 1
    print(f"{action}\t{name}\tlikes={fb.likes}\tblocked={str(fb.blocked).lower()}")
    return 0


def cmd_eval(settings: Settings, state: State, args: dict) -> int:
    """eval [--picks N] [--start ISO] [--json]"""
    from .scoring import evaluate, parse_start
    from .scoring.feedback import load_feedback as _lf
    from .scoring.features import load_features as _lf2

    try:
        feats = _lf2(state, settings.images_dir)
        fb = _lf(state)
        result = evaluate(
            feats, fb, args["picks"],
            args["start"] and parse_start(args["start"]) or parse_start(None),
            settings, state=state,
        )
    except Exception as exc:
        log.error("eval falhou: %s", exc)
        return 1
    as_json = args.get("json", False)
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print("=== eval old vs new ===")
        for pol in ("old", "new"):
            m = result[pol]
            print(
                f"  {pol}: picks={m.get('picks')} distintas={m.get('imagens_distintas')} "
                f"desvio_fonte={m.get('desvio_fonte_pp')}pp "
                f"desacordo_dia_noite={m.get('pct_desacordo_dia_noite')}%"
            )
        print("  checks: " + ", ".join(f"{k}={'OK' if v else 'FALHOU'}" for k, v in result["checks"].items()))
        print(f"  criterio_ok: {result['criterio_ok']}")
    return 0 if result["criterio_ok"] else 1


def cmd_clean(settings: Settings, state: State) -> int:
    removed = 0
    for p in settings.images_dir.iterdir():
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            p.unlink(missing_ok=True)
            removed += 1
    for fname in (
        "index", "hashes.tsv", "ids", "theme_idx", "source_idx", "history",
        "sources.tsv", "scores.tsv", "features.tsv", "accent", "page_offset",
    ):
        try:
            (state.dir / fname).unlink(missing_ok=True)
        except OSError:
            pass
    log.info("clean: %d arquivo(s) de imagem removidos.", removed)
    return 0


def cmd_features(settings: Settings, state: State, rebuild: bool, as_json: bool) -> int:
    from .scoring import list_features
    from .scoring.features import load_features

    feats = load_features(state, settings.images_dir, rebuild=rebuild)
    if as_json:
        print(json.dumps(
            [
                {
                    "name": f.name, "w": f.w, "h": f.h, "luma": f.luma,
                    "luma_std": f.luma_std, "warmth": f.warmth, "hue": f.hue,
                    "sat": f.sat, "dhash": str(f.dhash),
                }
                for f in feats.values()
            ],
            indent=2,
            ensure_ascii=False,
        ))
        return 0
    print(f"{'nome':<36} {'dim':>10} {'luma':>7} {'std':>6} {'warm':>6} {'hue':>7} {'sat':>6}")
    for name in sorted(feats):
        f = feats[name]
        print(
            f"{name:<36} {f.w}x{f.h:<7} {f.luma:>7.1f} {f.luma_std:>6.1f} "
            f"{f.warmth:>6.1f} {f.hue:>7.1f} {f.sat:>6.3f}"
        )
    print(f"\n{len(feats)} imagem(ns) — cache: {state.features_file}")
    return 0


def cmd_history(state: State, limit: int, as_json: bool) -> int:
    hist = state.load_history()
    if as_json:
        print(json.dumps([{"ts": ts.strftime("%Y-%m-%d %H:%M:%S"), "name": n} for ts, n in hist[-limit:]], ensure_ascii=False))
    else:
        for ts, n in hist[-limit:]:
            print(f"{ts.strftime('%Y-%m-%d %H:%M:%S')}\t{n}")
    return 0


def cmd_score(settings: Settings, state: State, name: str, as_json: bool) -> int:
    from .scoring import score_file

    result = score_file(settings, master_name_from_any(name), state=state)
    if result is None:
        log.error("Imagem sem features ou inexistente: %s", name)
        return 2
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0
    if result.get("blocked"):
        print(f"{result['name']}\tEXCLUÍDA (block)")
        return 0
    print(f"{result['name']}\tscore={result['total']:.3f}")
    for k, v in sorted(result.get("components", {}).items()):
        print(f"  {k:<20} {v:+.3f}")
    return 0


def cmd_simulate(settings: Settings, state: State, args: dict) -> int:
    from .scoring import parse_start, simulate
    from .scoring.feedback import load_feedback as _lf
    from .scoring.features import load_features as _lf2

    try:
        feats = _lf2(state, settings.images_dir)
        fb = _lf(state)
        hist = state.load_history()
        sources = state.load_sources()
        result = simulate(
            feats, fb, args["picks"], args["policy"],
            args["start"] and parse_start(args["start"]) or parse_start(None),
            settings, history=hist, sources=sources,
        )
    except Exception as exc:
        log.error("simulate falhou: %s", exc)
        return 1
    as_json = args.get("json", False)
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        for k, v in result.items():
            if k == "picks_list":
                print(f"picks: {', '.join(v[:10])}{'...' if len(v) > 10 else ''}")
            else:
                print(f"{k}: {v}")
    return 0


def cmd_list(settings: Settings, state: State) -> int:
    for p in _masters(settings):
        print(f"{p.name}\t{_source_label(p.name)}\t{state.source_of(p.name)}")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wallpaper-carousel",
        description="Carrossel premium de wallpapers para KDE Plasma",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", metavar="comando")

    def add(name, help_, **kw):
        sp = sub.add_parser(name, help=help_, **kw)
        return sp

    sp = add("rotate", "Aplica o próximo wallpaper")
    sp.add_argument("--dry-run", action="store_true")

    sp = add("download", "Baixa novos wallpapers")
    sp.add_argument(
        "source", nargs="?", default="all",
        choices=[
            "all", "wallhaven", "wikimedia", "nasa", "wallpaperscom",
            "picsum", "bing", "openverse", "reddit",
        ],
    )

    sp = add("prep", "Gera/valida caches -screen")
    sp.add_argument("--rebuild", action="store_true")

    sp = add("maintenance", "Housekeeping + higiene de estado")
    sp.add_argument("--prep", action="store_true")

    add("status", "Relatório de status")

    sp = add("doctor", "Diagnóstico de saúde")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--full", action="store_true")

    add("why", "Explica a escolha atual do scoring")

    for fb in ("like", "block", "unlike", "unblock"):
        sp = add(fb, f"Feedback: {fb}")
        sp.add_argument("file", nargs="?")

    sp = add("eval", "Avalia políticas de seleção")
    sp.add_argument("--picks", type=int, default=60)
    sp.add_argument("--start", default=None,
                    help="ISO 8601 ou 'now' (default: início do histórico)")
    sp.add_argument("--json", action="store_true")

    sp = add("features", "Lista features das imagens")
    sp.add_argument("--rebuild", action="store_true")
    sp.add_argument("--json", action="store_true")

    sp = add("history", "Histórico de rotações")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--json", action="store_true")

    sp = add("score", "Mostra o score de uma imagem")
    sp.add_argument("file")
    sp.add_argument("--json", action="store_true")

    sp = add("simulate", "Simula picks sem aplicar")
    sp.add_argument("--picks", type=int, default=60)
    sp.add_argument("--policy", default="new",
                    choices=["old", "new", "random"])
    sp.add_argument("--start", default=None,
                    help="ISO 8601 ou 'now' (default: início do histórico)")
    sp.add_argument("--json", action="store_true")

    add("list", "Lista imagens da coleção")
    add("clean", "Remove TODAS as imagens e o estado")

    sp = add("setup", "Instala/atualiza unidades systemd")
    sp.add_argument("--dry-run", action="store_true")

    add("uninstall", "Remove unidades systemd (mantém dados)")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"ERRO de configuração: {exc}", file=sys.stderr)
        return 2
    settings.ensure_dirs()
    setup_logging(settings.log_file, settings.log_max_kb)

    # flock não-bloqueante para comandos que mutam estado
    state = State(settings.state_dir)
    mutating = {
        "rotate", "download", "prep", "maintenance", "like", "block",
        "unlike", "unblock", "clean", "setup",
    }
    flock_held = bool(__import__("os").environ.get("WALLPAPER_FLOCK_HELD"))
    if (
        args.command in mutating
        and not flock_held
        and not state.try_lock("run")
    ):
        log.warning("Outra execução em andamento; abortando.")
        return 0

    try:
        if args.command == "rotate":
            return cmd_rotate(settings, state, dry_run=args.dry_run)
        if args.command == "download":
            return cmd_download(settings, state, source=args.source)
        if args.command == "prep":
            return cmd_prep(settings, state, rebuild=args.rebuild)
        if args.command == "maintenance":
            return cmd_maintenance(settings, state, prep=args.prep)
        if args.command == "status":
            return cmd_status(settings, state)
        if args.command == "doctor":
            return cmd_doctor(settings, state, as_json=args.json, full=args.full)
        if args.command == "why":
            return cmd_why(settings, state)
        if args.command in ("like", "block", "unlike", "unblock"):
            return cmd_feedback(settings, state, args.command, args.file)
        if args.command == "eval":
            return cmd_eval(settings, state, vars(args))
        if args.command == "features":
            return cmd_features(settings, state, args.rebuild, args.json)
        if args.command == "history":
            return cmd_history(state, args.limit, args.json)
        if args.command == "score":
            return cmd_score(settings, state, args.file, args.json)
        if args.command == "simulate":
            return cmd_simulate(settings, state, vars(args))
        if args.command == "list":
            return cmd_list(settings, state)
        if args.command == "clean":
            return cmd_clean(settings, state)
        if args.command == "setup":
            return systemd.setup(settings, dry_run=args.dry_run)
        if args.command == "uninstall":
            return systemd.uninstall(settings)
    except KeyboardInterrupt:
        log.warning("Interrompido pelo usuário.")
        return 130
    except Exception as exc:
        log.exception("Erro inesperado: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
