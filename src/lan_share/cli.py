"""Command line interface."""

from __future__ import annotations

import argparse
import getpass
import os
import re
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import json

from . import LanShareError, __version__, config as cfgmod, discover, doctor as doctormod, distro as distromod, mounts, planner, samba, teardown
from .network import detect_lan
from .runner import Context, execute, which


def _ctx(args: argparse.Namespace, apply: bool = False) -> Context:
    root = Path(args.root or os.environ.get("LAN_SHARE_ROOT", "/"))
    return Context(root=root, apply=apply)


def cmd_detect(args: argparse.Namespace) -> None:
    ctx = _ctx(args)
    d = distromod.detect(ctx)
    print(f"distro:          {d.name} (family: {d.family}, package manager: {d.pkg_manager})")
    for logical in distromod.BINARIES:
        state = "installed" if distromod.is_installed(logical) else "missing"
        print(f"{logical + ':':<16} {state}")
    lan = detect_lan()
    print("LAN interfaces:  " + (", ".join(f"{i.name} {i.address} ({i.network})" for i in lan) or "none"))


def cmd_setup(args: argparse.Namespace) -> None:
    ctx = _ctx(args, apply=args.apply)
    d = distromod.detect(ctx)
    cfg = cfgmod.load(ctx)
    lan = planner.select_lan(cfg, args.interface)
    plan = planner.build_setup_plan(ctx, d, cfg, lan, args.zone, args.allow_untrusted_zone)
    print(plan.render())
    if not args.apply:
        print("\n(dry run: nothing was changed; add --apply to execute)")
        return
    print()
    execute(plan, ctx)
    print("\ndone.")


def cmd_teardown(args: argparse.Namespace) -> None:
    ctx = _ctx(args, apply=args.apply)
    d = distromod.detect(ctx)
    cfg = cfgmod.load(ctx)
    plan = teardown.plan_teardown(ctx, d, cfg, keep_mounts=args.keep_mounts, purge=args.purge)
    print(plan.render())
    if not args.apply:
        print("\n(dry run: nothing was changed; add --apply to execute)")
        return
    print()
    execute(plan, ctx)
    print("\ndone.")


def cmd_render(args: argparse.Namespace) -> None:
    ctx = _ctx(args)
    cfg = cfgmod.load(ctx)
    print(samba.render(cfg, planner.select_lan(cfg, args.interface)), end="")


def _finish_share_change(args: argparse.Namespace, saved: Path) -> None:
    if not args.apply:
        print(f"saved to {saved}; run `sudo lan-share setup --apply` to apply")
        return
    print(f"saved to {saved}; applying ...")
    cmd_setup(argparse.Namespace(root=args.root, apply=True, interface=None, zone=None, allow_untrusted_zone=False))


def cmd_gui(args: argparse.Namespace) -> None:
    try:
        from .gui.app import run
    except (ImportError, ValueError) as e:
        raise LanShareError(
            f"the GUI needs GTK 4 and libadwaita 1.5+ for Python ({e}). Install: "
            "Debian/Ubuntu: python3-gi gir1.2-gtk-4.0 gir1.2-adw-1; Fedora: python3-gobject gtk4 libadwaita; "
            "Arch: python-gobject gtk4 libadwaita; openSUSE: python313-gobject (match your Python) typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1"
        )
    sys.exit(run())


def cmd_share_add(args: argparse.Namespace) -> None:
    ctx = _ctx(args)
    cfg = cfgmod.load(ctx)
    path = str(Path(args.path).expanduser().resolve())
    if not Path(path).is_dir() and ctx.real:
        raise LanShareError(f"not a directory: {path}")
    cfg.add_share(cfgmod.Share(args.name, path, args.user, read_only=not args.rw))
    saved = cfgmod.save(ctx, cfg)
    _finish_share_change(args, saved)


def cmd_share_remove(args: argparse.Namespace) -> None:
    ctx = _ctx(args)
    cfg = cfgmod.load(ctx)
    cfg.remove_share(args.name)
    saved = cfgmod.save(ctx, cfg)
    _finish_share_change(args, saved)


def cmd_share_list(args: argparse.Namespace) -> None:
    cfg = cfgmod.load(_ctx(args))
    if args.json:
        print(json.dumps([asdict(sh) for sh in cfg.shares]))
        return
    if not cfg.shares:
        print("no shares configured")
    for s in cfg.shares:
        print(f"{s.name}\t{s.path}\t{'ro' if s.read_only else 'rw'}\t{','.join(s.users)}")


def cmd_user_add(args: argparse.Namespace) -> None:
    if os.geteuid() != 0:
        raise LanShareError("needs root; re-run with sudo")
    if not which("smbpasswd"):
        raise LanShareError("smbpasswd not found; run `lan-share setup --apply` first")
    if args.password_stdin:
        pw = sys.stdin.readline().rstrip("\n")
        if not pw:
            raise LanShareError("empty password")
        # -s reads the new password (twice) from stdin instead of the terminal
        res = subprocess.run(["smbpasswd", "-a", "-s", args.name], input=f"{pw}\n{pw}\n", text=True)
        sys.exit(res.returncode)
    # Interactive: smbpasswd prompts for the password itself, so we never handle it.
    sys.exit(subprocess.run(["smbpasswd", "-a", args.name]).returncode)


