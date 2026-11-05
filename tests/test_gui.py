"""Drives the real GTK/libadwaita window headlessly (Broadway) against a fake backend.
Skipped where GTK4, libadwaita or gtk4-broadwayd are missing."""
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def gtk_ok() -> bool:
    if not shutil.which("gtk4-broadwayd"):
        return False
    res = subprocess.run([sys.executable, "-c", "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Gtk, Adw"],
                         capture_output=True)
    return res.returncode == 0


@unittest.skipUnless(gtk_ok(), "GTK4 / libadwaita / gtk4-broadwayd not available")
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        display = f":{20 + os.getpid() % 40}"
        cls.broadway = subprocess.Popen(["gtk4-broadwayd", display], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        cls.broadway.stdout.readline()  # "Listening on ..."
        time.sleep(0.5)
        env = {**os.environ, "GDK_BACKEND": "broadway", "BROADWAY_DISPLAY": display, "PYTHONPATH": str(ROOT / "src")}
        res = subprocess.run([sys.executable, str(ROOT / "tests" / "gui_driver.py")], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        lines = [ln for ln in res.stdout.splitlines() if ln.startswith("REPORT:")]
        assert lines, f"driver produced no report:\n{res.stdout}\n{res.stderr}"
        cls.r = json.loads(lines[0][7:])
        cls.stderr = res.stderr

    @classmethod
    def tearDownClass(cls):
        cls.broadway.terminate()

    def test_no_gtk_criticals_or_tracebacks(self):
        bad = [ln for ln in self.stderr.splitlines() if "CRITICAL" in ln or "Traceback" in ln or "Error" in ln]
        self.assertEqual(bad, [])

    def test_pages_and_lists(self):
        r = self.r
        self.assertEqual(r["pages"], ["Share", "Windows PCs", "Health"])
        self.assertEqual(r["share_rows"][0][0], "Docs")
        self.assertIn("read-only", r["share_rows"][0][1])
        self.assertEqual(r["mount_rows"][0][0], "pc-games")
        self.assertEqual(r["found_rows"][0][0], "DESKTOP-X")

    def test_health_sorted_problems_first_and_unchecked_last_with_friendly_text(self):
        titles = [t for t, _ in self.r["status_rows"]]
        self.assertTrue(titles[0].startswith("wsdd"))
        self.assertEqual(titles[-1], "Samba password for bob")
        self.assertIn("Not checked", self.r["status_rows"][-1][1])
        self.assertIn("1 problem, 1 warning", self.r["status_summary"])
        self.assertIn("1 check needs administrator rights", self.r["status_summary"])

    def test_markup_in_data_is_escaped_not_interpreted(self):
        self.assertIn("&lt;not&gt;", self.r["status_rows"][0][0])

    def test_actions_call_the_backend_with_the_right_arguments(self):
        calls = [tuple(c) for c in self.r["calls"] if c[0] != "doctor"]
        self.assertEqual(calls, [
            ("add_share", "Photos", "/srv/photos", ["bob", "amy"], True),
            ("remove_share", "Docs"),
            ("set_samba_password", "bob", "s3cret"),
            ("remote_shares", "192.168.1.50", "ADMIN", "wrong"),
            ("add_mount", "pc-games2", "192.168.1.50", "Games", "ADMIN", "pw"),
            ("remove_mount", "pc-games"),
        ])
        self.assertIn(["doctor", True], self.r["calls"])  # "Full check" asks for the privileged run

    def test_invalid_input_never_reaches_the_backend_and_errors_are_shown(self):
        errors = [m for m, is_err in self.r["toasts"] if is_err]
        self.assertIn("The two passwords don't match.", errors)
        self.assertTrue(any("Choose a folder" in m for m in errors))
        self.assertIn("NT_STATUS_LOGON_FAILURE", errors)
        self.assertFalse(any(c[0] == "add_share" and c[1] == "" for c in self.r["calls"]))
        self.assertEqual(sum(1 for c in self.r["calls"] if c[0] == "set_samba_password"), 1)

    def test_scan_failure_is_reported_and_button_recovers(self):
        f = self.r["scan_fail"]
        self.assertEqual(f["toasts"], [["no private LAN interface found", True]])
        self.assertEqual(f["rows"], 0)
        self.assertTrue(f["button_enabled"])

    def test_all_dialogs_construct(self):
        self.assertEqual(self.r["dialogs"], "ok")


if __name__ == "__main__":
    unittest.main()
