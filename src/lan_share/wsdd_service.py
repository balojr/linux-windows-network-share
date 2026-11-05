"""Run the vendored wsdd under our own systemd unit.

One code path on every distro: we ship wsdd, install it under /usr/local/lib/lan-share
and manage `lan-share-wsdd.service` ourselves, so distro packaging differences don't matter.
Distro-provided wsdd units are disabled to avoid duplicate announcements.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from . import LanShareError
from .network import Interface
from .runner import Context, Plan, Step, capture, file_step, which, write_file

VENDORED = Path(__file__).parent / "vendor" / "wsdd.py"
INSTALL_PATH = "/usr/local/lib/lan-share/wsdd.py"
UNIT_NAME = "lan-share-wsdd"
UNIT_PATH = f"/etc/systemd/system/{UNIT_NAME}.service"

# Units and config files shipped by distros for their own wsdd packages.
LEGACY_UNITS = ("wsdd", "wsdd-server", "wsdd2")
LEGACY_CONFIGS = ("/etc/sysconfig/wsdd", "/etc/default/wsdd", "/etc/conf.d/wsdd")
MANAGED_MARK = "Managed by lan-share"

_IFACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,14}$")


def vendored_command() -> list[str]:
    """argv prefix that runs the bundled wsdd directly (used by `scan`, no install needed)."""
    return [sys.executable, str(VENDORED)]


def render_unit(python: str, samba_unit: str, interfaces: list[str], workgroup: str) -> str:
    for i in interfaces:
        if not _IFACE_RE.match(i):
            raise LanShareError(f"unsafe interface name {i!r}")
    args = ["--shortlog", f"--chroot=/run/{UNIT_NAME}", "-4", *(a for i in interfaces for a in ("-i", i)), "-w", workgroup]
    return "\n".join([
        f"# {MANAGED_MARK}. Replaces the distro's wsdd unit; regenerate with `lan-share setup --apply`.",
        "[Unit]",
        "Description=lan-share: WS-Discovery host daemon (lists this machine in Windows' Network tab)",
        "Documentation=https://github.com/christgau/wsdd",
        "After=network-online.target",
        "Wants=network-online.target",
        f"After={samba_unit}.service",
        # Stop (and restart) together with Samba: advertising a dead file server is worse than not advertising.
        f"BindsTo={samba_unit}.service",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={python} {INSTALL_PATH} {' '.join(args)}",
        # Same sandbox the distro units use: unprivileged throwaway user, chroot into an empty runtime dir.
        "DynamicUser=yes",
        f"RuntimeDirectory={UNIT_NAME}",
        "AmbientCapabilities=CAP_SYS_CHROOT",
        "Restart=on-failure",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ])


def _active_or_enabled(unit: str) -> bool:
    return capture(["systemctl", "is-active", unit]) == "active" or capture(["systemctl", "is-enabled", unit]) == "enabled"


def _restore_config(path: Path) -> None:
    bak = path.with_name(path.name + ".lan-share.bak")
    if path.exists() and bak.exists() and MANAGED_MARK in path.read_text():
        os.replace(bak, path)


def plan_wsdd(ctx: Context, samba_unit: str, lan: list[Interface], interfaces: list[str], workgroup: str, plan: Plan) -> None:
    # 1. Retire the distro's own wsdd (and undo edits an earlier lan-share made to its config).
    if ctx.real:
        for unit in LEGACY_UNITS:
            if _active_or_enabled(unit):
                plan.steps.append(Step(f"Disable the distro's {unit}.service (replaced by {UNIT_NAME})",
                                       cmd=["systemctl", "disable", "--now", unit]))
    for cfg in LEGACY_CONFIGS:
        p = ctx.path(cfg)
        if p.exists() and MANAGED_MARK in p.read_text() and p.with_name(p.name + ".lan-share.bak").exists():
            plan.steps.append(Step(f"Restore the original {cfg} (saved by an earlier lan-share)", action=lambda p=p: _restore_config(p)))

    # 2. Install the bundled wsdd and our unit.
    target = ctx.path(INSTALL_PATH)
    plan.steps.append(Step(
        f"Install bundled wsdd v0.8 ({INSTALL_PATH})",
        action=lambda: write_file(target, VENDORED.read_text(), 0o755),
        detail="MIT-licensed, by Steffen Christgau; see lan_share/vendor/README.md",
    ))
    python = shutil.which("python3") or sys.executable
    plan.steps.append(file_step(ctx, f"Write {UNIT_NAME}.service", UNIT_PATH,
                                render_unit(python, samba_unit, interfaces or [i.name for i in lan], workgroup)))
    plan.steps.append(Step("Reload systemd", cmd=["systemctl", "daemon-reload"]))