def _password(prompt: str, from_stdin: bool) -> str:
    """Read a password. --password-stdin lets a GUI pipe it in; it is never taken from argv."""
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    return getpass.getpass(prompt)


def _login_user() -> str:
    return os.environ.get("SUDO_USER") or getpass.getuser()


def _need_root(ctx: Context) -> None:
    if ctx.real and os.geteuid() != 0:
        raise LanShareError("needs root; re-run with sudo")


def cmd_scan(args: argparse.Namespace) -> None:
    cfg = cfgmod.load(_ctx(args))
    lan = planner.select_lan(cfg, args.interface)
    if not args.json:
        print(f"scanning {', '.join(i.name for i in lan)} for {args.timeout:g}s ...")
    hosts = discover.scan([i.name for i in lan], args.timeout)
    own = {str(i.address) for i in lan}
    hosts = [h for h in hosts if h.address not in own]
    if args.json:
        print(json.dumps([{"address": h.address, "name": h.name, "group": h.group} for h in hosts]))
        return
    if not hosts:
        print("no hosts found (is Network discovery on, and the Windows profile Private?)")
    for h in hosts:
        print(f"{h.address:<16} {h.name:<24} {h.group}")


def cmd_shares(args: argparse.Namespace) -> None:
    password = _password(f"Password for {args.user}@{args.host}: ", args.password_stdin)
    found = discover.list_shares(args.host, args.user, password, args.domain)
    if args.json:
        print(json.dumps([{"name": n, "comment": c} for n, c in found]))
        return
    if not found:
        print("no disk shares found")
    for name, comment in found:
        print(f"{name}\t{comment}")


_UNC = re.compile(r"^(?://|\\\\)([^/\\]+)[/\\](.+)$")


def cmd_mount_add(args: argparse.Namespace) -> None:
    ctx = _ctx(args, apply=True)
    _need_root(ctx)
    m = _UNC.match(args.unc)
    if not m:
        raise LanShareError(f"expected //HOST/SHARE, got {args.unc!r}")
    cfg = cfgmod.load(ctx)
    mount = cfgmod.Mount(args.name, m[1], m[2], args.user, args.local_user or _login_user(), args.domain or "")
    mount.validate()
    if any(x.name == mount.name for x in cfg.mounts):
        raise LanShareError(f"mount {mount.name!r} already exists; remove it first")
    password = _password(f"Password for {mount.user}@{mount.host}: ", args.password_stdin)
    if not password:
        raise LanShareError("empty password")
    if ctx.real and not args.skip_check:
        shares = [n for n, _ in discover.list_shares(mount.host, mount.user, password, mount.domain)]
        if mount.share.lower() not in (n.lower() for n in shares):
            raise LanShareError(f"login works, but {mount.host} has no share {mount.share!r} (found: {', '.join(shares) or 'none'})")
        print("credentials OK, share exists")
    plan = mounts.plan_add(ctx, mount, password)
    execute(plan, ctx)
    cfg.add_mount(mount)
    cfgmod.save(ctx, cfg)
    print(f"\nmounted on first access: ls {mount.mountpoint}")


def cmd_mount_remove(args: argparse.Namespace) -> None:
    ctx = _ctx(args, apply=True)
    _need_root(ctx)
    cfg = cfgmod.load(ctx)
    mount = cfg.remove_mount(args.name)
    execute(mounts.plan_remove(ctx, mount), ctx)
    cfgmod.save(ctx, cfg)
    print(f"removed {mount.name}")


def cmd_mount_list(args: argparse.Namespace) -> None:
    cfg = cfgmod.load(_ctx(args))
    if args.json:
        print(json.dumps([{**asdict(m), "mountpoint": m.mountpoint} for m in cfg.mounts]))
        return
    if not cfg.mounts:
        print("no mounts configured")
    for m in cfg.mounts:
        print(f"{m.name}\t//{m.host}/{m.share}\t{m.mountpoint}\t{m.user}")


_MARK = {"ok": "\u2713", "warn": "!", "fail": "\u2717", "skip": "-"}


