"""Runs the real GTK window against a fake backend and prints a JSON report. Launched by test_gui.py
inside a Broadway display (no real screen needed)."""
import json
import sys
import threading

sys.path.insert(0, "src")
from gi.repository import GLib  # noqa: E402

from lan_share.gui import app as A  # noqa: E402
from lan_share.gui.backend import BackendError  # noqa: E402


class FakeBackend:
    def __init__(self, fail_scan=False):
        self.calls, self.fail_scan = [], fail_scan

    def doctor(self, root=False):
        self.calls.append(("doctor", root))
        return [{"status": "ok", "name": "Samba server installed", "detail": "", "fix": ""},
                {"status": "skip", "name": "Samba password for bob", "detail": "needs root; re-run with sudo", "fix": ""},
                {"status": "fail", "name": "wsdd <not> running & dead", "detail": "port 3702 closed", "fix": "sudo systemctl restart lan-share-wsdd"},
                {"status": "warn", "name": "SMB signing is not required", "detail": "", "fix": "sudo lan-share setup --apply"}]

    def shares(self):
        return [{"name": "Docs", "path": "/srv/docs", "users": ["bob"], "read_only": True}]

    def mounts(self):
        return [{"name": "pc-games", "host": "192.168.1.50", "share": "Games", "user": "ADMIN", "local_user": "bob",
                 "domain": "", "mountpoint": "/mnt/lan-share/pc-games"}]

    def scan(self, timeout=6.0):
        if self.fail_scan:
            raise BackendError("no private LAN interface found")
        return [{"address": "192.168.1.50", "name": "DESKTOP-X", "group": "WORKGROUP"}]

    def remote_shares(self, host, user, password, domain=""):
        self.calls.append(("remote_shares", host, user, password))
        if password == "wrong":
            raise BackendError("NT_STATUS_LOGON_FAILURE")
        return [{"name": "Games", "comment": ""}, {"name": "Docs", "comment": "d"}]

    def add_share(self, *a): self.calls.append(("add_share", *a))
    def remove_share(self, n): self.calls.append(("remove_share", n))
    def set_samba_password(self, u, p): self.calls.append(("set_samba_password", u, p))
    def add_mount(self, *a): self.calls.append(("add_mount", *a))
    def remove_mount(self, n): self.calls.append(("remove_mount", n))


def pump(ms=400):
    loop = GLib.MainLoop()
    GLib.timeout_add(ms, loop.quit)
    loop.run()


def titles(group_rows):
    return [r.get_title() for r in group_rows]


def toasts(win):
    return [t for t in getattr(win, "_seen_toasts", [])]


def main():
    be = FakeBackend()
    app = A.Adw.Application(application_id="io.github.lan_share.test", flags=A.Gio.ApplicationFlags.NON_UNIQUE)
    report = {}

    def on_activate(a):
        win = A.MainWindow(a, be)
        win.present()
        seen = []
        orig = win.toast
        win.toast = lambda m, error=False: (seen.append((m, error)), orig(m, error))[1]
        pump(600)
        report["share_rows"] = [(r.get_title(), r.get_subtitle()) for r in win.sharing.rows]
        report["mount_rows"] = [(r.get_title(), r.get_subtitle()) for r in win.windows.mount_rows]
        report["status_rows"] = [(r.get_title(), r.get_subtitle()) for r in win.status.rows]
        report["status_summary"] = win.status.summary.get_description()
        report["pages"] = [win.stack.get_page(p).get_title() for p in (win.sharing, win.windows, win.status)]

        win.windows.scan(); pump(800)
        report["found_rows"] = [(r.get_title(), r.get_subtitle()) for r in win.windows.found_rows]

        # actions that would normally come from dialog responses
        win.sharing.do_add("  Photos ", "/srv/photos", "bob amy", True); pump(300)
        win.sharing.do_add("", "", "", False); pump(100)                           # invalid -> toast, no call
        win.sharing.do_remove("Docs"); pump(300)
        win.sharing.do_set_password("bob", "a", "b"); pump(100)                    # mismatch -> toast, no call
        win.sharing.do_set_password("bob", "s3cret", "s3cret"); pump(300)
        host = {"name": "DESKTOP-X", "address": "192.168.1.50", "group": "WORKGROUP"}
        win.windows.do_login(host, "ADMIN", "wrong"); pump(300)                    # failure -> toast
        win.windows.do_mount(host, "ADMIN", "pw", "Games", "pc-games2"); pump(300)
        win.windows.do_unmount("pc-games"); pump(300)
        win.status.refresh(root=True); pump(500)

        # dialogs must at least construct and present without raising
        win.sharing.add_dialog(); pump(200)
        win.sharing.password_dialog("bob"); pump(200)
        win.sharing.confirm_remove("Docs"); pump(200)
        win.windows.login_dialog(host); pump(200)
        win.windows.confirm_remove("pc-games"); pump(200)
        report["dialogs"] = "ok"

        report["calls"] = [list(c) for c in be.calls]
        report["toasts"] = seen
        a.quit()

    app.connect("activate", on_activate)
    app.run(None)

    # scan failure path, with a fresh app
    be2 = FakeBackend(fail_scan=True)
    app2 = A.Adw.Application(application_id="io.github.lan_share.test2", flags=A.Gio.ApplicationFlags.NON_UNIQUE)

    def on_activate2(a):
        win = A.MainWindow(a, be2)
        win.present()
        seen = []
        orig = win.toast
        win.toast = lambda m, error=False: (seen.append((m, error)), orig(m, error))[1]
        pump(300)
        win.windows.scan(); pump(800)
        report["scan_fail"] = {"toasts": seen, "rows": len(win.windows.found_rows), "button_enabled": win.windows.scan_btn.get_sensitive()}
        a.quit()

    app2.connect("activate", on_activate2)
    app2.run(None)
    print("REPORT:" + json.dumps(report))


main()
