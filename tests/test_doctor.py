import tempfile
import unittest
from ipaddress import ip_address, ip_network
from pathlib import Path

from lan_share import config, doctor
from lan_share.distro import from_os_release
from lan_share.network import Interface
from lan_share.runner import Context

LAN = [Interface("wlp3s0", ip_address("192.168.1.20"), ip_network("192.168.1.0/24"))]
FEDORA = from_os_release({"ID": "fedora"})

SS_GOOD = """\
tcp LISTEN 0 50 192.168.1.20:445 0.0.0.0:*
tcp LISTEN 0 50 127.0.0.1:445 0.0.0.0:*
tcp LISTEN 0 50 [::1]:445 [::]:*
udp UNCONN 0 0 192.168.1.20:3702 0.0.0.0:*
tcp LISTEN 0 5 192.168.1.20:5357 0.0.0.0:*
"""
HOME_LISTING = """home (active)
  target: default
  interfaces: wlp3s0
  services: dhcpv6-client samba ssh
  ports: 3702/udp 5357/tcp
  protocols:
"""
TESTPARM_GOOD = "[global]\n\tbind interfaces only = Yes\n\thosts deny = ALL\n\tserver signing = required\n"


class FakeSystem:
    """Maps command prefixes to (rc, stdout); anything unknown fails with rc 127."""

    def __init__(self, table):
        self.table = table

    def run(self, cmd):
        for prefix, result in self.table.items():
            if tuple(cmd[: len(prefix)]) == prefix:
                return result
        return 127, ""


def good_table(overrides=None):
    t = {
        ("systemctl", "is-active"): (0, ""),
        ("systemctl", "is-enabled"): (0, "enabled"),
        ("testparm",): (0, TESTPARM_GOOD),
        ("ss",): (0, SS_GOOD),
        ("firewall-cmd", "--get-active-zones"): (0, "home\n  interfaces: wlp3s0"),
        ("firewall-cmd", "--get-default-zone"): (0, "home"),
        ("firewall-cmd", "--zone=home", "--list-all"): (0, HOME_LISTING),
        ("getenforce",): (0, "Enforcing"),
        ("stat",): (0, "unconfined_u:object_r:samba_share_t:s0"),
        ("pdbedit",): (0, "bob:1000:Bob"),
    }
    t.update(overrides or {})
    return t


def make_env(root, table, euid=0, reachable=True, tools=None):
    fs = FakeSystem(table)
    tools = tools if tools is not None else {"testparm", "firewall-cmd", "getenforce", "pdbedit"}
    return doctor.Env(Context(root=root), run=fs.run, connect=lambda h, p: reachable,
                      euid=lambda: euid, have=lambda n: f"/usr/bin/{n}" if n in tools else None)


def by_name(results, fragment):
    hits = [r for r in results if fragment in r.name]
    assert hits, f"no result containing {fragment!r}: {[r.name for r in results]}"
    return hits


class ParserTests(unittest.TestCase):
    def test_parse_listeners(self):
        ls = doctor.parse_listeners(SS_GOOD)
        self.assertIn(("tcp", "192.168.1.20", 445), ls)
        self.assertIn(("tcp", "::1", 445), ls)
        self.assertIn(("udp", "192.168.1.20", 3702), ls)

    def test_parse_testparm(self):
        self.assertEqual(doctor.parse_testparm(TESTPARM_GOOD)["hosts deny"], "ALL")


class CheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "etc/samba").mkdir(parents=True)
        (self.root / "etc/samba/smb.conf").write_text("[global]\ninclude = /etc/samba/lan-share.conf\n")
        (self.root / "srv/docs").mkdir(parents=True)
        self.cfg = config.Config()
        self.cfg.add_share(config.Share("docs", "/srv/docs", ["bob"]))
        self.addCleanup(self._tmp.cleanup)

    def statuses(self, results, fragment):
        return [r.status for r in by_name(results, fragment)]

    def test_everything_healthy(self):
        env = make_env(self.root, good_table())
        for fn, args in [(doctor.check_services, (FEDORA,)), (doctor.check_samba_config, ()),
                         (doctor.check_listeners, (FEDORA, LAN)), (doctor.check_firewall, (LAN,)),
                         (doctor.check_shares, (self.cfg,))]:
            bad = [r for r in fn(env, *args) if r.status in ("warn", "fail")]
            self.assertEqual(bad, [], fn.__name__)

    def test_service_down_and_not_enabled(self):
        env = make_env(self.root, good_table({("systemctl", "is-active"): (3, "")}))
        res = doctor.check_services(env, FEDORA)
        self.assertEqual(self.statuses(res, "Samba service"), ["fail"])
        self.assertIn("systemctl enable --now smb", by_name(res, "Samba service")[0].fix)
        env = make_env(self.root, good_table({("systemctl", "is-enabled"): (1, "disabled")}))
        self.assertEqual(self.statuses(doctor.check_services(env, FEDORA), "wsdd"), ["warn"])

    def test_missing_include_and_bad_config(self):
        (self.root / "etc/samba/smb.conf").write_text("[global]\n")
        res = doctor.check_samba_config(make_env(self.root, good_table()))
        self.assertEqual(res[0].status, "fail")
        (self.root / "etc/samba/smb.conf").write_text("[global]\ninclude = /etc/samba/lan-share.conf\n")
        res = doctor.check_samba_config(make_env(self.root, good_table({("testparm",): (1, "")})))
        self.assertEqual(res[0].status, "fail")

    def test_weak_config_warns(self):
        weak = "[global]\n\tserver signing = auto\n"
        res = doctor.check_samba_config(make_env(self.root, good_table({("testparm",): (0, weak)})))
        self.assertEqual(self.statuses(res, "signing"), ["warn"])
        self.assertEqual(self.statuses(res, "LAN interface"), ["warn"])

    def test_listener_problems(self):
        # smb on all interfaces, wsdd missing
        ss = "tcp LISTEN 0 50 0.0.0.0:445 0.0.0.0:*\n"
        res = doctor.check_listeners(make_env(self.root, good_table({("ss",): (0, ss)})), FEDORA, LAN)
        self.assertIn("warn", self.statuses(res, "ALL interfaces"))
        self.assertEqual(self.statuses(res, "3702"), ["fail"])
        self.assertEqual(self.statuses(res, "5357"), ["fail"])
        # nothing on 445
        res = doctor.check_listeners(make_env(self.root, good_table({("ss",): (0, "")})), FEDORA, LAN)
        self.assertEqual(self.statuses(res, "port 445"), ["fail"])
        # only on loopback
        ss = "tcp LISTEN 0 50 127.0.0.1:445 0.0.0.0:*\n"
        res = doctor.check_listeners(make_env(self.root, good_table({("ss",): (0, ss)})), FEDORA, LAN)
        self.assertEqual(self.statuses(res, "LAN address"), ["fail"])

    def test_firewall_blocks_and_untrusted_zone(self):
        closed = "home (active)\n  services: ssh\n  ports: 8080/tcp\n"
        env = make_env(self.root, good_table({("firewall-cmd", "--zone=home", "--list-all"): (0, closed)}))
        res = doctor.check_firewall(env, LAN)
        self.assertEqual(sorted(set(self.statuses(res, "blocks"))), ["fail"])
        self.assertEqual(len(by_name(res, "blocks")), 3)
        env = make_env(self.root, good_table({
            ("firewall-cmd", "--get-active-zones"): (0, "public\n  interfaces: wlp3s0"),
            ("firewall-cmd", "--zone=public", "--list-all"): (0, HOME_LISTING)}))
        self.assertIn("warn", self.statuses(doctor.check_firewall(env, LAN), "untrusted"))

    def test_firewall_port_ranges_count_and_default_zone_fallback(self):
        listing = "work (active)\n  services: samba\n  ports: 1025-65535/tcp 1025-65535/udp\n"
        env = make_env(self.root, good_table({
            ("firewall-cmd", "--get-active-zones"): (0, "docker\n  interfaces: docker0"),  # wlp3s0 not listed -> default zone
            ("firewall-cmd", "--get-default-zone"): (0, "work"),
            ("firewall-cmd", "--zone=work", "--list-all"): (0, listing)}))
        res = doctor.check_firewall(env, LAN)
        self.assertEqual([r.status for r in res], ["ok", "ok", "ok"])

    def test_parsers(self):
        self.assertEqual(doctor.parse_active_zones("home\n  interfaces: a b\ndocker\n  interfaces: docker0\n"),
                         {"a": "home", "b": "home", "docker0": "docker"})
        services, ports = doctor.parse_zone_listing(HOME_LISTING)
        self.assertEqual(services, {"dhcpv6-client", "samba", "ssh"})
        self.assertEqual(ports, [(3702, 3702, "udp"), (5357, 5357, "tcp")])

    def test_ufw_needs_root_and_detects_missing_rules(self):
        tools = {"ufw"}
        table = {("ufw", "status"): (0, "Status: active\n445/tcp ALLOW 192.168.1.0/24")}
        res = doctor.check_firewall(make_env(self.root, table, euid=1000, tools=tools), LAN)
        self.assertEqual(res[0].status, "skip")
        res = doctor.check_firewall(make_env(self.root, table, euid=0, tools=tools), LAN)
        self.assertEqual(res[0].status, "fail")

    def test_no_firewall_found_warns(self):
        res = doctor.check_firewall(make_env(self.root, {}, tools=set()), LAN)
        self.assertEqual(res[0].status, "warn")

    def test_selinux_label_and_samba_user(self):
        table = good_table({("stat",): (0, "unconfined_u:object_r:user_home_t:s0"), ("pdbedit",): (0, "amy:1001:Amy")})
        res = doctor.check_shares(make_env(self.root, table), self.cfg)
        self.assertEqual(self.statuses(res, "SELinux label"), ["fail"])
        user = by_name(res, "no Samba password")[0]
        self.assertEqual(user.status, "fail")
        self.assertIn("lan-share user add bob", user.fix)

    def test_samba_user_check_skipped_without_root(self):
        res = doctor.check_shares(make_env(self.root, good_table(), euid=1000), self.cfg)
        self.assertEqual(self.statuses(res, "Samba password"), ["skip"])

    def test_missing_share_directory(self):
        self.cfg.add_share(config.Share("gone", "/srv/gone", ["bob"]))
        res = doctor.check_shares(make_env(self.root, good_table()), self.cfg)
        self.assertEqual(self.statuses(res, "not a directory"), ["fail"])

    def test_mounts(self):
        self.cfg.add_mount(config.Mount("pc", "192.168.1.50", "Docs", "alice", "alice"))
        env = make_env(self.root, good_table(), reachable=False)
        res = doctor.check_mounts(env, self.cfg)
        self.assertEqual(self.statuses(res, "unreachable"), ["warn"])
        self.assertEqual(self.statuses(res, "credentials file"), ["fail"])  # root, file absent
        res = doctor.check_mounts(make_env(self.root, good_table({("systemctl", "is-active"): (3, "")})), self.cfg)
        self.assertEqual(self.statuses(res, "automount is not active"), ["fail"])


if __name__ == "__main__":
    unittest.main()
