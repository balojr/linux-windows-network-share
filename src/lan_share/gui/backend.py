"""Everything the GUI does goes through here: it runs the lan-share CLI and parses its JSON.

No GTK in this file, so it is unit-testable. Privileged commands run via `pkexec`, so the GUI itself never
runs as root. Passwords go to the child's stdin (never argv, which other users can see in `ps`).
"""

from __future__ import annotations

import getpass
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import lan_share

LIBDIR = str(Path(lan_share.__file__).resolve().parent.parent)  # directory that contains the lan_share package
HELPER = Path(LIBDIR) / "lan-share-helper"  # present when installed; has a polkit policy (friendly prompt, cached auth)


class BackendError(Exception):
    """A command failed; str(e) is a message fit to show the user."""


@dataclass
class Outcome:
    rc: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def message(self) -> str:
        """Best human-readable failure reason."""
        for text in (self.stderr, self.stdout):
            lines = [ln for ln in text.splitlines() if ln.strip()]
            errs = [ln for ln in lines if ln.startswith("error:")]
            if errs:
                return errs[-1][len("error:"):].strip()
        if self.rc in (126, 127):
            return "authorization was cancelled or denied"
        tail = [ln for ln in (self.stderr or self.stdout).splitlines() if ln.strip()]
        return tail[-1] if tail else f"exit status {self.rc}"


Runner = Callable[..., subprocess.CompletedProcess]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-_")[:32] or "windows"


class Backend:
    def __init__(self, run: Runner = subprocess.run, elevate: Optional[list[str]] = None, extra: Optional[list[str]] = None,
                 helper: Optional[str] = None):
        self._run = run
        # Installed: `pkexec <helper> <args>` -> the policy's own message, and polkit can remember the authorization.
        # Running from a source checkout (no helper): generic `pkexec env ... python3 -m lan_share`.
        self._helper = helper if helper is not None else (str(HELPER) if elevate is None and HELPER.exists() else None)
        # pkexec resets the environment, so pass PYTHONPATH explicitly through env(1).
        self._elevate = elevate if elevate is not None else [
            shutil.which("pkexec") or "pkexec", shutil.which("env") or "env", f"PYTHONPATH={LIBDIR}",
        ]
        self._extra = extra or []  # e.g. ["--root", DIR] for tests

    def argv(self, args: list[str], root: bool = False) -> list[str]:
        if root and self._helper:
            return [shutil.which("pkexec") or "pkexec", self._helper, *args]
        cli = [sys.executable, "-B", "-m", "lan_share", *self._extra, *args]
        return [*self._elevate, *cli] if root else cli

    def call(self, args: list[str], *, root: bool = False, stdin: Optional[str] = None, timeout: float = 600) -> Outcome:
        env = None
        if not root:
            import os
            env = {**os.environ, "PYTHONPATH": LIBDIR}
        try:
            res = self._run(self.argv(args, root), input=stdin, capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return Outcome(124, "", "error: timed out")
        except OSError as e:
            return Outcome(127, "", f"error: could not run: {e}")
        return Outcome(res.returncode, res.stdout, res.stderr)

    def _json(self, args: list[str], ok_codes: tuple[int, ...] = (0,), **kw):
        out = self.call(args, **kw)
        if out.rc not in ok_codes:
            raise BackendError(out.message)
        try:
            return json.loads(out.stdout)
        except json.JSONDecodeError:
            raise BackendError(f"unexpected output from lan-share: {out.stdout[:120]!r}")

    # --- reading (no privileges) -------------------------------------------------
    def doctor(self, root: bool = False) -> list[dict]:
        # exit 1 just means "found problems". root=True also runs the checks that need administrator rights.
        return self._json(["doctor", "--json"], ok_codes=(0, 1), root=root)

    def shares(self) -> list[dict]:
        return self._json(["share", "list", "--json"])

    def mounts(self) -> list[dict]:
        return self._json(["mount", "list", "--json"])

    def scan(self, timeout: float = 6.0) -> list[dict]:
        return self._json(["scan", "--json", "--timeout", str(timeout)], timeout=timeout + 30)

    def remote_shares(self, host: str, user: str, password: str, domain: str = "") -> list[dict]:
        args = ["shares", host, "--user", user, "--password-stdin", "--json"]
        return self._json(args + (["--domain", domain] if domain else []), stdin=password + "\n", timeout=60)

    # --- changing things (asks for authorization) --------------------------------
    def _must(self, out: Outcome) -> Outcome:
        if not out.ok:
            raise BackendError(out.message)
        return out

    def add_share(self, name: str, path: str, users: list[str], read_write: bool) -> Outcome:
        args = ["share", "add", name, path, "--apply"]
        for u in users:
            args += ["--user", u]
        return self._must(self.call(args + (["--rw"] if read_write else []), root=True))

    def remove_share(self, name: str) -> Outcome:
        return self._must(self.call(["share", "remove", name, "--apply"], root=True))

    def apply(self) -> Outcome:
        return self._must(self.call(["setup", "--apply"], root=True))

    def set_samba_password(self, user: str, password: str) -> Outcome:
        if not password:
            raise BackendError("the password is empty")
        return self._must(self.call(["user", "add", user, "--password-stdin"], root=True, stdin=password + "\n"))

    def add_mount(self, name: str, host: str, share: str, user: str, password: str, domain: str = "") -> Outcome:
        args = ["mount", "add", name, f"//{host}/{share}", "--user", user, "--local-user", getpass.getuser(), "--password-stdin"]
        return self._must(self.call(args + (["--domain", domain] if domain else []), root=True, stdin=password + "\n"))

    def remove_mount(self, name: str) -> Outcome:
        return self._must(self.call(["mount", "remove", name], root=True))
