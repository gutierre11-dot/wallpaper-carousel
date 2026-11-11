"""Aplicação de wallpaper e notificações (KDE Plasma / Wayland).

Dois caminhos de aplicação:
1. plasma-apply-wallpaperimage (preferido)
2. Fallback D-Bus: org.kde.PlasmaShell.evaluateScript
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from .config import Settings
from .state import master_name_from_any

log = logging.getLogger("wallpaper.apply")


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _js_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _apply_via_dbus(img: Path) -> bool:
    js_img = _js_escape(str(img))
    script = (
        "var allDesktops = desktops();\n"
        "for (var i = 0; i < allDesktops.length; i++) {\n"
        "    var d = allDesktops[i];\n"
        "    d.wallpaperPlugin = 'org.kde.image';\n"
        "    d.currentConfigGroup = Array('Wallpaper', 'org.kde.image', 'General');\n"
        f"    d.writeConfig('Image', 'file://{js_img}');\n"
        "}\n"
    )
    try:
        cp = subprocess.run(
            [
                "dbus-send",
                "--session",
                "--dest=org.kde.plasmashell",
                "--type=method_call",
                "/PlasmaShell",
                "org.kde.PlasmaShell.evaluateScript",
                f"string:{script}",
            ],
            capture_output=True,
            timeout=15,
        )
        return cp.returncode == 0
    except Exception:
        return False


def apply_wallpaper(img: Path) -> bool:
    """Aplica `img` como wallpaper. True em sucesso."""
    if not img.is_file():
        log.error("apply: arquivo inexistente: %s", img)
        return False
    if _have("plasma-apply-wallpaperimage"):
        try:
            cp = subprocess.run(
                ["plasma-apply-wallpaperimage", str(img)],
                capture_output=True,
                timeout=30,
            )
            if cp.returncode == 0:
                return True
            log.warning("plasma-apply-wallpaperimage falhou (rc=%s); tentando D-Bus.", cp.returncode)
        except Exception as exc:
            log.warning("plasma-apply-wallpaperimage exceção: %s; tentando D-Bus.", exc)
    if _have("dbus-send") and __import__("os").environ.get("DBUS_SESSION_BUS_ADDRESS"):
        if _apply_via_dbus(img):
            return True
        log.warning("Fallback D-Bus também falhou.")
    return False


# ---------------------------------------------------------------------------
# Notificações
# ---------------------------------------------------------------------------

def _source_label(name: str) -> str:
    if name.startswith("wallhaven-"):
        return "Wallhaven"
    if name.startswith(("reddit-", "r-")):
        return "Reddit"
    if name.startswith("bing-"):
        return "Bing"
    if name.startswith("picsum-"):
        return "Lorem Picsum"
    return "Outro"


def _notify_supports_actions() -> bool:
    try:
        cp = subprocess.run(
            ["notify-send", "--help"], capture_output=True, text=True, timeout=10
        )
        return "-A" in cp.stdout or "-A" in cp.stderr
    except Exception:
        return False


def _record_action(name: str, action: str) -> None:
    """Callback de ação da notificação (roda em background)."""
    import sys

    script = Path(__file__).resolve().parent.parent / "wallpaper_carousel" / "__main__.py"
    cmd = [sys.executable, "-m", "wallpaper_carousel", action, name]
    try:
        subprocess.run(cmd, timeout=60, capture_output=True)
    except Exception:
        pass


def notify(settings: Settings, master_name: str, resolution: tuple[int, int] | None) -> None:
    """Notifica a troca de wallpaper (se habilitado)."""
    if not settings.notify_enabled or not _have("notify-send"):
        return
    label = _source_label(master_name)
    res_txt = f"{resolution[0]}x{resolution[1]}" if resolution else ""
    body = f"{label} · {master_name}" + (f" · {res_txt}" if res_txt else "")
    try:
        if settings.notify_actions and _notify_supports_actions():
            proc = subprocess.Popen(
                [
                    "notify-send",
                    "-a", "Wallpaper Carousel",
                    "-i", "preferences-desktop-wallpaper",
                    "--expire-time=15000",
                    "-A", "like=Curtir",
                    "-A", "block=Bloquear",
                    "Próximo wallpaper",
                    body,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            # lê a ação escolhida e registra feedback
            import threading

            def _wait():
                try:
                    out = proc.communicate(timeout=30)[0] or ""
                    action = out.strip()
                    if action in ("like", "block"):
                        _record_action(master_name, action)
                except Exception:
                    pass

            threading.Thread(target=_wait, daemon=True).start()
        else:
            subprocess.run(
                [
                    "notify-send",
                    "-a", "Wallpaper Carousel",
                    "--expire-time=3000",
                    "Próximo wallpaper",
                    body,
                ],
                timeout=10,
                capture_output=True,
            )
    except Exception as exc:
        log.warning("notify-send falhou: %s", exc)
