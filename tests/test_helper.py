import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lan_share import LanShareError, helper
from lan_share.gui.backend import Backend

ROOT = Path(__file__).resolve().parent.parent
DTD = Path("/usr/share/polkit-1/policyconfig-1.dtd")

# exactly what gui/backend.py sends
ALLOWED = [
    ["doctor", "--json"],
    ["setup", "--apply"],
    ["share", "add", "Docs", "/srv/docs", "--apply", "--user", "bob", "--user", "amy", "--rw"],
    ["share", "add", "Docs", "/srv/docs", "--apply", "--user", "bob"],
    ["share", "remove", "Docs", "--apply"],
    ["user", "add", "bob", "--password-stdin"],
    ["mount", "add", "pc", "//10.0.0.5/Docs", "--user", "ADMIN", "--local-user", "bob", "--password-stdin"],
    ["mount", "add", "pc", "//10.0.0.5/Docs", "--user", "ADMIN", "--local-user", "bob", "--password-stdin", "--domain", "CORP"],
    ["mount", "remove", "pc"],
]
REJECTED = [
    [],
    ["--root", "/tmp/x", "setup", "--apply"],                    # would redirect where root writes
    ["setup", "--apply", "--root", "/tmp/x"],
    ["setup"],                                                    # dry run is pointless as root, and not an allowed shape
    ["setup", "--apply", "--zone", "public", "--allow-untrusted-zone"],
    ["teardown", "--apply"],                                      # not offered by the GUI
    ["gui"],
    ["doctor"],
    ["share", "add", "Docs", "/srv/docs", "--user", "bob"],       # missing --apply
    ["share", "add", "Docs", "/srv/docs", "--apply"],             # missing --user
    ["share", "add", "--rw", "/srv/docs", "--apply", "--user", "b"],  # flag where a positional belongs
    ["share", "add", "Docs", "/srv/docs", "--apply", "--user", "--rw"],  # option value that looks like a flag
    ["share", "add", "Docs", "/srv/docs", "--apply", "--user", "bob", "--evil"],
    ["share", "remove", "-x", "--apply"],
    ["user", "add", "bob"],                                       # interactive password prompt: never as root via GUI
    ["mount", "add", "pc", "//h/s", "--user", "a", "--local-user", "b"],  # missing --password-stdin
    ["mount", "add", "pc", "//h/s", "--user", "a", "--user", "b", "--local-user", "c", "--password-stdin"],  # duplicate
    ["mount", "remove"],
    ["mount", "remove", "pc", "extra"],
]


class WhitelistTests(unittest.TestCase):
    def test_allowed_shapes_pass(self):
        for argv in ALLOWED:
            self.assertEqual(helper.validate(list(argv)), argv)

    def test_everything_else_is_refused(self):
        for argv in REJECTED:
            with self.assertRaises(LanShareError, msg=argv):
                helper.validate(list(argv))

    def test_the_backend_only_sends_what_the_helper_allows(self):
        """Run every privileged Backend method against a recorder and validate what it would send."""
        sent = []

        def run(argv, **kw):
            sent.append(argv)
            return subprocess.CompletedProcess(argv, 0, "[]", "")

        b = Backend(run=run, helper="/usr/lib/lan-share/lan-share-helper")
        b.add_share("Docs", "/srv/docs", ["bob", "amy"], True)
        b.add_share("Docs", "/srv/docs", ["bob"], False)
        b.remove_share("Docs")
        b.apply()
        b.set_samba_password("bob", "pw")
        b.add_mount("pc", "10.0.0.5", "Docs", "ADMIN", "pw")
        b.add_mount("pc", "10.0.0.5", "Docs", "ADMIN", "pw", "CORP")
        b.remove_mount("pc")
        b.doctor(root=True)
        self.assertEqual(len(sent), 9)
        for argv in sent:
            self.assertTrue(argv[0].endswith("pkexec"))
            self.assertEqual(argv[1], "/usr/lib/lan-share/lan-share-helper")
            helper.validate(argv[2:])

    def test_main_validates_then_hands_over_to_the_cli(self):
        with mock.patch("lan_share.cli.main", return_value=0) as cli_main:
            self.assertEqual(helper.main(["setup", "--apply"]), 0)
            cli_main.assert_called_once_with(["setup", "--apply"])
            self.assertEqual(helper.main(["--root", "/x", "setup", "--apply"]), 2)
            cli_main.assert_called_once()  # the rejected call never reached the CLI

    def test_environment_cannot_redirect_a_root_run(self):
        with mock.patch.dict(os.environ, {"LAN_SHARE_ROOT": "/tmp/evil"}), mock.patch("lan_share.cli.main", return_value=0):
            helper.main(["setup", "--apply"])
            self.assertNotIn("LAN_SHARE_ROOT", os.environ)


