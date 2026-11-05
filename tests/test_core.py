import json
import tempfile
import unittest
from ipaddress import ip_address, ip_network
from pathlib import Path

from lan_share import LanShareError, config, distro, samba
from lan_share.mac import fcontext_regex
from lan_share.network import Interface, lan_interfaces, parse_ip_json
from lan_share.runner import Context


def os_release(**kw):
    return distro.from_os_release(kw)


class DistroTests(unittest.TestCase):
    def test_families(self):
        cases = [
            (dict(ID="fedora"), "fedora"),
            (dict(ID="rocky", ID_LIKE="rhel centos fedora"), "fedora"),
            (dict(ID="ubuntu", ID_LIKE="debian"), "debian"),
            (dict(ID="linuxmint", ID_LIKE="ubuntu debian"), "debian"),
            (dict(ID="manjaro", ID_LIKE="arch"), "arch"),
            (dict(ID="opensuse-tumbleweed", ID_LIKE="opensuse suse"), "suse"),
            (dict(ID="opensuse-leap"), "suse"),
            (dict(ID="alpine"), "unknown"),
        ]
        for fields, family in cases:
            self.assertEqual(os_release(**fields).family, family, fields)

    def test_parse_os_release(self):
        d = distro.parse_os_release('# c\nID=fedora\nPRETTY_NAME="Fedora Linux 44"\nVERSION_ID=44\n')
        self.assertEqual(d["PRETTY_NAME"], "Fedora Linux 44")

    def test_service_names(self):
        self.assertEqual(os_release(ID="debian").service_names["samba"], "smbd")
        self.assertEqual(os_release(ID="fedora").service_names["samba"], "smb")

    def test_install_commands(self):
        self.assertEqual(distro.install_command(os_release(ID="fedora"), ["a"])[:3], ["dnf", "install", "-y"])
        self.assertIn("--noconfirm", distro.install_command(os_release(ID="arch"), ["a"]))
        self.assertIn("--non-interactive", distro.install_command(os_release(ID="suse"), ["a"]))
        self.assertIn("DEBIAN_FRONTEND=noninteractive", distro.install_command(os_release(ID="debian"), ["a"]))


IP_JSON = json.dumps([
    {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]},
    {"ifname": "wlp3s0", "addr_info": [{"family": "inet", "local": "192.168.1.20", "prefixlen": 24},
                                       {"family": "inet6", "local": "fe80::1", "prefixlen": 64}]},
    {"ifname": "docker0", "addr_info": [{"family": "inet", "local": "172.17.0.1", "prefixlen": 16}]},
    {"ifname": "tailscale0", "addr_info": [{"family": "inet", "local": "100.64.0.5", "prefixlen": 10}]},
    {"ifname": "eth0", "addr_info": [{"family": "inet", "local": "203.0.113.5", "prefixlen": 24}]},
    {"ifname": "eth1", "addr_info": [{"family": "inet", "local": "169.254.3.4", "prefixlen": 16}]},
])


class NetworkTests(unittest.TestCase):
    def test_only_real_private_lans(self):
        lan = lan_interfaces(parse_ip_json(IP_JSON))
        self.assertEqual([i.name for i in lan], ["wlp3s0"])
        self.assertEqual(lan[0].network, ip_network("192.168.1.0/24"))


    def test_ioctl_fallback_sees_real_interfaces(self):
        from lan_share.network import interfaces_via_ioctl

        ifaces = interfaces_via_ioctl()
        lo = [i for i in ifaces if i.name == "lo"]
        self.assertEqual(str(lo[0].address), "127.0.0.1")


class ConfigTests(unittest.TestCase):
    def test_share_validation(self):
        bad = [
            config.Share("global", "/srv/x", ["bob"]),
            config.Share("ok", "relative", ["bob"]),
            config.Share("ok", "/srv/x\n[evil]", ["bob"]),
            config.Share("ok", "/srv/%U", ["bob"]),
            config.Share("ok", "/srv/x", []),
            config.Share("ok", "/srv/x", ["bad user"]),
            config.Share("bad]\n[x", "/srv/x", ["bob"]),
        ]
        for s in bad:
            with self.assertRaises(LanShareError, msg=s):
                s.validate()
        config.Share("Media 1", "/srv/media", ["bob", "amy"]).validate()

    def test_duplicates_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = Context(root=Path(d))
            cfg = config.Config()
            cfg.add_share(config.Share("docs", "/srv/docs", ["bob"], read_only=False))
            with self.assertRaises(LanShareError):
                cfg.add_share(config.Share("DOCS", "/srv/other", ["bob"]))
            config.save(ctx, cfg)
            self.assertEqual(config.load(ctx).shares[0].path, "/srv/docs")
            cfg.remove_share("docs")
            with self.assertRaises(LanShareError):
                cfg.remove_share("docs")


