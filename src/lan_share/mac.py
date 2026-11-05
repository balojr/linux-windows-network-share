"""Mandatory access control: SELinux labels so smbd may read the shared folders."""

from __future__ import annotations

import re
import subprocess

from . import LanShareError, state
from .config import Share
from .runner import Context, Plan, Step, capture, which


def selinux_mode() -> str:
    """Enforcing | Permissive | Disabled (also when SELinux is not present)."""
    return capture(["getenforce"]) or "Disabled"


def fcontext_regex(path: str) -> str:
    return re.sub(r"([.^$*+?{}\[\]\\|()])", r"\\\1", path.rstrip("/")) + "(/.*)?"


def _label(ctx: Context, path: str) -> None:
    rx = fcontext_regex(path)
    add = ["semanage", "fcontext", "-a", "-t", "samba_share_t", rx]
    if subprocess.run(add, capture_output=True).returncode == 0:
        state.record_selinux(ctx, path)  # ours: teardown may remove it
        return
    # Rule already exists (ours from an earlier run, or someone else's): modify, but do not claim it.
    mod = ["semanage", "fcontext", "-m", "-t", "samba_share_t", rx]
    if subprocess.run(mod).returncode != 0:
        raise LanShareError(f"could not set the SELinux context for {path}")


def plan_selinux(ctx: Context, shares: list[Share], plan: Plan) -> None:
    if selinux_mode() == "Disabled" or not shares:
        return
    if not which("semanage"):
        plan.warnings.append("SELinux is active but `semanage` is missing; shares may be unreadable")
        return
    for s in shares:
        plan.steps.append(
            Step(
                f"SELinux: label {s.path} as samba_share_t",
                action=lambda p=s.path: _label(ctx, p),
                touches_system=True,
                detail=f"semanage fcontext -a -t samba_share_t '{fcontext_regex(s.path)}'",
            )
        )
        plan.steps.append(Step(f"SELinux: apply labels under {s.path}", cmd=["restorecon", "-R", s.path]))
