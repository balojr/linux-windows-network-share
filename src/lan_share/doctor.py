"""Diagnose why a machine isn't showing up in (or can't reach) the Windows network.

Every probe goes through `Env`, so the checks are testable with a fake system.
Results are plain data so the CLI (and later the GUI) can render them.
"""

from __future__ import annotations

import os
import socket
import subprocess
from dataclasses import asdict, dataclass
from typing import Callable, Optional

from . import distro as distromod, mounts
from .config import Config
from .distro import Distro
from .network import Interface
from .runner import Context, which

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"


@dataclass
class Result:
    status: str
    name: str
    detail: str = ""
    fix: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _default_run(cmd: list[str]) -> tuple[int, str]:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return res.returncode, res.stdout.strip()


def _default_connect(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass
class Env:
    ctx: Context
    run: Callable[[list[str]], tuple[int, str]] = _default_run
    connect: Callable[[str, int], bool] = _default_connect
    euid: Callable[[], int] = os.geteuid
    have: Callable[[str], Optional[str]] = which

    @property
    def root(self) -> bool:
        return self.euid() == 0


def parse_listeners(ss_output: str) -> set[tuple[str, str, int]]:
    """`ss -H -tuln` -> {(proto, address, port)}."""
    out = set()
    for line in ss_output.splitlines():
        f = line.split()
        if len(f) < 5 or f[0] not in ("tcp", "udp"):
            continue
        addr, _, port = f[4].rpartition(":")
        if port.isdigit():
            out.add((f[0], addr.strip("[]").split("%")[0], int(port)))
    return out


def parse_testparm(output: str) -> dict[str, str]:
    params = {}
    for line in output.splitlines():
        if "=" in line and not line.lstrip().startswith(("[", "#")):
            k, _, v = line.partition("=")
            params[k.strip().lower()] = v.strip()
    return params


# --------------------------------------------------------------------------- checks


def check_packages(env: Env, d: Distro) -> list[Result]:
    out = []
    for logical, label in [("samba", "Samba server"),
                           ("avahi", "Avahi (mDNS names)"), ("cifs", "cifs-utils (mount Windows shares)"),
                           ("smbclient", "smbclient")]:
        if distromod.is_installed(logical):
            out.append(Result(OK, f"{label} installed"))
        else:
            pkg = d.package(logical)
            fix = f"sudo lan-share setup --apply   (installs {pkg})" if pkg else "install it manually; not packaged on this distro"
            out.append(Result(FAIL if logical == "samba" else WARN, f"{label} missing", fix=fix))
    return out


def check_wsdd(env: Env) -> list[Result]:
    from . import wsdd_service as W

    out = []
    installed = env.ctx.path(W.INSTALL_PATH)
    if not installed.exists():
        out.append(Result(FAIL, "Bundled wsdd is not installed", "Windows will not list this machine in the Network tab",
                          "sudo lan-share setup --apply"))
    elif installed.read_text() != W.VENDORED.read_text():
        out.append(Result(WARN, "Installed wsdd differs from the bundled version", fix="sudo lan-share setup --apply"))
    else:
        out.append(Result(OK, "Bundled wsdd is installed and current"))
    if not env.ctx.path(W.UNIT_PATH).exists():
        out.append(Result(FAIL, f"{W.UNIT_NAME}.service is not installed", fix="sudo lan-share setup --apply"))
    for legacy in W.LEGACY_UNITS:
        if env.run(["systemctl", "is-active", "--quiet", legacy])[0] == 0:
            out.append(Result(WARN, f"The distro's {legacy}.service is also running", "duplicate announcements on the network",
                              f"sudo systemctl disable --now {legacy}"))
    return out


def check_services(env: Env, d: Distro) -> list[Result]:
    out = []
    for key, label in [("samba", "Samba"), ("wsdd", "wsdd"), ("avahi", "Avahi")]:
        unit = d.service_names[key]
        rc, _ = env.run(["systemctl", "is-active", "--quiet", unit])
        _, enabled = env.run(["systemctl", "is-enabled", unit])
        if rc == 0:
            note = "" if enabled == "enabled" else f" (but not enabled at boot: {enabled or 'unknown'})"
            out.append(Result(OK if not note else WARN, f"{label} service running{note}",
                              fix=f"sudo systemctl enable {unit}" if note else ""))
        else:
            out.append(Result(FAIL if key != "avahi" else WARN, f"{label} service ({unit}) not running",
                              fix=f"sudo systemctl enable --now {unit}   (see: journalctl -u {unit})"))
    return out


def check_samba_config(env: Env) -> list[Result]:
    out = []
    smb_conf = env.ctx.path("/etc/samba/smb.conf")
    include = "include = /etc/samba/lan-share.conf"
    if not smb_conf.exists() or include not in smb_conf.read_text():
        return [Result(FAIL, "lan-share config is not included from smb.conf", fix="sudo lan-share setup --apply")]
    if not env.have("testparm"):
        return [Result(SKIP, "Samba config check", "testparm not installed")]
    rc, text = env.run(["testparm", "-s", "--suppress-prompt", "--section-name=global"])
    if rc != 0:
        return [Result(FAIL, "Samba rejects its configuration", "run `testparm` for details", "sudo lan-share setup --apply")]
    p = parse_testparm(text)
    out.append(Result(OK, "Samba configuration is valid"))
    wants = [("bind interfaces only", "yes", "listens only on the LAN interface"),
             ("hosts deny", "all", "denies all hosts except the LAN")]
    for key, want, meaning in wants:
        if p.get(key, "").lower() == want:
            out.append(Result(OK, f"Samba {meaning}"))
        else:
            out.append(Result(WARN, f"Samba is not configured to {meaning}", f"{key} = {p.get(key, '(unset)')}",
                              "sudo lan-share setup --apply"))
    if p.get("server signing", "").lower() in ("required", "mandatory"):
        out.append(Result(OK, "SMB signing is required"))
    else:
        out.append(Result(WARN, "SMB signing is not required", fix="sudo lan-share setup --apply"))
    return out


def check_listeners(env: Env, d: Distro, lan: list[Interface]) -> list[Result]:
    rc, text = env.run(["ss", "-H", "-tuln"])
    if rc != 0:
        return [Result(SKIP, "Listening ports", "`ss` unavailable")]
    ls = parse_listeners(text)
    lan_ips = {str(i.address) for i in lan}
    out = []
    smb = {a for (p, a, port) in ls if p == "tcp" and port == 445}
    if not smb:
        out.append(Result(FAIL, "Nothing is listening on SMB port 445", fix=f"sudo systemctl restart {d.service_names['samba']}"))
    else:
        if smb & lan_ips:
            out.append(Result(OK, "SMB (445/tcp) is listening on the LAN address"))
        else:
            out.append(Result(FAIL, "SMB is not listening on the LAN address", f"listening on: {', '.join(sorted(smb))}",
                              "check `interfaces` in /etc/samba/lan-share.conf"))
        if smb & {"0.0.0.0", "::", "*"}:
            out.append(Result(WARN, "SMB is listening on ALL interfaces", "including ones you may not trust",
                              "sudo lan-share setup --apply"))
    for proto, port, what in [("udp", 3702, "WS-Discovery (3702/udp)"), ("tcp", 5357, "WS-Discovery metadata (5357/tcp)")]:
        hit = {a for (p, a, pt) in ls if p == proto and pt == port}
        if hit & (lan_ips | {"0.0.0.0", "::", "*"}):
            out.append(Result(OK, f"{what} is listening"))
        else:
            out.append(Result(FAIL, f"{what} is not listening",
                              "Windows will not list this machine in the Network tab", "sudo systemctl restart wsdd"))
    return out


def parse_active_zones(text: str) -> dict[str, str]:
    """`firewall-cmd --get-active-zones` -> {interface: zone}."""
    out, zone = {}, None
    for line in text.splitlines():
        if line and not line[0].isspace():
            zone = line.split()[0]
        elif line.strip().startswith("interfaces:") and zone:
            for iface in line.split(":", 1)[1].split():
                out[iface] = zone
    return out


def parse_zone_listing(text: str) -> tuple[set[str], list[tuple[int, int, str]]]:
    """`firewall-cmd --list-all` -> (services, [(first_port, last_port, proto)])."""
    services: set[str] = set()
    ports: list[tuple[int, int, str]] = []
    for line in text.splitlines():
        key, _, value = line.strip().partition(":")
        if key == "services":
            services = set(value.split())
        elif key == "ports":
            for item in value.split():
                span, _, proto = item.partition("/")
                lo, _, hi = span.partition("-")
                if lo.isdigit():
                    ports.append((int(lo), int(hi or lo), proto))
    return services, ports


def _covers(ports: list[tuple[int, int, str]], port: int, proto: str) -> bool:
    return any(p == proto and lo <= port <= hi for lo, hi, p in ports)


def check_firewall(env: Env, lan: list[Interface]) -> list[Result]:
    from .firewall import UNTRUSTED_ZONES

    # firewall-cmd is slow (~0.8s per call: it starts Python), so use as few calls as possible.
    active = env.run(["firewall-cmd", "--get-active-zones"]) if env.have("firewall-cmd") else (1, "")
    if active[0] == 0:
        zone_of = parse_active_zones(active[1])
        default = None
        out = []
        for i in lan:
            zone = zone_of.get(i.name)
            if zone is None:  # interface not listed: it belongs to the default zone
                default = default or env.run(["firewall-cmd", "--get-default-zone"])[1]
                zone = default
            if zone in UNTRUSTED_ZONES:
                out.append(Result(WARN, f"{i.name} is in the untrusted firewalld zone '{zone}'",
                                  fix=f"sudo firewall-cmd --permanent --zone=home --change-interface={i.name}"))
            rc, listing = env.run(["firewall-cmd", f"--zone={zone}", "--list-all"])
            if rc != 0:
                out.append(Result(SKIP, f"firewalld zone '{zone}'", "could not read its rules"))
                continue
            services, ports = parse_zone_listing(listing)
            for label, ok in [("file sharing", "samba" in services), ("tcp/5357", _covers(ports, 5357, "tcp")),
                              ("udp/3702", _covers(ports, 3702, "udp"))]:
                out.append(Result(OK, f"firewalld zone '{zone}' allows {label}") if ok else
                           Result(FAIL, f"firewalld zone '{zone}' blocks {label}", fix="sudo lan-share setup --apply"))
        return out
    if env.have("ufw"):
        if not env.root:
            return [Result(SKIP, "ufw rules", "needs root to read ufw status; re-run with sudo")]
        _, text = env.run(["ufw", "status"])
        if "inactive" in text.lower():
            return [Result(OK, "ufw is inactive (nothing blocks sharing)")]
        ok = all(s in text for s in ("445", "3702", "5357"))
        return [Result(OK if ok else FAIL, "ufw allows 445, 3702 and 5357" if ok else "ufw is missing rules for 445/3702/5357",
                       fix="" if ok else "sudo lan-share setup --apply")]
    return [Result(WARN, "No active firewalld/ufw found", "if you use nftables/iptables, allow tcp/445, udp/3702, tcp/5357 from your LAN")]


def check_shares(env: Env, cfg: Config) -> list[Result]:
    out = []
    if not cfg.shares:
        return [Result(WARN, "No shared folders configured", fix="lan-share share add NAME PATH --user USER")]
    selinux = env.run(["getenforce"])[1] if env.have("getenforce") else "Disabled"
    pdb = None
    if env.have("pdbedit") and env.root:
        rc, text = env.run(["pdbedit", "-L"])
        pdb = {ln.split(":")[0] for ln in text.splitlines() if ":" in ln} if rc == 0 else None
    for s in cfg.shares:
        p = env.ctx.path(s.path)
        if not p.is_dir():
            out.append(Result(FAIL, f"Share '{s.name}': {s.path} is not a directory"))
            continue
        out.append(Result(OK, f"Share '{s.name}' -> {s.path}"))
        if selinux in ("Enforcing", "Permissive"):
            _, ctxt = env.run(["stat", "-c", "%C", s.path])
            if "samba_share_t" in ctxt:
                out.append(Result(OK, f"  SELinux label on {s.path} allows Samba"))
            else:
                out.append(Result(FAIL if selinux == "Enforcing" else WARN, f"  SELinux label on {s.path} is not samba_share_t",
                                  ctxt, "sudo lan-share setup --apply"))
        if pdb is None:
            out.append(Result(SKIP, f"  Samba password for {', '.join(s.users)}", "needs root; re-run with sudo"))
        else:
            for u in s.users:
                if u in pdb:
                    out.append(Result(OK, f"  Samba password set for '{u}'"))
                else:
                    out.append(Result(FAIL, f"  '{u}' has no Samba password", fix=f"sudo lan-share user add {u}"))
    return out


def check_mounts(env: Env, cfg: Config) -> list[Result]:
    out = []
    for m in cfg.mounts:
        _, auto = mounts.unit_names(m)
        if env.run(["systemctl", "is-active", "--quiet", auto])[0] == 0:
            out.append(Result(OK, f"Mount '{m.name}' automount is active at {m.mountpoint}"))
        else:
            out.append(Result(FAIL, f"Mount '{m.name}' automount is not active",
                              fix=f"sudo systemctl enable --now '{auto}'"))
        if env.connect(m.host, 445):
            out.append(Result(OK, f"  {m.host}:445 is reachable"))
        else:
            out.append(Result(WARN, f"  {m.host}:445 is unreachable", "PC off, asleep, IP changed, or its firewall blocks file sharing",
                              "lan-share scan   (to find its current IP)"))
        if env.root and not env.ctx.path(m.credentials_path).exists():
            out.append(Result(FAIL, f"  credentials file for '{m.name}' is missing",
                              fix=f"sudo lan-share mount remove {m.name} && sudo lan-share mount add ..."))
    return out


WINDOWS_CHECKLIST = [
    "Network profile is Private (Settings > Network & internet > your connection).",
    "Network discovery is on (Control Panel > Network and Sharing Center > Advanced sharing settings).",
    "Function Discovery Resource Publication and SSDP Discovery services are running (services.msc).",
    "To let this laptop reach Windows: File and printer sharing is on, and the folder is shared to your account.",
]


def run_checks(env: Env, d: Distro, cfg: Config, lan: list[Interface]) -> list[Result]:
    results: list[Result] = []
    if d.family == "unknown":
        return [Result(FAIL, f"Unsupported distro: {d.name}")]
    results.append(Result(OK, f"Distro: {d.name} ({d.family})"))
    if lan:
        results.append(Result(OK, "LAN: " + ", ".join(f"{i.name} {i.address}" for i in lan)))
    else:
        results.append(Result(FAIL, "No private LAN interface found", "connect to wifi/ethernet on a 10.x/172.16-31.x/192.168.x network"))
    results += check_packages(env, d)
    results += check_wsdd(env)
    results += check_services(env, d)
    results += check_samba_config(env)
    results += check_listeners(env, d, lan)
    results += check_firewall(env, lan)
    results += check_shares(env, cfg)
    results += check_mounts(env, cfg)
    return results
