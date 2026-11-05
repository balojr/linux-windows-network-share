"""Record of system changes `setup` made that are not plain files we own.

Firewall rules, SELinux rules and enabled services may have existed before lan-share.
Only what is recorded here (because setup added it) is ever removed by `teardown`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from .runner import Context, write_file

STATE_PATH = "/var/lib/lan-share/state.json"


@dataclass
class State:
    # zone -> {"services": [...], "ports": ["3702/udp", ...]} that setup added
    firewalld: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    ufw: list[list[str]] = field(default_factory=list)  # `ufw` args after the program name, e.g. ["allow", "proto", ...]
    selinux: list[str] = field(default_factory=list)  # paths whose samba_share_t rule setup added
    enabled_units: list[str] = field(default_factory=list)  # units setup enabled (they were not enabled before)


def load(ctx: Context) -> Optional[State]:
    p = ctx.path(STATE_PATH)
    if not p.exists():
        return None
    raw = json.loads(p.read_text())
    return State(
        firewalld=raw.get("firewalld", {}),
        ufw=raw.get("ufw", []),
        selinux=raw.get("selinux", []),
        enabled_units=raw.get("enabled_units", []),
    )


def save(ctx: Context, st: State) -> None:
    write_file(ctx.path(STATE_PATH), json.dumps(asdict(st), indent=2) + "\n", 0o644)


def update(ctx: Context, change: Callable[[State], None]) -> None:
    st = load(ctx) or State()
    change(st)
    save(ctx, st)


def _add_unique(items: list, value) -> None:
    if value not in items:
        items.append(value)


def record_firewalld(ctx: Context, zone: str, service: Optional[str] = None, port: Optional[str] = None) -> None:
    def change(st: State) -> None:
        z = st.firewalld.setdefault(zone, {"services": [], "ports": []})
        if service:
            _add_unique(z["services"], service)
        if port:
            _add_unique(z["ports"], port)

    update(ctx, change)


def record_ufw(ctx: Context, rule: list[str]) -> None:
    update(ctx, lambda st: _add_unique(st.ufw, rule))


def record_selinux(ctx: Context, path: str) -> None:
    update(ctx, lambda st: _add_unique(st.selinux, path))


def record_unit(ctx: Context, unit: str) -> None:
    update(ctx, lambda st: _add_unique(st.enabled_units, unit))
