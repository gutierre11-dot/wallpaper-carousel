"""Geração das unidades systemd --user (setup/uninstall)."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from .config import Settings

log = logging.getLogger("wallpaper.systemd")

SERVICE_ROTATE = "wallpaper-carousel-rotate.service"
SERVICE_DOWNLOAD = "wallpaper-carousel-download.service"
TIMER_ROTATE = "wallpaper-carousel-rotate.timer"
TIMER_DOWNLOAD = "wallpaper-carousel-download.timer"

_RUN_SH = "{data_dir}/run.sh"

_HARDENING = """\
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths={data_dir}
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=300
"""


def _rotate_timer_calendar(minutes: int) -> str:
    if minutes == 60:
        return "OnCalendar=*:00:00"
    if minutes < 60 and 60 % minutes == 0:
        return f"OnCalendar=*:0/{minutes}:00"
    return ""


def unit_files(settings: Settings) -> dict[str, str]:
    """Retorna {nome_arquivo: conteúdo} das 4 unidades."""
    run = _RUN_SH.format(data_dir=settings.data_dir)
    env_common = (
        f"Environment=XDG_RUNTIME_DIR=%t\n"
        f"Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus\n"
        f"Environment=HOME=%h\n"
        f"Environment=PYTHONUNBUFFERED=1\n"
    )

    rotate_service = f"""\
[Unit]
Description=Wallpaper carousel: alterna o papel de parede
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=oneshot
ExecStart={run} rotate
{env_common}{_HARDENING.format(data_dir=settings.data_dir)}KillMode=process
"""

    download_service = f"""\
[Unit]
Description=Wallpaper carousel: baixa novos wallpapers
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={run} download
Environment=HOME=%h
Environment=PYTHONUNBUFFERED=1
{_HARDENING.format(data_dir=settings.data_dir)}"""

    cal = _rotate_timer_calendar(settings.rotate_minutes)
    if cal:
        cal_block = f"{cal}\n"
    else:
        cal_block = f"OnBootSec=30s\nOnUnitActiveSec={settings.rotate_minutes}min\n"
    rotate_timer = f"""\
[Unit]
Description=Alterna o papel de parede a cada {settings.rotate_minutes} min

[Timer]
{cal_block}Persistent=true
AccuracySec=10s

[Install]
WantedBy=timers.target
"""

    download_timer = f"""\
[Unit]
Description=Renova a colecao de wallpapers a cada {settings.download_hours}h

[Timer]
OnCalendar=0/{settings.download_hours}:15
RandomizedDelaySec=120
Persistent=true

[Install]
WantedBy=timers.target
"""

    return {
        SERVICE_ROTATE: rotate_service,
        SERVICE_DOWNLOAD: download_service,
        TIMER_ROTATE: rotate_timer,
        TIMER_DOWNLOAD: download_timer,
    }


def setup(settings: Settings, dry_run: bool = False) -> int:
    """Grava unidades, daemon-reload, enable --now dos timers."""
    target_dir = settings.systemd_dir or (Path.home() / ".config" / "systemd" / "user")
    units = unit_files(settings)
    for name, content in units.items():
        path = target_dir / name
        log.info("%s %s", "gravaria" if dry_run else "gravando", path)
        if not dry_run:
            target_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    if dry_run or settings.systemd_dir:
        if settings.systemd_dir:
            log.info("WALLPAPER_SYSTEMD_DIR definido: systemctl não será chamado.")
        return 0
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    for timer in (TIMER_ROTATE, TIMER_DOWNLOAD):
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", timer], check=False
        )
    return 0


def uninstall(settings: Settings) -> int:
    """Para e remove timers/services (mantém imagens e estado)."""
    target_dir = settings.systemd_dir or (Path.home() / ".config" / "systemd" / "user")
    if not settings.systemd_dir:
        for unit in (TIMER_ROTATE, TIMER_DOWNLOAD, SERVICE_ROTATE, SERVICE_DOWNLOAD):
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", unit], check=False
            )
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    for name in (TIMER_ROTATE, TIMER_DOWNLOAD, SERVICE_ROTATE, SERVICE_DOWNLOAD):
        path = target_dir / name
        if path.exists():
            path.unlink()
            log.info("removido %s", path)
    return 0
