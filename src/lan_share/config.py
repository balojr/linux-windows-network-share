"""User configuration: which folders to share and with whom."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import LanShareError
from .runner import Context, write_file

CONFIG_PATH = "/etc/lan-share/config.json"

_RESERVED_NAMES = {"global", "homes", "printers", "print$", "ipc$"}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
_MOUNT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_REMOTE_SHARE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._$-]{0,79}$")
_REMOTE_USER_RE = re.compile(r"^[A-Za-z0-9._@+-]{1,64}$")  # includes Microsoft-account e-mail logins

MOUNT_ROOT = "/mnt/lan-share"


@dataclass
class Share:
    name: str
    path: str
    users: list[str]
    read_only: bool = True

    def validate(self) -> None:
        if not _NAME_RE.match(self.name) or self.name.lower() in _RESERVED_NAMES:
            raise LanShareError(f"invalid share name {self.name!r}")
        if not self.path.startswith("/") or any(c in self.path for c in "\n\r\0%"):
            raise LanShareError(f"share path must be absolute with no control characters or '%': {self.path!r}")
        if not self.users:
            raise LanShareError(f"share {self.name!r} needs at least one user (guest shares are not supported)")
        for u in self.users:
            if not _USER_RE.match(u):
                raise LanShareError(f"invalid user name {u!r}")


@dataclass
class Mount:
    """A Windows (or other SMB) share mounted on this machine on first access."""

    name: str
    host: str  # IP or resolvable name; an IP is the most reliable (Windows names often don't resolve on Linux)
    share: str
    user: str  # remote account
    local_user: str  # local account that owns the files
    domain: str = ""

    @property
    def mountpoint(self) -> str:
        return f"{MOUNT_ROOT}/{self.name}"

    @property
    def credentials_path(self) -> str:
        return f"/etc/lan-share/credentials/{self.name}"

    def validate(self) -> None:
        if not _MOUNT_NAME_RE.match(self.name):
            raise LanShareError(f"invalid mount name {self.name!r} (use lowercase letters, digits, - and _)")
        if not _HOST_RE.match(self.host):
            raise LanShareError(f"invalid host {self.host!r}")
        if not _REMOTE_SHARE_RE.match(self.share):
            raise LanShareError(f"invalid share name {self.share!r}")
        if not _REMOTE_USER_RE.match(self.user):
            raise LanShareError(f"invalid remote user {self.user!r}")
        if self.domain and not _REMOTE_USER_RE.match(self.domain):
            raise LanShareError(f"invalid domain {self.domain!r}")
        if not _USER_RE.match(self.local_user):
            raise LanShareError(f"invalid local user {self.local_user!r}")


@dataclass
class Config:
    workgroup: str = "WORKGROUP"
    interfaces: list[str] = field(default_factory=list)  # empty = auto-detect
    netbios: bool = False
    shares: list[Share] = field(default_factory=list)
    mounts: list[Mount] = field(default_factory=list)

    def validate(self) -> None:
        if not re.match(r"^[A-Za-z0-9_-]{1,15}$", self.workgroup):
            raise LanShareError(f"invalid workgroup {self.workgroup!r}")
        seen: set[str] = set()
        for s in self.shares:
            s.validate()
            if s.name.lower() in seen:
                raise LanShareError(f"duplicate share name {s.name!r}")
            seen.add(s.name.lower())
        names: set[str] = set()
        for m in self.mounts:
            m.validate()
            if m.name in names:
                raise LanShareError(f"duplicate mount name {m.name!r}")
            names.add(m.name)

    def add_mount(self, mount: Mount) -> None:
        mount.validate()
        if any(m.name == mount.name for m in self.mounts):
            raise LanShareError(f"mount {mount.name!r} already exists (remove it first)")
        self.mounts.append(mount)

    def remove_mount(self, name: str) -> Mount:
        for m in self.mounts:
            if m.name == name:
                self.mounts.remove(m)
                return m
        raise LanShareError(f"no such mount {name!r}")

    def add_share(self, share: Share) -> None:
        share.validate()
        if any(s.name.lower() == share.name.lower() for s in self.shares):
            raise LanShareError(f"share {share.name!r} already exists")
        self.shares.append(share)

    def remove_share(self, name: str) -> None:
        before = len(self.shares)
        self.shares = [s for s in self.shares if s.name.lower() != name.lower()]
        if len(self.shares) == before:
            raise LanShareError(f"no such share {name!r}")


def load(ctx: Context) -> Config:
    p = ctx.path(CONFIG_PATH)
    if not p.exists():
        return Config()
    raw = json.loads(p.read_text())
    cfg = Config(
        workgroup=raw.get("workgroup", "WORKGROUP"),
        interfaces=raw.get("interfaces", []),
        netbios=raw.get("netbios", False),
        shares=[Share(**s) for s in raw.get("shares", [])],
        mounts=[Mount(**m) for m in raw.get("mounts", [])],
    )
    cfg.validate()
    return cfg


def save(ctx: Context, cfg: Config) -> Path:
    cfg.validate()
    p = ctx.path(CONFIG_PATH)
    write_file(p, json.dumps(asdict(cfg), indent=2) + "\n", 0o644)
    return p
