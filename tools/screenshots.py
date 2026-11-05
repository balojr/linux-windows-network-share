"""Render the README screenshots from the real GTK window, using generic demo data (no real hostnames or IPs).

    GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 PYTHONPATH=src python3 tools/screenshots.py docs/screenshots
(with `gtk4-broadwayd :7` running, so no real screen is needed; it works under Wayland/X11 too.)
"""
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Graphene  # noqa: E402

from lan_share.gui import app as A  # noqa: E402
from lan_share.gui.backend import BackendError  # noqa: E402


class DemoBackend:
    """Canned, generic data."""

    def doctor(self, root=False):
        ok = lambda n: {"status": "ok", "name": n, "detail": "", "fix": ""}  # noqa: E731
        return ([
            {"status": "warn", "name": "SMB signing is not required", "detail": "", "fix": "sudo lan-share setup --apply"},
            ok("Distro: Fedora Linux 44 (Workstation Edition) (fedora)"), ok("LAN: wlp3s0 192.168.1.20"),
            ok("Samba server installed"), ok("Avahi (mDNS names) installed"), ok("cifs-utils (mount Windows shares) installed"),
            ok("Bundled wsdd is installed and current"), ok("Samba service running"), ok("wsdd service running"),
            ok("Samba configuration is valid"), ok("Samba listens only on the LAN interface"),
            ok("SMB (445/tcp) is listening on the LAN address"), ok("WS-Discovery (3702/udp) is listening"),
            ok("firewalld zone 'home' allows file sharing"), ok("Share 'photos' -> /home/alice/Photos"),
            {"status": "skip", "name": "  Samba password for alice", "detail": "needs root", "fix": ""},
        ])

    def shares(self):
        return [{"name": "photos", "path": "/home/alice/Photos", "users": ["alice"], "read_only": False},
                {"name": "documents", "path": "/home/alice/Documents", "users": ["alice", "bob"], "read_only": True}]

    def mounts(self):
        return [{"name": "office-projects", "host": "192.168.1.50", "share": "Projects", "user": "alice", "local_user": "alice",
                 "domain": "", "mountpoint": "/mnt/lan-share/office-projects"}]

    def scan(self, timeout=6.0):
        return [{"address": "192.168.1.50", "name": "DESKTOP-OFFICE", "group": "WORKGROUP"},
                {"address": "192.168.1.62", "name": "LAPTOP-BOB", "group": "WORKGROUP"}]

    def remote_shares(self, host, user, password, domain=""):
        return [{"name": "Projects", "comment": ""}, {"name": "Downloads", "comment": ""}]


def pump(ms):
    loop = GLib.MainLoop()
    GLib.timeout_add(ms, loop.quit)
    loop.run()


def save(win, path: Path) -> None:
    """Render the window (header bar + pages) to a PNG.

    Dialogs are not captured: libadwaita draws them in a separate layer that does not render reliably from a headless
    surface (they are exercised by tests/test_gui.py instead)."""
    from gi.repository import Gdk

    host = win.get_first_child()
    w, h = host.get_width(), host.get_height()
    snap = Gtk.Snapshot()
    # opaque backdrop in the theme's window colour, then the widget tree on top
    bg = Gdk.RGBA()
    bg.parse("#242424" if Adw.StyleManager.get_default().get_dark() else "#fafafa")
    snap.append_color(bg, Graphene.Rect().init(0, 0, w, h))
    Gtk.WidgetPaintable.new(host).snapshot(snap, w, h)
    node = snap.to_node()
    texture = win.get_native().get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))
    texture.save_to_png(str(path))
    print("wrote", path, f"{w}x{h}")


def select(win, page):
    win.stack.set_visible_child(page)
    pump(900)  # also lets the view switcher's highlighted tab catch up


def main(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    app = Adw.Application(application_id="io.github.lan_share.shots", flags=Gio.ApplicationFlags.NON_UNIQUE)

    def activate(a):
        try:
            run(a)
        except Exception:
            import traceback
            traceback.print_exc()
            sys.exit(1)
        finally:
            a.quit()

    def run(a):
        # a headless surface has no real frame clock, so animations (dialog open, page transitions) would never finish
        Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
        win = A.MainWindow(a, DemoBackend())
        win.set_default_size(760, 600)
        win.present()
        pump(2000)  # let the first frame and the async data load finish
        select(win, win.windows)
        select(win, win.sharing)
        pump(800)
        save(win, out / "share.png")

        select(win, win.windows)
        win.windows.scan()
        pump(1200)
        save(win, out / "windows-pcs.png")

        select(win, win.status)
        save(win, out / "health.png")

    app.connect("activate", activate)
    app.run(None)


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "docs/screenshots"))
