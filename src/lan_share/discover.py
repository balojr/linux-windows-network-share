"""Find Windows machines (and other WS-Discovery hosts) and list their shares."""

from __future__ import annotations

import ipaddress
import os
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass

from . import LanShareError
from .runner import which
from .wsdd_service import vendored_command

_DISCOVERED = re.compile(r"discovered (?P<name>\S+) in (?P<kind>Workgroup|Domain):(?P<group>\S+) on (?P<addr>\S+)\s*$")


@dataclass(frozen=True)
class Host:
    name: str
    group: str
    address: str


def parse_wsdd_output(text: str) -> list[Host]:
    seen: dict[str, Host] = {}
    for line in text.splitlines():
        m = _DISCOVERED.search(line)
        if m:
            seen[m["addr"]] = Host(m["name"], m["group"], m["addr"])
    def numeric(h: Host):
        try:
            return (0, int(ipaddress.ip_address(h.address)), "")
        except ValueError:
            return (1, 0, h.address)

    return sorted(seen.values(), key=numeric)


def scan(interfaces: list[str], timeout: float = 8.0) -> list[Host]:
    """Run wsdd in discovery-only mode for `timeout` seconds and collect what answers."""
    cmd = [*vendored_command(), "--no-host", "--discovery", "-4", "-v"]
    for i in interfaces:
        cmd += ["-i", i]
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    time.sleep(timeout)
    proc.send_signal(signal.SIGINT)
    try:
        out, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return parse_wsdd_output(out)


def parse_smbclient_shares(text: str) -> list[tuple[str, str]]:
    """Parse `smbclient -g -L` output into (name, comment) for disk shares."""
    out = []
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) >= 2 and parts[0] == "Disk" and not parts[1].endswith("$"):
            out.append((parts[1], parts[2] if len(parts) > 2 else ""))
    return out


def list_shares(host: str, user: str, password: str, domain: str = "") -> list[tuple[str, str]]:
    """List a host's disk shares. The password goes through a private temp file, never argv."""
    if not which("smbclient"):
        raise LanShareError("smbclient not found; run `lan-share setup --apply` first")
    fd, path = tempfile.mkstemp(prefix="lan-share-auth-")
    try:
        with os.fdopen(fd, "w") as fh:  # mkstemp creates it 0600
            fh.write(f"username = {user}\npassword = {password}\n")
            if domain:
                fh.write(f"domain = {domain}\n")
        res = subprocess.run(
            ["smbclient", "-g", "-L", f"//{host}", "-A", path, "-m", "SMB3"],
            capture_output=True, text=True, timeout=30,
        )
    finally:
        os.unlink(path)
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).strip().splitlines()
        raise LanShareError(f"could not list shares on {host}: {detail[-1] if detail else 'unknown error'}")
    return parse_smbclient_shares(res.stdout)
