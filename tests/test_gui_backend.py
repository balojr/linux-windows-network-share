import json
import subprocess
import unittest

from lan_share.gui.backend import Backend, BackendError, Outcome, slug


class Recorder:
    """Fake subprocess.run: records calls, replays canned results."""

    def __init__(self, rc=0, out="", err=""):
        self.calls, self.rc, self.out, self.err = [], rc, out, err

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, self.rc, self.out, self.err)


class BackendTests(unittest.TestCase):
    def test_privileged_commands_go_through_pkexec_and_unprivileged_do_not(self):
        r = Recorder()
        b = Backend(run=r, elevate=["/usr/bin/pkexec", "/usr/bin/env", "PYTHONPATH=/x"])
        b.call(["doctor", "--json"])
        b.call(["setup", "--apply"], root=True)
        plain, priv = r.calls[0][0], r.calls[1][0]
        self.assertNotIn("pkexec", " ".join(plain))
        self.assertEqual(priv[:3], ["/usr/bin/pkexec", "/usr/bin/env", "PYTHONPATH=/x"])
        self.assertEqual(priv[-3:], ["lan_share", "setup", "--apply"][-3:])

    def test_passwords_go_to_stdin_never_argv(self):
        r = Recorder(out="[]")
        b = Backend(run=r, elevate=["pkexec"])
        b.remote_shares("10.0.0.5", "ADMIN", "hunter2")
        b.add_mount("pc", "10.0.0.5", "Docs", "ADMIN", "hunter2")
        b.set_samba_password("bob", "hunter2")
        self.assertEqual(len(r.calls), 3)
        for argv, kw in r.calls:
            self.assertNotIn("hunter2", " ".join(argv))
            self.assertEqual(kw["input"], "hunter2\n")
            self.assertIn("--password-stdin", argv)

    def test_add_share_builds_the_cli_call(self):
        r = Recorder()
        Backend(run=r, elevate=["pkexec"]).add_share("Docs", "/srv/docs", ["bob", "amy"], True)
        argv = r.calls[0][0]
        tail = argv[argv.index("share"):]
        self.assertEqual(tail, ["share", "add", "Docs", "/srv/docs", "--apply", "--user", "bob", "--user", "amy", "--rw"])

    def test_doctor_exit_1_is_not_an_error_but_other_codes_are(self):
        data = [{"status": "fail", "name": "x"}]
        self.assertEqual(Backend(run=Recorder(rc=1, out=json.dumps(data))).doctor(), data)
        with self.assertRaises(BackendError):
            Backend(run=Recorder(rc=2, err="error: boom")).doctor()

    def test_errors_become_readable_messages(self):
        self.assertEqual(Outcome(1, "", "noise\nerror: could not list shares: NT_STATUS_LOGON_FAILURE\n").message,
                         "could not list shares: NT_STATUS_LOGON_FAILURE")
        self.assertIn("cancelled", Outcome(126, "", "").message)   # pkexec: dialog dismissed
        self.assertIn("cancelled", Outcome(127, "", "").message)
        with self.assertRaises(BackendError) as cm:
            Backend(run=Recorder(rc=1, err="error: needs root")).remove_share("x")
        self.assertEqual(str(cm.exception), "needs root")

    def test_garbage_output_is_reported_not_crashed(self):
        with self.assertRaises(BackendError):
            Backend(run=Recorder(out="not json")).shares()

    def test_timeout_and_missing_binary(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(a[0], 1)

        self.assertEqual(Backend(run=boom).call(["x"]).rc, 124)
        self.assertEqual(Backend(run=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("pkexec"))).call(["x"], root=True).rc, 127)

    def test_slug(self):
        self.assertEqual(slug("DESKTOP-OFFICE Games!"), "desktop-office-games")
        self.assertEqual(slug("***"), "windows")
        self.assertLessEqual(len(slug("a" * 100)), 32)


if __name__ == "__main__":
    unittest.main()