class InstalledHelperTests(unittest.TestCase):
    """Install into a throwaway prefix and exercise the real helper script and policy file."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        t = Path(cls._tmp.name)
        cls.prefix, cls.pk = t / "usr", t / "polkit"
        env = {**os.environ, "PREFIX": str(cls.prefix), "POLKITDIR": str(cls.pk)}
        res = subprocess.run([str(ROOT / "packaging/install.sh"), "install"], env=env, capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        cls.helper = cls.prefix / "lib/lan-share/lan-share-helper"
        cls.policy = cls.pk / "io.github.lan_share.policy"

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_files_and_permissions(self):
        self.assertTrue(os.access(self.helper, os.X_OK))
        self.assertEqual(oct(self.policy.stat().st_mode & 0o777), "0o644")
        self.assertEqual(oct(self.helper.stat().st_mode & 0o777), "0o755")

    def test_policy_points_at_the_installed_helper_and_is_not_permissive(self):
        text = self.policy.read_text()
        self.assertIn(f'<annotate key="org.freedesktop.policykit.exec.path">{self.helper}</annotate>', text)
        self.assertNotIn("@", text, "no unsubstituted placeholders")
        for line in re.findall(r"<allow_\w+>(\w+)</allow_\w+>", text):
            self.assertIn(line, ("auth_admin", "auth_admin_keep"), "never allow without authentication")
        self.assertIn("<allow_any>auth_admin</allow_any>", text)

    @unittest.skipUnless(shutil.which("xmllint") and DTD.exists(), "xmllint or the polkit DTD is not available")
    def test_policy_is_valid_against_polkits_dtd_and_the_check_can_fail(self):
        def validate(text):
            f = Path(self._tmp.name) / "p.xml"
            f.write_text("\n".join(ln for ln in text.splitlines() if "<!DOCTYPE" not in ln))
            return subprocess.run(["xmllint", "--noout", "--dtdvalid", str(DTD), str(f)], capture_output=True).returncode

        text = self.policy.read_text()
        self.assertEqual(validate(text), 0)
        self.assertNotEqual(validate(text.replace("<message>", "<bogus>").replace("</message>", "</bogus>")), 0)

    def test_helper_runs_and_refuses_what_it_should(self):
        ok = subprocess.run([str(self.helper), "doctor", "--json"], capture_output=True, text=True)
        self.assertIn(ok.returncode, (0, 1))
        self.assertIsInstance(json.loads(ok.stdout), list)
        for bad in (["--root", "/tmp/x", "doctor", "--json"], ["setup"], ["share", "add", "x", "/etc", "--user", "u", "--evil"]):
            res = subprocess.run([str(self.helper), *bad], capture_output=True, text=True)
            self.assertEqual(res.returncode, 2, bad)
            self.assertTrue(res.stderr.startswith("error:"), (bad, res.stderr))
            self.assertEqual(res.stdout, "", "a refused command must not have run anything")

    def test_helper_ignores_python_environment_tricks(self):
        """pkexec sanitises the environment, but the helper must not trust it anyway.

        A module planted in PYTHONPATH or the current directory that shadows a standard-library module the
        CLI imports (here `json`) would run as root if the helper trusted either."""
        evil = Path(self._tmp.name) / "evil"
        evil.mkdir()
        (evil / "json.py").write_text("raise SystemExit(99)\n")
        env = {**os.environ, "PYTHONPATH": str(evil)}
        res = subprocess.run([str(self.helper), "doctor", "--json"], env=env, capture_output=True, text=True, cwd=evil)
        self.assertIn(res.returncode, (0, 1), res.stderr)  # 99 would mean the planted json.py was imported
        self.assertIsInstance(json.loads(res.stdout), list)

    def test_backend_prefers_the_helper_when_installed_and_falls_back_otherwise(self):
        self.assertEqual(Backend(helper=str(self.helper)).argv(["setup", "--apply"], root=True)[1:], [str(self.helper), "setup", "--apply"])
        self.assertEqual(Backend(helper="").argv(["x"], root=False)[1:4], ["-B", "-m", "lan_share"])


if __name__ == "__main__":
    unittest.main()
