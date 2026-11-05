import tempfile
import unittest
from ipaddress import ip_address, ip_network
from pathlib import Path

from lan_share import config, firewall, mounts, samba, state, teardown, wsdd_service as W
from lan_share.distro import from_os_release
from lan_share.network import Interface
from lan_share.runner import Context, Plan, execute

LAN = [Interface("wlp3s0", ip_address("192.168.1.20"), ip_network("192.168.1.0/24"))]
FEDORA = from_os_release({"ID": "fedora"})


def cmds(plan):
    return [" ".join(s.cmd) for s in plan.steps if s.cmd]


class FirewallOwnershipTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.ctx = Context(root=Path(self._t.name), apply=True)
        self.addCleanup(self._t.cleanup)

    def run_plan(self, present):
        plan = Plan()
        firewall.plan_firewalld(self.ctx, LAN, "home", False, plan, query=lambda c: any(p in " ".join(c) for p in present))
        return plan

    def test_adds_and_records_only_what_is_missing(self):
        plan = self.run_plan(present=["--query-port=3702/udp"])  # port already open, service and 5357 are not
        c = cmds(plan)
        self.assertIn("firewall-cmd --permanent --zone=home --add-service=samba", c)
        self.assertIn("firewall-cmd --permanent --zone=home --add-port=5357/tcp", c)
        self.assertNotIn("firewall-cmd --permanent --zone=home --add-port=3702/udp", c)
        execute(plan, self.ctx, log=lambda _: None)  # staged: commands skipped, record actions run
        st = state.load(self.ctx)
        self.assertEqual(st.firewalld["home"], {"services": ["samba"], "ports": ["5357/tcp"]})

    def test_preexisting_rules_are_never_claimed(self):
        plan = self.run_plan(present=["--query-service=samba", "--query-port"])
        self.assertEqual(cmds(plan), [])
        execute(plan, self.ctx, log=lambda _: None)
        self.assertIsNone(state.load(self.ctx))

    def test_rerun_keeps_ownership_of_what_we_added(self):
        state.record_firewalld(self.ctx, "home", service="samba")
        plan = self.run_plan(present=["--query-service=samba", "--query-port"])  # now present because we added it
        execute(plan, self.ctx, log=lambda _: None)
        self.assertEqual(state.load(self.ctx).firewalld["home"]["services"], ["samba"])

    def test_untrusted_zone_refused(self):
        from lan_share import LanShareError
        with self.assertRaises(LanShareError):
            firewall.plan_firewalld(self.ctx, LAN, "public", False, Plan(), query=lambda c: False)


class TeardownTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name)
        self.ctx = Context(root=self.root, apply=True)
        self.addCleanup(self._t.cleanup)
        # what setup would have left behind
        self.smb = self.ctx.path(samba.SMB_CONF)
        self.smb.parent.mkdir(parents=True)
        self.smb.write_text("[global]\n   workgroup = HOME\n[printers]\n   path = /var/tmp\n")
        samba.ensure_include(self.ctx)
        self.ctx.path(samba.CONF_PATH).write_text("[global]\n")
        W_unit = self.ctx.path(W.UNIT_PATH); W_unit.parent.mkdir(parents=True); W_unit.write_text("unit")
        inst = self.ctx.path(W.INSTALL_PATH); inst.parent.mkdir(parents=True); inst.write_text("wsdd")
        self.cfg = config.Config()
        self.cfg.add_share(config.Share("docs", "/srv/docs", ["bob"]))
        self.cfg.add_mount(config.Mount("pc", "192.168.1.50", "Docs", "alice", "alice"))
        config.save(self.ctx, self.cfg)
        mounts.plan_add  # noqa
        for rel in (mounts.CRED_DIR,):
            self.ctx.path(rel).mkdir(parents=True)
        self.ctx.path(self.cfg.mounts[0].credentials_path).write_text("username=a\npassword=b\n")

    def seed_state(self):
        state.record_firewalld(self.ctx, "home", service="samba")
        state.record_firewalld(self.ctx, "home", port="5357/tcp")
        state.record_ufw(self.ctx, ["allow", "proto", "tcp", "from", "192.168.1.0/24", "to", "any", "port", "445"])
        state.record_selinux(self.ctx, "/srv/docs")
        state.record_unit(self.ctx, "smb")

    def test_removes_our_files_and_keeps_the_users_smb_conf(self):
        self.seed_state()
        plan = teardown.plan_teardown(self.ctx, FEDORA, self.cfg)
        execute(plan, self.ctx, log=lambda _: None)
        text = self.smb.read_text()
        self.assertNotIn("lan-share", text)
        self.assertIn("workgroup = HOME", text)
        self.assertIn("path = /var/tmp", text)
        for gone in (samba.CONF_PATH, W.UNIT_PATH, W.INSTALL_PATH, self.cfg.mounts[0].credentials_path):
            self.assertFalse(self.ctx.path(gone).exists(), gone)
        self.assertFalse(self.ctx.path(W.INSTALL_PATH).parent.exists(), "empty install dir removed")
        self.assertIsNone(state.load(self.ctx), "state forgotten")
        self.assertTrue(self.ctx.path(config.CONFIG_PATH).exists(), "config kept without --purge")

    def test_only_recorded_changes_are_undone(self):
        self.seed_state()
        c = cmds(teardown.plan_teardown(self.ctx, FEDORA, self.cfg))
        self.assertIn("firewall-cmd --permanent --zone=home --remove-service=samba", c)
        self.assertIn("firewall-cmd --permanent --zone=home --remove-port=5357/tcp", c)
        self.assertNotIn("firewall-cmd --permanent --zone=home --remove-port=3702/udp", c)  # never recorded
        self.assertIn("ufw delete allow proto tcp from 192.168.1.0/24 to any port 445", c)
        self.assertIn("systemctl disable --now smb", c)
        self.assertNotIn("avahi-daemon", " ".join(c))  # was enabled before lan-share
        self.assertTrue(any(x.startswith("semanage fcontext -d") for x in c))

    def test_without_state_nothing_system_wide_is_touched_and_user_is_told(self):
        plan = teardown.plan_teardown(self.ctx, FEDORA, self.cfg)
        c = " ".join(cmds(plan))
        for forbidden in ("firewall-cmd", "ufw", "semanage", "restorecon"):
            self.assertNotIn(forbidden, c)
        self.assertNotIn("disable --now smb ", c)
        self.assertTrue(any("no record" in w for w in plan.warnings))

    def test_keep_mounts_and_purge(self):
        plan = teardown.plan_teardown(self.ctx, FEDORA, self.cfg, keep_mounts=True)
        self.assertFalse(any("automount" in s.title for s in plan.steps))
        execute(teardown.plan_teardown(self.ctx, FEDORA, self.cfg, keep_mounts=True, purge=True), self.ctx, log=lambda _: None)
        self.assertFalse(self.ctx.path("/etc/lan-share").exists())

    def test_strip_include_is_idempotent_and_handles_a_file_we_created(self):
        teardown._strip_include(self.smb)
        teardown._strip_include(self.smb)
        self.assertEqual(self.smb.read_text().count("include"), 0)
        fresh = self.root / "fresh.conf"
        fresh.write_text(f"[global]\n   {samba.INCLUDE_LINE}\n")
        teardown._strip_include(fresh)
        self.assertEqual(fresh.read_text(), "[global]\n")

    def test_setup_then_teardown_roundtrip_of_smb_conf(self):
        original = "[global]\n   workgroup = HOME\n"
        self.smb.write_text(original)
        samba.ensure_include(self.ctx)
        self.assertNotEqual(self.smb.read_text(), original)
        teardown._strip_include(self.smb)
        self.assertEqual(self.smb.read_text(), original)


if __name__ == "__main__":
    unittest.main()
