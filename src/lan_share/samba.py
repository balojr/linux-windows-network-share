"""Render the managed Samba configuration fragment.

We never rewrite the user's smb.conf. We write our own file and append a single
`include` line to the end of smb.conf. Our file opens with its own `[global]`
header, so its settings apply as globals regardless of what section smb.conf
ended in, and (being last) they override anything earlier.
"""

from __future__ import annotations

from .config import Config, Share
from .network import Interface
from .runner import Context, Step, write_file

CONF_PATH = "/etc/samba/lan-share.conf"
SMB_CONF = "/etc/samba/smb.conf"
INCLUDE_LINE = f"include = {CONF_PATH}"
HEADER = "# Managed by lan-share. Manual edits will be overwritten; edit /etc/lan-share/config.json."


def render(cfg: Config, lan: list[Interface]) -> str:
    names = cfg.interfaces or [i.name for i in lan]
    nets = sorted({str(i.network) for i in lan})
    g = [
        HEADER,
        "[global]",
        f"   workgroup = {cfg.workgroup}",
        # SMB2+ only, signed, encrypted when the client supports it; no SMB1, no guests.
        "   server min protocol = SMB2_02",
        "   server signing = mandatory",
        "   server smb encrypt = desired",
        "   map to guest = Never",
        "   restrict anonymous = 2",
        # LAN only.
        "   bind interfaces only = yes",
        f"   interfaces = lo {' '.join(names)}",
        f"   hosts allow = 127.0.0.1 {' '.join(nets)}",
        "   hosts deny = ALL",
        # Not a print server.
        "   load printers = no",
        "   printcap name = /dev/null",
        "   disable spoolss = yes",
    ]
    if not cfg.netbios:
        g += ["   disable netbios = yes", "   smb ports = 445"]
    out = ["\n".join(g)]
    out += [_render_share(s) for s in cfg.shares]
    return "\n\n".join(out) + "\n"


def _render_share(s: Share) -> str:
    lines = [
        f"[{s.name}]",
        f"   path = {s.path}",
        "   comment = Shared by lan-share",
        "   browseable = yes",
        "   guest ok = no",
        f"   valid users = {' '.join(s.users)}",
    ]
    if s.read_only:
        lines.append("   read only = yes")
    else:
        lines += ["   read only = no", "   create mask = 0664", "   directory mask = 0775"]
    return "\n".join(lines)


def ensure_include(ctx: Context) -> None:
    """Append the include line to smb.conf (creating it if absent), keeping a backup."""
    p = ctx.path(SMB_CONF)
    if not p.exists():
        write_file(p, f"[global]\n   {INCLUDE_LINE}\n", 0o644)
        return
    text = p.read_text()
    if any(ln.strip() == INCLUDE_LINE for ln in text.splitlines()):
        return
    if not text.endswith("\n"):
        text += "\n"
    write_file(p, text + f"\n# lan-share\n{INCLUDE_LINE}\n", 0o644, backup=True)


def include_step(ctx: Context) -> Step:
    return Step(
        title=f"Include {CONF_PATH} from {SMB_CONF} (original backed up once)",
        action=lambda: ensure_include(ctx),
        detail=INCLUDE_LINE,
    )
