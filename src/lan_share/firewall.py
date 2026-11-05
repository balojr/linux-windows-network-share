"""Firewall rules: SMB (445/tcp) and WS-Discovery (3702/udp, 5357/tcp), LAN only."""

from __future__ import annotations

from typing import Callable, Optional

from . import LanShareError, state
from .network import Interface
from .runner import Context, Plan, Step, capture, which

# Opening file sharing on these zones would expose it beyond a trusted LAN.
UNTRUSTED_ZONES = {"public", "external", "dmz"}

WSD_PORTS = [("3702", "udp"), ("5357", "tcp")]


def detect_backend() -> Optional[str]:
    if which("firewall-cmd") and capture(["firewall-cmd", "--state"]) == "running":
        return "firewalld"
    if which("ufw"):
        return "ufw"
    return None


def _zones_for(lan: list[Interface]) -> dict[str, list[str]]:
    zones: dict[str, list[str]] = {}
    default = capture(["firewall-cmd", "--get-default-zone"]) or ""
    for i in lan:
        z = capture(["firewall-cmd", "--get-zone-of-interface=" + i.name]) or default
        zones.setdefault(z, []).append(i.name)
    return zones


def _yes(cmd: list[str]) -> bool:
    return capture(cmd) == "yes"


def plan_firewalld(
    ctx: Context, lan: list[Interface], zone: Optional[str], allow_untrusted: bool, plan: Plan,
    query: Callable[[list[str]], bool] = _yes,
) -> None:
    zones = {zone: []} if zone else _zones_for(lan)
    for z in zones:
        if z in UNTRUSTED_ZONES and not allow_untrusted:
            raise LanShareError(
                f"firewalld zone {z!r} is not a trusted zone; move the interface to a home/work zone "
                f"(e.g. `firewall-cmd --permanent --zone=home --change-interface=<if>`), "
                f"or pass --allow-untrusted-zone to override"
            )
    changed = False
    for z in zones:
        # firewall-cmd rejects --add-service and --add-port in one call. Ports are opened
        # directly rather than via named services, whose names vary across firewalld versions.
        base = ["firewall-cmd", "--permanent", f"--zone={z}"]
        owned = (state.load(ctx) or state.State()).firewalld.get(z, {"services": [], "ports": []})
        if "samba" in owned["services"] or query([*base, "--query-service=samba"]):
            pass  # already open: either ours from an earlier run, or pre-existing (never ours to remove)
        else:
            plan.steps.append(Step(f"Open file sharing in firewalld zone {z!r}", cmd=[*base, "--add-service=samba"]))
            plan.steps.append(Step("Record it", action=lambda z=z: state.record_firewalld(ctx, z, service="samba")))
            changed = True
        for port, proto in WSD_PORTS:
            spec = f"{port}/{proto}"
            if spec in owned["ports"] or query([*base, f"--query-port={spec}"]):
                continue
            plan.steps.append(Step(f"Open {spec} (WS-Discovery) in firewalld zone {z!r}", cmd=[*base, f"--add-port={spec}"]))
            plan.steps.append(Step("Record it", action=lambda z=z, spec=spec: state.record_firewalld(ctx, z, port=spec)))
            changed = True
    if changed:
        plan.steps.append(Step("Reload firewalld", cmd=["firewall-cmd", "--reload"]))


def plan_ufw(ctx: Context, lan: list[Interface], plan: Plan) -> None:
    for net in sorted({str(i.network) for i in lan}):
        rules = [("tcp", "445")] + [(proto, p) for p, proto in WSD_PORTS]
        for proto, port in rules:
            rule = ["allow", "proto", proto, "from", net, "to", "any", "port", port]
            plan.steps.append(Step(f"ufw: allow {proto}/{port} from {net}", cmd=["ufw", *rule]))
            plan.steps.append(Step("Record it", action=lambda rule=rule: state.record_ufw(ctx, rule)))


def plan_firewall(
    ctx: Context, lan: list[Interface], zone: Optional[str], allow_untrusted: bool, plan: Plan
) -> None:
    backend = detect_backend()
    if backend == "firewalld":
        plan_firewalld(ctx, lan, zone, allow_untrusted, plan)
    elif backend == "ufw":
        plan_ufw(ctx, lan, plan)
    else:
        plan.warnings.append(
            "no active firewalld/ufw detected; if you use nftables/iptables directly, allow "
            "tcp/445, udp/3702 and tcp/5357 from your LAN yourself"
        )