def cmd_doctor(args: argparse.Namespace) -> None:
    ctx = _ctx(args)
    d = distromod.detect(ctx)
    cfg = cfgmod.load(ctx)
    results = doctormod.run_checks(doctormod.Env(ctx), d, cfg, detect_lan())
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        for r in results:
            print(f"[{_MARK[r.status]}] {r.name}" + (f" ({r.detail})" if r.detail else ""))
            if r.fix and r.status in ("warn", "fail"):
                print(f"      fix: {r.fix}")
        counts = {k: sum(r.status == k for r in results) for k in _MARK}
        print(f"\n{counts['ok']} ok, {counts['warn']} warnings, {counts['fail']} failed, {counts['skip']} skipped")
        if counts["skip"] and os.geteuid() != 0:
            print("(some checks need root: run with sudo for the full picture)")
        print("\nWindows side (cannot be checked from here):")
        for line in doctormod.WINDOWS_CHECKLIST:
            print(f"  - {line}")
    if any(r.status == "fail" for r in results):
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lan-share", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--root", help="operate on a staged filesystem root instead of / (commands are skipped)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("detect", help="show distro, installed components and LAN interfaces").set_defaults(func=cmd_detect)

    s = sub.add_parser("setup", help="install and configure everything (dry run unless --apply)")
    s.add_argument("--apply", action="store_true", help="actually make the changes (needs root)")
    s.add_argument("--interface", action="append", help="restrict to this interface (repeatable)")
    s.add_argument("--zone", help="firewalld zone to open (default: zone of the LAN interface)")
    s.add_argument("--allow-untrusted-zone", action="store_true", help="allow opening public/external/dmz zones")
    s.set_defaults(func=cmd_setup)

    td = sub.add_parser("teardown", help="undo setup: remove what lan-share added (dry run unless --apply)")
    td.add_argument("--apply", action="store_true", help="actually make the changes (needs root)")
    td.add_argument("--keep-mounts", action="store_true", help="leave configured Windows mounts in place")
    td.add_argument("--purge", action="store_true", help="also delete /etc/lan-share (share list, credentials) and state")
    td.set_defaults(func=cmd_teardown)

    r = sub.add_parser("render", help="print the generated Samba config")
    r.add_argument("--interface", action="append")
    r.set_defaults(func=cmd_render)

    sh = sub.add_parser("share", help="manage shared folders").add_subparsers(dest="share_command", required=True)
    a = sh.add_parser("add", help="share a folder")
    a.add_argument("name")
    a.add_argument("path")
    a.add_argument("--user", action="append", required=True, help="Linux user allowed in (repeatable)")
    a.add_argument("--rw", action="store_true", help="read-write (default: read-only)")
    a.add_argument("--apply", action="store_true", help="also run `setup --apply` right away (needs root)")
    a.set_defaults(func=cmd_share_add)
    rm = sh.add_parser("remove", help="stop sharing a folder")
    rm.add_argument("name")
    rm.add_argument("--apply", action="store_true", help="also run `setup --apply` right away (needs root)")
    rm.set_defaults(func=cmd_share_remove)
    shl = sh.add_parser("list", help="list shares")
    shl.add_argument("--json", action="store_true")
    shl.set_defaults(func=cmd_share_list)

    sub.add_parser("gui", help="open the graphical interface (needs GTK 4 + libadwaita)").set_defaults(func=cmd_gui)

    dr = sub.add_parser("doctor", help="diagnose sharing problems end to end")
    dr.add_argument("--json", action="store_true", help="machine-readable output")
    dr.set_defaults(func=cmd_doctor)

    sc = sub.add_parser("scan", help="find Windows machines on the LAN (WS-Discovery)")
    sc.add_argument("--timeout", type=float, default=8.0)
    sc.add_argument("--interface", action="append")
    sc.add_argument("--json", action="store_true", help="machine-readable output")
    sc.set_defaults(func=cmd_scan)

    ls = sub.add_parser("shares", help="list the shared folders on a Windows machine")
    ls.add_argument("host")
    ls.add_argument("--user", required=True)
    ls.add_argument("--domain", default="")
    ls.add_argument("--password-stdin", action="store_true", help="read the password from stdin (for programs)")
    ls.add_argument("--json", action="store_true", help="machine-readable output")
    ls.set_defaults(func=cmd_shares)

    mt = sub.add_parser("mount", help="mount Windows shares on this machine").add_subparsers(dest="mount_command", required=True)
    ma = mt.add_parser("add", help="mount //HOST/SHARE at /mnt/lan-share/NAME on first access")
    ma.add_argument("name")
    ma.add_argument("unc", metavar="//HOST/SHARE")
    ma.add_argument("--user", required=True, help="Windows account (for a Microsoft account use the e-mail address)")
    ma.add_argument("--domain", default="")
    ma.add_argument("--local-user", help="local user who should own the files (default: the invoking user)")
    ma.add_argument("--password-stdin", action="store_true", help="read the password from stdin (for programs)")
    ma.add_argument("--skip-check", action="store_true", help="don't verify the login/share before saving")
    ma.set_defaults(func=cmd_mount_add)
    mr = mt.add_parser("remove", help="unmount and forget a share")
    mr.add_argument("name")
    mr.set_defaults(func=cmd_mount_remove)
    mtl = mt.add_parser("list", help="list configured mounts")
    mtl.add_argument("--json", action="store_true")
    mtl.set_defaults(func=cmd_mount_list)

    u = sub.add_parser("user", help="manage Samba users").add_subparsers(dest="user_command", required=True)
    ua = u.add_parser("add", help="set a Samba password for an existing Linux user")
    ua.add_argument("name")
    ua.add_argument("--password-stdin", action="store_true", help="read the password from stdin (for programs)")
    ua.set_defaults(func=cmd_user_add)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except LanShareError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