class SambaTests(unittest.TestCase):
    LAN = [Interface("wlp3s0", ip_address("192.168.1.20"), ip_network("192.168.1.0/24"))]

    def test_render_is_locked_down(self):
        cfg = config.Config()
        cfg.add_share(config.Share("docs", "/srv/docs", ["bob"]))
        cfg.add_share(config.Share("rw", "/srv/rw", ["bob", "amy"], read_only=False))
        out = samba.render(cfg, self.LAN)
        for needle in [
            "server min protocol = SMB2_02", "server signing = mandatory", "map to guest = Never",
            "bind interfaces only = yes", "interfaces = lo wlp3s0",
            "hosts allow = 127.0.0.1 192.168.1.0/24", "hosts deny = ALL", "disable netbios = yes",
            "[docs]", "path = /srv/docs", "valid users = bob", "read only = yes",
            "[rw]", "valid users = bob amy", "read only = no", "guest ok = no",
        ]:
            self.assertIn(needle, out)
        self.assertNotIn("guest ok = yes", out)

    def test_include_is_idempotent_and_backs_up(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = Context(root=Path(d))
            conf = ctx.path(samba.SMB_CONF)
            conf.parent.mkdir(parents=True)
            conf.write_text("[global]\n   workgroup = HOME\n[printers]\n   path = /var/tmp")
            samba.ensure_include(ctx)
            samba.ensure_include(ctx)
            text = conf.read_text()
            self.assertEqual(text.count(samba.INCLUDE_LINE), 1)
            self.assertTrue(text.startswith("[global]\n   workgroup = HOME"))
            self.assertIn("path = /var/tmp", conf.with_name("smb.conf.lan-share.bak").read_text())

    def test_include_creates_missing_conf(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = Context(root=Path(d))
            samba.ensure_include(ctx)
            self.assertIn(samba.INCLUDE_LINE, ctx.path(samba.SMB_CONF).read_text())


class MacTests(unittest.TestCase):
    def test_fcontext_escapes_regex(self):
        self.assertEqual(fcontext_regex("/home/a.b/Docs/"), r"/home/a\.b/Docs(/.*)?")


class ReverseDirectionTests(unittest.TestCase):
    M = config.Mount("pc", "192.168.1.50", "Docs", "alice", "alice")

    def test_escape_path_matches_systemd(self):
        import shutil
        import subprocess

        from lan_share.mounts import escape_path

        paths = ["/mnt/lan-share/my-pc.docs_1", "/mnt/lan-share/pc", "/mnt/a b/c", "/.hidden/x"]
        if shutil.which("systemd-escape"):
            for p in paths:
                want = subprocess.run(["systemd-escape", "-p", p], capture_output=True, text=True).stdout.strip()
                self.assertEqual(escape_path(p), want, p)

    def test_units(self):
        from lan_share import mounts

        mount, auto = mounts.render_units(self.M, 1000, 1000)
        self.assertIn("What=//192.168.1.50/Docs", mount)
        self.assertIn("Where=/mnt/lan-share/pc", mount)
        self.assertIn("credentials=/etc/lan-share/credentials/pc", mount)
        self.assertNotIn("password", mount)
        self.assertIn("Where=/mnt/lan-share/pc", auto)
        self.assertEqual(mounts.unit_names(self.M)[1], "mnt-lan\\x2dshare-pc.automount")

    def test_plan_hides_password_and_stages_private_files(self):
        import os

        from lan_share import mounts
        from lan_share.runner import execute

        with tempfile.TemporaryDirectory() as d:
            ctx = Context(root=Path(d), apply=True)
            plan = mounts.plan_add(ctx, self.M, "s3cret!")
            self.assertNotIn("s3cret", plan.render())
            execute(plan, ctx, log=lambda _: None)
            cred = ctx.path(self.M.credentials_path)
            self.assertEqual(oct(os.stat(cred).st_mode & 0o777), "0o600")
            self.assertEqual(oct(os.stat(cred.parent).st_mode & 0o777), "0o700")
            self.assertIn("password=s3cret!", cred.read_text())
            execute(mounts.plan_remove(ctx, self.M), ctx, log=lambda _: None)
            self.assertFalse(cred.exists())

    def test_mount_validation(self):
        for bad in [
            config.Mount("Bad Name", "h", "s", "u", "alice"),
            config.Mount("ok", "host;rm", "s", "u", "alice"),
            config.Mount("ok", "h", "s\nx", "u", "alice"),
            config.Mount("ok", "h", "s", "u,uid=0", "alice"),
        ]:
            with self.assertRaises(LanShareError, msg=bad):
                bad.validate()

    def test_parse_wsdd_and_smbclient(self):
        from lan_share import discover

        out = ("2026 INFO: discovered DESKTOP-OFFICE in Workgroup:WORKGROUP on 192.168.1.50\n"
               "noise\n2026 INFO: discovered NAS in Domain:corp.example on 192.168.1.9\n")
        hosts = discover.parse_wsdd_output(out)
        self.assertEqual([(h.name, h.address) for h in hosts],
                         [("NAS", "192.168.1.9"), ("DESKTOP-OFFICE", "192.168.1.50")])
        shares = discover.parse_smbclient_shares("Disk|Docs|my docs\nDisk|C$|Default share\nIPC|IPC$|\nPrinter|HP|\n")
        self.assertEqual(shares, [("Docs", "my docs")])


class WsddServiceTests(unittest.TestCase):
    def test_unit_contents(self):
        from lan_share import wsdd_service as W

        unit = W.render_unit("/usr/bin/python3", "smbd", ["wlp3s0"], "WORKGROUP")
        self.assertIn("ExecStart=/usr/bin/python3 /usr/local/lib/lan-share/wsdd.py --shortlog "
                      "--chroot=/run/lan-share-wsdd -4 -i wlp3s0 -w WORKGROUP", unit)
        self.assertIn("BindsTo=smbd.service", unit)
        self.assertIn("DynamicUser=yes", unit)

    def test_rejects_unsafe_interface_names(self):
        from lan_share import wsdd_service as W

        for bad in ["eth0 --evil", "eth0\nExecStartPost=/bin/sh", "-i", "", "a" * 16]:
            with self.assertRaises(LanShareError, msg=bad):
                W.render_unit("/usr/bin/python3", "smb", [bad], "WORKGROUP")

    def test_vendored_copy_is_present_and_licensed(self):
        from lan_share import wsdd_service as W

        self.assertIn("Steffen Christgau", W.VENDORED.read_text())
        self.assertIn("MIT", (W.VENDORED.parent / "LICENSE-wsdd").read_text())

    def test_plan_installs_files_and_restores_old_config(self):
        from lan_share import wsdd_service as W
        from lan_share.runner import Plan, execute

        with tempfile.TemporaryDirectory() as d:
            ctx = Context(root=Path(d), apply=True)
            cfg = ctx.path("/etc/sysconfig/wsdd")
            cfg.parent.mkdir(parents=True)
            cfg.write_text("# Managed by lan-share (original: ...)\nOPTIONS=\"-i x\"\n")
            cfg.with_name("wsdd.lan-share.bak").write_text('OPTIONS=""\n')
            plan = Plan()
            lan = [Interface("wlp3s0", ip_address("192.168.1.20"), ip_network("192.168.1.0/24"))]
            W.plan_wsdd(ctx, "smb", lan, [], "WORKGROUP", plan)
            execute(plan, ctx, log=lambda _: None)
            self.assertEqual(cfg.read_text(), 'OPTIONS=""\n')
            installed = ctx.path(W.INSTALL_PATH)
            self.assertEqual(installed.read_text(), W.VENDORED.read_text())
            self.assertTrue(installed.stat().st_mode & 0o111)
            self.assertIn("-i wlp3s0", ctx.path(W.UNIT_PATH).read_text())
            self.assertNotIn("v0.8\n" + "#!/usr", plan.render())  # file body is never dumped into the plan


class ExecuteTests(unittest.TestCase):
    def test_staged_root_skips_system_actions_and_commands(self):
        from lan_share.runner import Plan, Step, execute

        ran = []
        plan = Plan([
            Step("sys", action=lambda: ran.append("sys"), touches_system=True),
            Step("file", action=lambda: ran.append("file")),
            Step("cmd", cmd=["false"]),
        ])
        with tempfile.TemporaryDirectory() as d:
            execute(plan, Context(root=Path(d), apply=True), log=lambda _: None)
        self.assertEqual(ran, ["file"])


if __name__ == "__main__":
    unittest.main()
