"""Build the ordered setup plan from distro + config + current system state."""

from __future__ import annotations

from typing import Optional

from . import LanShareError, samba, state, wsdd_service
from .config import Config
from .distro import Distro, install_command, is_installed
from .firewall import plan_firewall
from .mac import plan_selinux, selinux_mode
from .network import Interface, detect_lan
from .runner import Context, Plan, Step, capture, file_step

def select_lan(cfg: Config, override: Optional[list[str]]) -> list[Interface]:
    lan = detect_lan()
    wanted = override or cfg.interfaces
    if wanted:
        lan = [i for i in lan if i.name in wanted]
        missing = set(wanted) - {i.name for i in lan}
        if missing:
            raise LanShareError(f"interface(s) not found or not on a private network: {', '.join(sorted(missing))}")
    if not lan:
        raise LanShareError("no private LAN interface found; connect to a network or pass --interface")
    return lan


def build_setup_plan(
    ctx: Context,
    distro: Distro,
    cfg: Config,
    lan: list[Interface],
    zone: Optional[str] = None,
    allow_untrusted_zone: bool = False,
) -> Plan:
    cfg.validate()
    plan = Plan()
    if distro.family == "unknown":
        raise LanShareError(f"unsupported distro {distro.name!r}: need a Debian, Fedora, Arch or SUSE family system")

    # 1. Packages (wsdd is bundled, not installed from the distro)
    wanted = ["samba", "avahi", "cifs", "smbclient"]
    if selinux_mode() != "Disabled" and distro.package("selinux-tools"):
        wanted.append("selinux-tools")
    missing = [distro.package(l) for l in wanted if not is_installed(l) and distro.package(l)]
    if missing:
        plan.steps.append(Step(f"Install packages: {', '.join(missing)}", cmd=install_command(distro, missing)))

    # 2. Samba config
    plan.steps.append(file_step(ctx, "Write managed Samba config", samba.CONF_PATH, samba.render(cfg, lan)))
    plan.steps.append(samba.include_step(ctx))
    plan.steps.append(Step("Validate Samba config", cmd=["testparm", "-s", "--suppress-prompt"]))

    # 3. SELinux
    plan_selinux(ctx, cfg.shares, plan)

    # 4. WS-Discovery: bundled wsdd under our own unit, advertising on the LAN interfaces only
    wsdd_service.plan_wsdd(ctx, distro.service_names["samba"], lan, cfg.interfaces, cfg.workgroup, plan)

    # 5. Firewall
    plan_firewall(ctx, lan, zone, allow_untrusted_zone, plan)

    # 6. Services
    svc = distro.service_names
    names = [svc["samba"], svc["avahi"], svc["wsdd"]]
    # Remember which of the generic services were NOT enabled before, so teardown only disables what we enabled.
    # (lan-share-wsdd is ours outright and always removed.)
    newly_enabled = [] if not ctx.real else [
        u for u in (svc["samba"], svc["avahi"]) if capture(["systemctl", "is-enabled", u]) != "enabled"
    ]
    plan.steps.append(Step(f"Enable and start: {', '.join(names)}", cmd=["systemctl", "enable", "--now", *names]))
    for unit in newly_enabled:
        plan.steps.append(Step(f"Record that {unit} was enabled by lan-share",
                               action=lambda u=unit: state.record_unit(ctx, u)))
    plan.steps.append(Step("Reload Samba configuration", cmd=["systemctl", "reload-or-restart", svc["samba"]]))
    plan.steps.append(Step("Restart wsdd to pick up its unit and arguments", cmd=["systemctl", "restart", svc["wsdd"]]))

    # 7. Reminders that can't be automated
    users = sorted({u for s in cfg.shares for u in s.users})
    if users:
        plan.warnings.append(
            "Samba passwords are separate from login passwords. Run `sudo lan-share user add <name>` "
            f"for: {', '.join(users)}"
        )
    if not cfg.shares:
        plan.warnings.append("no shares configured yet; add one with `lan-share share add NAME PATH --user USER`")
    return plan
