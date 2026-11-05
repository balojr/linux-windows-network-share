"""Execution context and the plan/step model.

Everything that changes the system is expressed as a Step and collected into a
Plan, so it can be shown (dry run) before it is applied.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import LanShareError

# Admin tools often live in sbin, which is missing from a normal user's PATH.
_SEARCH_PATH = os.pathsep.join(
    [os.environ.get("PATH", ""), "/usr/local/sbin", "/usr/sbin", "/sbin"]
)


def which(name: str) -> Optional[str]:
    return shutil.which(name, path=_SEARCH_PATH)


def capture(cmd: list[str]) -> Optional[str]:
    """Run a read-only command; return stdout, or None if it failed or is missing."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout.strip() if res.returncode == 0 else None


@dataclass(frozen=True)
class Context:
    """Where to operate. A non-"/" root stages file writes and skips commands."""

    root: Path = Path("/")
    apply: bool = False

    @property
    def real(self) -> bool:
        return self.root == Path("/")

    def path(self, absolute: str) -> Path:
        return self.root / absolute.lstrip("/")


@dataclass
class Step:
    title: str
    cmd: Optional[list[str]] = None
    action: Optional[Callable[[], None]] = None
    detail: str = ""
    interactive: bool = False
    # True if `action` changes the live system rather than files under ctx.root.
    touches_system: bool = False
    # Best-effort step (used by teardown): a failing command is reported but does not abort the run.
    tolerate_failure: bool = False


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines: list[str] = []
        for w in self.warnings:
            lines.append(f"warning: {w}")
        if self.warnings:
            lines.append("")
        for i, s in enumerate(self.steps, 1):
            lines.append(f"{i:>2}. {s.title}")
            if s.cmd:
                lines.append("      $ " + " ".join(_quote(a) for a in s.cmd))
            if s.detail:
                lines.extend("      | " + ln for ln in s.detail.splitlines())
        return "\n".join(lines)


def _quote(arg: str) -> str:
    import shlex

    return shlex.quote(arg)


def write_file(path: Path, content: str, mode: int = 0o644, backup: bool = False) -> None:
    """Atomically write a file, optionally keeping a one-time backup of the original."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        bak = path.with_name(path.name + ".lan-share.bak")
        if not bak.exists():
            shutil.copy2(path, bak)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def file_step(
    ctx: Context, title: str, absolute: str, content: str, mode: int = 0o644,
    backup: bool = False, secret: bool = False,
) -> Step:
    target = ctx.path(absolute)
    return Step(
        title=f"{title} ({absolute})",
        action=lambda: write_file(target, content, mode, backup),
        detail="<contents hidden>" if secret else content.rstrip("\n"),
    )


def execute(plan: Plan, ctx: Context, log: Callable[[str], None] = print) -> None:
    if ctx.real and os.geteuid() != 0:
        raise LanShareError("applying changes needs root; re-run with sudo")
    for i, s in enumerate(plan.steps, 1):
        log(f"[{i}/{len(plan.steps)}] {s.title}")
        if s.action:
            if s.touches_system and not ctx.real:
                log("      (staged root: skipped system action)")
            else:
                s.action()
        if s.cmd:
            if not ctx.real:
                log(f"      (staged root: skipped `{' '.join(s.cmd)}`)")
                continue
            res = subprocess.run(s.cmd)
            if res.returncode != 0:
                if s.tolerate_failure:
                    log(f"      (ignored: exit {res.returncode})")
                    continue
                raise LanShareError(f"command failed ({res.returncode}): {' '.join(s.cmd)}")
