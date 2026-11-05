"""Mount remote SMB shares on first access, via systemd .mount/.automount units.

Units (not /etc/fstab) so that removal is a clean file delete and the boot never
waits on the network. Credentials live in a root-only file, never in a unit.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

from . import LanShareError
from .config import Mount
from .runner import Context, Plan, Step, file_step

UNIT_DIR = "/etc/systemd/system"
CRED_DIR = "/etc/lan-share/credentials"
IDLE_TIMEOUT = 120  # seconds of inactivity before the share is unmounted again
HEADER = "# Managed by lan-share. Remove with `lan-share mount remove`."


def escape_path(path: str) -> str:
    """systemd-escape --path: the unit-name form of a mount point."""
    parts = [p for p in os.path.normpath(path).split("/") if p]
    if not parts:
        return "-"
    out = []
    for n, part in enumerate(parts):
        enc = []
        for i, b in enumerate(part.encode()):
            ch = chr(b)
            if ch.isascii() and (ch.isalnum() or ch in ":_" or (ch == "." and not (n == 0 and i == 0))):
                enc.append(ch)
            else:
                enc.append(f"\\x{b:02x}")
        out.append("".join(enc))
    return "-".join(out)


def unit_names(m: Mount) -> tuple[str, str]:
    base = escape_path(m.mountpoint)
    return f"{base}.mount", f"{base}.automount"


def render_units(m: Mount, uid: int, gid: int) -> tuple[str, str]:
    options = [
        f"credentials={m.credentials_path}",
        f"uid={uid}", f"gid={gid}",
        "file_mode=0664", "dir_mode=0775",
        "nosuid", "nodev",
    ]
    mount = "\n".join([
        HEADER,
        "[Unit]",
        f"Description=lan-share mount of //{m.host}/{m.share}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Mount]",
        f"What=//{m.host}/{m.share}",
        f"Where={m.mountpoint}",
        "Type=cifs",
        f"Options={','.join(options)}",
        "TimeoutSec=20",
        "",
    ])
    auto = "\n".join([
        HEADER,
        "[Unit]",
        f"Description=lan-share automount for //{m.host}/{m.share}",
        "",
        "[Automount]",
        f"Where={m.mountpoint}",
        f"TimeoutIdleSec={IDLE_TIMEOUT}",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ])
    return mount, auto


def render_credentials(m: Mount, password: str) -> str:
    lines = [f"username={m.user}", f"password={password}"]
    if m.domain:
        lines.append(f"domain={m.domain}")
    return "\n".join(lines) + "\n"


def _ids(ctx: Context, local_user: str) -> tuple[int, int]:
    try:
        pw = pwd.getpwnam(local_user)
        return pw.pw_uid, pw.pw_gid
    except KeyError:
        if ctx.real:
            raise LanShareError(f"local user {local_user!r} does not exist")
        return 1000, 1000


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def plan_add(ctx: Context, m: Mount, password: str) -> Plan:
    m.validate()
    uid, gid = _ids(ctx, m.local_user)
    mount_unit, auto_unit = render_units(m, uid, gid)
    mount_name, auto_name = unit_names(m)
    plan = Plan()
    plan.steps.append(Step(f"Create private credentials directory ({CRED_DIR})",
                           action=lambda: _private_dir(ctx.path(CRED_DIR))))
    plan.steps.append(file_step(ctx, "Write credentials (root-only)", m.credentials_path,
                                render_credentials(m, password), mode=0o600, secret=True))
    plan.steps.append(file_step(ctx, "Write mount unit", f"{UNIT_DIR}/{mount_name}", mount_unit))
    plan.steps.append(file_step(ctx, "Write automount unit", f"{UNIT_DIR}/{auto_name}", auto_unit))
    plan.steps.append(Step("Reload systemd", cmd=["systemctl", "daemon-reload"]))
    plan.steps.append(Step(f"Enable automount at {m.mountpoint}", cmd=["systemctl", "enable", "--now", auto_name]))
    return plan


def plan_remove(ctx: Context, m: Mount) -> Plan:
    mount_name, auto_name = unit_names(m)
    plan = Plan()
    plan.steps.append(Step("Disable automount", cmd=["systemctl", "disable", "--now", auto_name], tolerate_failure=True))
    plan.steps.append(Step("Unmount if mounted", cmd=["systemctl", "stop", mount_name], tolerate_failure=True))
    for title, path in [
        ("Delete mount unit", f"{UNIT_DIR}/{mount_name}"),
        ("Delete automount unit", f"{UNIT_DIR}/{auto_name}"),
        ("Delete stored credentials", m.credentials_path),
    ]:
        target = ctx.path(path)
        plan.steps.append(Step(f"{title} ({path})", action=lambda t=target: t.unlink(missing_ok=True)))
    plan.steps.append(Step("Reload systemd", cmd=["systemctl", "daemon-reload"]))
    return plan
