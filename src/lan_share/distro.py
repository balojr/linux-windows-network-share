"""Distro detection and the per-family tables (packages, service names)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .runner import Context, which

_FAMILY_BY_ID = {
    "debian": "debian", "ubuntu": "debian",
    "fedora": "fedora", "rhel": "fedora", "centos": "fedora",
    "rocky": "fedora", "almalinux": "fedora", "ol": "fedora",
    "arch": "arch",
    "suse": "suse", "opensuse": "suse", "sles": "suse", "sled": "suse",
}

# Logical package -> {family: distro package name}. A None entry means "not a
# separate package on this family" (already provided by another one).
# Verified in containers on Debian 13, Ubuntu 22.04/24.04, Fedora, Rocky 9, Arch and
# openSUSE Leap/Tumbleweed (tests/matrix). wsdd is not here: lan-share bundles it.
PACKAGES: dict[str, dict[str, Optional[str]]] = {
    "samba": {"debian": "samba", "fedora": "samba", "arch": "samba", "suse": "samba"},
    "avahi": {"debian": "avahi-daemon", "fedora": "avahi", "arch": "avahi", "suse": "avahi"},
    "cifs": {"debian": "cifs-utils", "fedora": "cifs-utils", "arch": "cifs-utils", "suse": "cifs-utils"},
    "smbclient": {"debian": "smbclient", "fedora": "samba-client", "arch": None, "suse": "samba-client"},
    "selinux-tools": {"fedora": "policycoreutils-python-utils"},
}

# Logical package -> binaries that prove it is installed.
BINARIES = {
    "samba": ["smbd"],
    "avahi": ["avahi-daemon"],
    "cifs": ["mount.cifs"],
    "smbclient": ["smbclient"],
    "selinux-tools": ["semanage"],
}

_PKG_MANAGER = {"debian": "apt", "fedora": "dnf", "arch": "pacman", "suse": "zypper"}


@dataclass(frozen=True)
class Distro:
    id: str
    name: str
    version: str
    family: str  # debian | fedora | arch | suse | unknown

    @property
    def pkg_manager(self) -> str:
        return _PKG_MANAGER.get(self.family, "unknown")

    @property
    def service_names(self) -> dict[str, str]:
        return {
            "samba": "smbd" if self.family == "debian" else "smb",
            "wsdd": "lan-share-wsdd",  # our own unit; see wsdd_service.py
            "avahi": "avahi-daemon",
        }

    def package(self, logical: str) -> Optional[str]:
        return PACKAGES[logical].get(self.family)


def parse_os_release(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key] = value.strip().strip('"').strip("'")
    return out


def from_os_release(fields: dict[str, str]) -> Distro:
    distro_id = fields.get("ID", "unknown").lower()
    tokens = [distro_id, *fields.get("ID_LIKE", "").lower().split()]
    family = next((_FAMILY_BY_ID[t] for t in tokens if t in _FAMILY_BY_ID), None)
    if family is None:
        # opensuse-leap, opensuse-tumbleweed, ...
        family = "suse" if any(t.startswith("opensuse") for t in tokens) else "unknown"
    return Distro(
        id=distro_id,
        name=fields.get("PRETTY_NAME", fields.get("NAME", distro_id)),
        version=fields.get("VERSION_ID", ""),
        family=family,
    )


def detect(ctx: Context) -> Distro:
    for rel in ("/etc/os-release", "/usr/lib/os-release"):
        p = ctx.path(rel)
        if p.exists():
            return from_os_release(parse_os_release(p.read_text()))
    return Distro("unknown", "unknown", "", "unknown")


def is_installed(logical: str) -> bool:
    return any(which(b) for b in BINARIES[logical])


def install_command(distro: Distro, packages: list[str]) -> list[str]:
    pm = distro.pkg_manager
    if pm == "apt":
        return ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", *packages]
    if pm == "dnf":
        return ["dnf", "install", "-y", *packages]
    if pm == "pacman":
        return ["pacman", "-S", "--needed", "--noconfirm", *packages]
    if pm == "zypper":
        return ["zypper", "--non-interactive", "install", *packages]
    raise ValueError(f"no package manager known for family {distro.family!r}")
