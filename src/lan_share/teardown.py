"""Undo `setup`: remove what lan-share added, and only that.

Files we own are always removed. Firewall rules, SELinux rules and enabled services are removed only
if the state file says setup added them (they may have existed before). Packages, Samba passwords and
the user's shared folders are never touched.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import mounts, samba, state, wsdd_service as W
from .config import Config
from .distro import Distro
from .mac import fcontext_regex
from .runner import Context, Plan, Step


def _strip_include(smb_conf: Path) -> None:
    if not smb_conf.exists():
        return
    lines = smb_conf.read_text().splitlines()
    out: list[str] = []
    for ln in lines:
        if ln.strip() == samba.INCLUDE_LINE:
            if out and out[-1].strip() == "# lan-share":
                out.pop()
            if out and out[-1].strip() == "":
                out.pop()  # the blank line we added before our marker
            continue
        out.append(ln)
    smb_conf.write_text("\n".join(out) + "\n")


def _rmdir_if_empty(p: Path) -> None:
    try:
        p.rmdir()
    except OSError:
        pass


def _remove_wsdd_files(ctx: Context) -> None:
    installed = ctx.path(W.INSTALL_PATH)
    installed.unlink(missing_ok=True)
    _rmdir_if_empty(installed.parent)
    ctx.path(W.UNIT_PATH).unlink(missing_ok=True)


def _forget(ctx: Context) -> None:
    ctx.path(state.STATE_PATH).unlink(missing_ok=True)


def _purge(ctx: Context) -> None:
    for d in ("/etc/lan-share", "/var/lib/lan-share"):
        shutil.rmtree(ctx.path(d), ignore_errors=True)


def plan_teardown(ctx: Context, distro: Distro, cfg: Config, keep_mounts: bool = False, purge: bool = False) -> Plan:
    plan = Plan()
    st = state.load(ctx)
    svc = distro.service_names

    # 1. Windows mounts (they live in units and credential files we created)
    if not keep_mounts:
        for m in cfg.mounts:
            plan.steps.extend(mounts.plan_remove(ctx, m).steps)

    # 2. WS-Discovery: our unit and bundled copy
    plan.steps.append(Step(f"Stop and disable {W.UNIT_NAME}", cmd=["systemctl", "disable", "--now", W.UNIT_NAME], tolerate_failure=True))
    plan.steps.append(Step("Remove the bundled wsdd and its unit", action=lambda: _remove_wsdd_files(ctx)))

    # 3. Samba: unhook our config (the user's own smb.conf content is preserved)
    plan.steps.append(Step(f"Remove the lan-share include from {samba.SMB_CONF}", action=lambda: _strip_include(ctx.path(samba.SMB_CONF))))
    plan.steps.append(Step(f"Delete {samba.CONF_PATH}", action=lambda: ctx.path(samba.CONF_PATH).unlink(missing_ok=True)))
    plan.steps.append(Step("Reload systemd", cmd=["systemctl", "daemon-reload"], tolerate_failure=True))
    plan.steps.append(Step("Reload Samba if running", cmd=["systemctl", "try-reload-or-restart", svc["samba"]], tolerate_failure=True))

    if st is None:
        plan.warnings.append(
            "no record of firewall/SELinux/service changes (this machine was set up before lan-share kept one, or never was). "
            "Those were NOT touched. Check manually: `firewall-cmd --list-all` (samba service, ports 3702/udp, 5357/tcp), "
            "`semanage fcontext -l -C` (samba_share_t rules), and `systemctl is-enabled smb avahi-daemon`."
        )
    else:
        # 4. Services we enabled (and nothing else)
        if st.enabled_units:
            plan.steps.append(Step(f"Disable services that lan-share enabled: {', '.join(st.enabled_units)}",
                                   cmd=["systemctl", "disable", "--now", *st.enabled_units], tolerate_failure=True))
        # 5. Firewall rules we added
        for zone, owned in st.firewalld.items():
            base = ["firewall-cmd", "--permanent", f"--zone={zone}"]
            for service in owned.get("services", []):
                plan.steps.append(Step(f"Close firewalld service {service!r} in zone {zone!r}", cmd=[*base, f"--remove-service={service}"], tolerate_failure=True))
            for port in owned.get("ports", []):
                plan.steps.append(Step(f"Close {port} in firewalld zone {zone!r}", cmd=[*base, f"--remove-port={port}"], tolerate_failure=True))
        if st.firewalld:
            plan.steps.append(Step("Reload firewalld", cmd=["firewall-cmd", "--reload"], tolerate_failure=True))
        for rule in st.ufw:
            plan.steps.append(Step("ufw: delete " + " ".join(rule), cmd=["ufw", "delete", *rule], tolerate_failure=True))
        # 6. SELinux rules we added
        for path in st.selinux:
            plan.steps.append(Step(f"SELinux: drop the samba_share_t rule for {path}",
                                   cmd=["semanage", "fcontext", "-d", fcontext_regex(path)], tolerate_failure=True))
            plan.steps.append(Step(f"SELinux: restore default labels under {path}", cmd=["restorecon", "-R", path], tolerate_failure=True))
        plan.steps.append(Step("Forget the recorded changes", action=lambda: _forget(ctx)))

    if purge:
        plan.steps.append(Step("Delete /etc/lan-share and /var/lib/lan-share (config, credentials, state)", action=lambda: _purge(ctx)))
    else:
        plan.warnings.append("kept /etc/lan-share/config.json (your share list); use --purge to delete it too")

    shares = ", ".join(s.name for s in cfg.shares)
    plan.warnings.append(
        "NOT removed: installed packages (samba, avahi, cifs-utils), Samba passwords "
        "(remove with `sudo pdbedit -x -u NAME`)" + (f", and the shared folders themselves ({shares})" if shares else "")
    )
    return plan
