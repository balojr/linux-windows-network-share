"""LAN interface detection."""

from __future__ import annotations

import fcntl
import ipaddress
import json
import socket
import struct
from dataclasses import dataclass

from .runner import capture

# Virtual/overlay interfaces that are not "the LAN" and must never serve shares.
_VIRTUAL_PREFIXES = (
    "lo", "docker", "veth", "br-", "virbr", "vmnet", "vboxnet", "tun", "tap",
    "podman", "cni", "flannel", "tailscale", "wg", "zt", "kube",
)


# Python's is_private also accepts documentation/benchmark/CGNAT-adjacent ranges; we want RFC1918 only.
_RFC1918 = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]


@dataclass(frozen=True)
class Interface:
    name: str
    address: ipaddress.IPv4Address
    network: ipaddress.IPv4Network


def parse_ip_json(text: str) -> list[Interface]:
    out: list[Interface] = []
    for link in json.loads(text):
        for a in link.get("addr_info", []):
            if a.get("family") != "inet":
                continue
            iface = ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}")
            out.append(Interface(link["ifname"], iface.ip, iface.network))
    return out


def lan_interfaces(ifaces: list[Interface]) -> list[Interface]:
    """Physical-looking interfaces with an RFC1918 address."""
    return [
        i for i in ifaces
        if not i.name.startswith(_VIRTUAL_PREFIXES)
        and any(i.address in n for n in _RFC1918)
    ]


_SIOCGIFADDR, _SIOCGIFNETMASK = 0x8915, 0x891B


def interfaces_via_ioctl() -> list[Interface]:
    """IPv4 addresses without the `ip` tool (absent on minimal images): stdlib + ioctl."""
    out: list[Interface] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for _, name in socket.if_nameindex():
            req = struct.pack("256s", name.encode()[:15])
            try:
                addr = socket.inet_ntoa(fcntl.ioctl(s, _SIOCGIFADDR, req)[20:24])
                mask = socket.inet_ntoa(fcntl.ioctl(s, _SIOCGIFNETMASK, req)[20:24])
            except OSError:  # interface has no IPv4 address
                continue
            iface = ipaddress.ip_interface(f"{addr}/{mask}")
            out.append(Interface(name, iface.ip, iface.network))
    return out


def detect_lan() -> list[Interface]:
    text = capture(["ip", "-j", "-4", "addr"])
    try:
        found = parse_ip_json(text) if text else interfaces_via_ioctl()
    except (ValueError, KeyError):
        found = interfaces_via_ioctl()
    return lan_interfaces(found)
