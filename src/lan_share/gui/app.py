"""GTK 4 / libadwaita front-end. All real work happens in backend.py (the lan-share CLI)."""

from __future__ import annotations

import getpass
import threading
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .backend import Backend, BackendError, slug  # noqa: E402

if not hasattr(Adw, "AlertDialog"):  # added in libadwaita 1.5 (Ubuntu 22.04, Debian 12, Rocky 9 ship older)
    raise ImportError(f"libadwaita 1.5 or newer is required (found {Adw.get_major_version()}.{Adw.get_minor_version()})")

APP_ID = "io.github.lan_share"
_ICONS = {"ok": ("object-select-symbolic", "success"), "warn": ("dialog-warning-symbolic", "warning"),
          "fail": ("dialog-error-symbolic", "error"), "skip": ("action-unavailable-symbolic", "dim-label")}


def esc(text: str) -> str:
    return GLib.markup_escape_text(text or "")


def in_background(work: Callable[[], object], done: Callable[[object, Optional[Exception]], None]) -> None:
    """Run `work` on a thread; call `done(result, error)` back on the GTK main loop."""
    def runner() -> None:
        try:
            result, error = work(), None
        except Exception as e:  # shown to the user, never swallowed
            result, error = None, e
        GLib.idle_add(lambda: (done(result, error), False)[1])
    threading.Thread(target=runner, daemon=True).start()


def clear(group: Adw.PreferencesGroup, rows: list) -> None:
    for r in rows:
        group.remove(r)
    rows.clear()


class Page(Adw.PreferencesPage):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win, self.backend = win, win.backend

    def fail(self, error: Exception) -> None:
        self.win.toast(str(error) or error.__class__.__name__, error=True)


# ----------------------------------------------------------------------------- Status
class StatusPage(Page):
    def __init__(self, win):
        super().__init__(win)
        self.summary = Adw.PreferencesGroup(title="Health check")
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Check again")
        self.refresh_btn.add_css_class("flat")
        self.refresh_btn.connect("clicked", lambda *_: self.refresh())
        self.full_btn = Gtk.Button(label="Full check", valign=Gtk.Align.CENTER, tooltip_text="Also run the checks that need administrator rights")
        self.full_btn.add_css_class("flat")
        self.full_btn.connect("clicked", lambda *_: self.refresh(root=True))
        suffix = Gtk.Box(spacing=6)
        suffix.append(self.full_btn)
        suffix.append(self.refresh_btn)
        self.summary.set_header_suffix(suffix)
        self.add(self.summary)
        self.rows: list = []

        win_group = Adw.PreferencesGroup(
            title="On the Windows PC", description="These can't be checked from here. If this computer doesn't appear in Explorer's Network tab, look at:")
        for text in ("The network profile is set to Private", "Network discovery is turned on",
                     "File and printer sharing is turned on"):
            win_group.add(Adw.ActionRow(title=text, icon_name="computer-symbolic"))
        self.add(win_group)
        self.refresh()

    def refresh(self, root: bool = False) -> None:
        self.refresh_btn.set_sensitive(False)
        self.full_btn.set_sensitive(False)
        self.summary.set_description("Checking… you may be asked for your password." if root else "Checking…")
        in_background(lambda: self.backend.doctor(root=root), self._show)

    def _show(self, results, error) -> None:
        self.refresh_btn.set_sensitive(True)
        self.full_btn.set_sensitive(True)
        clear(self.summary, self.rows)
        if error:
            self.summary.set_description("Could not run the health check.")
            return self.fail(error)
        bad = [r for r in results if r["status"] == "fail"]
        warn = [r for r in results if r["status"] == "warn"]
        skipped = [r for r in results if r["status"] == "skip"]
        text = ("Everything looks good." if not bad and not warn
                else f"{len(bad)} problem{'s' * (len(bad) != 1)}, {len(warn)} warning{'s' * (len(warn) != 1)}.")
        if skipped:
            text += (f" {len(skipped)} check{'s need' if len(skipped) != 1 else ' needs'} administrator rights — press Full check.")
        self.summary.set_description(text)
        # problems first, then what passed, then what could not be checked
        for r in sorted(results, key=lambda r: {"fail": 0, "warn": 1, "ok": 2, "skip": 3}[r["status"]]):
            detail = r.get("detail") or ""
            if r["status"] == "skip":
                detail = "Not checked: needs administrator rights"
            sub = " — ".join(x for x in (detail, f"Fix: {r['fix']}" if r.get("fix") else "") if x)
            row = Adw.ActionRow(title=esc(r["name"].strip()), subtitle=esc(sub))
            row.set_subtitle_selectable(True)
            icon, css = _ICONS[r["status"]]
            img = Gtk.Image.new_from_icon_name(icon)
            img.add_css_class(css)
            row.add_prefix(img)
            self.summary.add(row)
            self.rows.append(row)


# ----------------------------------------------------------------------------- Share from this computer
class SharePage(Page):
    def __init__(self, win):
        super().__init__(win)
        self.group = Adw.PreferencesGroup(
            title="Folders shared from this computer",
            description="Windows PCs on your network can open these. Each needs a Samba password for the allowed user.")
        add_btn = Gtk.Button(label="Add folder…", valign=Gtk.Align.CENTER)
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", lambda *_: self.add_dialog())
        self.group.set_header_suffix(add_btn)
        self.add(self.group)
        self.rows: list = []
        self.refresh()

    def refresh(self) -> None:
        in_background(self.backend.shares, self._show)

    def _show(self, shares, error) -> None:
        clear(self.group, self.rows)
        if error:
            return self.fail(error)
        if not shares:
            row = Adw.ActionRow(title="Nothing is shared yet", subtitle="Use “Add folder…” to share one.")
            self.group.add(row)
            self.rows.append(row)
        for s in shares:
            mode = "read-only" if s["read_only"] else "read & write"
            row = Adw.ActionRow(title=esc(s["name"]), subtitle=esc(f"{s['path']}  ·  {mode}  ·  users: {', '.join(s['users'])}"))
            row.add_prefix(Gtk.Image.new_from_icon_name("folder-publicshare-symbolic"))
            pw = Gtk.Button(icon_name="dialog-password-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Set the Samba password")
            pw.add_css_class("flat")
            pw.connect("clicked", lambda _b, u=s["users"][0]: self.password_dialog(u))
            rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Stop sharing")
            rm.add_css_class("flat")
            rm.connect("clicked", lambda _b, n=s["name"]: self.confirm_remove(n))
            row.add_suffix(pw)
            row.add_suffix(rm)
            self.group.add(row)
            self.rows.append(row)

    # -- add a folder
    def add_dialog(self) -> None:
        name = Adw.EntryRow(title="Share name")
        users = Adw.EntryRow(title="Allowed users (space separated)")
        users.set_text(getpass.getuser())
        rw = Adw.SwitchRow(title="Allow changes", subtitle="Windows may create, edit and delete files")
        folder = Adw.ActionRow(title="Folder", subtitle="Choose a folder…", activatable=True)
        folder.add_suffix(Gtk.Image.new_from_icon_name("folder-open-symbolic"))
        chosen: dict[str, str] = {}

        def pick(*_):
            def done(dlg, res):
                try:
                    f = dlg.select_folder_finish(res)
                except GLib.Error:
                    return
                chosen["path"] = f.get_path()
                folder.set_subtitle(esc(chosen["path"]))
                if not name.get_text():
                    name.set_text(slug(f.get_basename()).replace("-", " ").title().replace(" ", ""))
            Gtk.FileDialog(title="Folder to share").select_folder(self.win, None, done)
        folder.connect("activated", pick)

        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        for w in (folder, name, users, rw):
            box.append(w)
        dlg = Adw.AlertDialog(heading="Share a folder", body="It becomes visible to Windows PCs on your network.")
        dlg.set_extra_child(box)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("add", "Share")
        dlg.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, resp):
            if resp != "add":
                return
            self.do_add(name.get_text(), chosen.get("path", ""), users.get_text(), rw.get_active())
        dlg.connect("response", on_response)
        dlg.present(self.win)

    def do_add(self, name: str, path: str, users_text: str, read_write: bool) -> None:
        name, users = name.strip(), users_text.split()
        if not path or not name or not users:
            return self.win.toast("Choose a folder, give the share a name, and list who may open it.", error=True)
        self.win.busy("Sharing folder — you may be asked for your password…")
        in_background(lambda: self.backend.add_share(name, path, users, read_write),
                      lambda _r, err: self._changed(err, f"Sharing “{name}”. Now set a Samba password (key icon)."))

    def confirm_remove(self, name: str) -> None:
        dlg = Adw.AlertDialog(heading=f"Stop sharing “{name}”?", body="The folder and its files stay where they are.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("remove", "Stop sharing")
        dlg.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.connect("response", lambda _d, resp: self.do_remove(name) if resp == "remove" else None)
        dlg.present(self.win)

    def do_remove(self, name: str) -> None:
        self.win.busy("Removing share…")
        in_background(lambda: self.backend.remove_share(name), lambda _r, err: self._changed(err, f"Stopped sharing “{name}”."))

    def _changed(self, error, message: str) -> None:
        self.win.idle()
        if error:
            return self.fail(error)
        self.win.toast(message)
        self.refresh()
        self.win.status.refresh()

    # -- samba password
    def password_dialog(self, user: str) -> None:
        pw1 = Adw.PasswordEntryRow(title="New password")
        pw2 = Adw.PasswordEntryRow(title="Repeat password")
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        box.append(pw1)
        box.append(pw2)
        dlg = Adw.AlertDialog(heading=f"Samba password for “{user}”",
                              body="Windows asks for this when it opens your shared folder. It can differ from your login password.")
        dlg.set_extra_child(box)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("set", "Set password")
        dlg.set_response_appearance("set", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, resp):
            if resp == "set":
                self.do_set_password(user, pw1.get_text(), pw2.get_text())
        dlg.connect("response", on_response)
        dlg.present(self.win)

    def do_set_password(self, user: str, pw1: str, pw2: str) -> None:
        if pw1 != pw2:
            return self.win.toast("The two passwords don't match.", error=True)
        if not pw1:
            return self.win.toast("Enter a password.", error=True)
        self.win.busy("Setting password…")
        in_background(lambda: self.backend.set_samba_password(user, pw1), lambda _r, err: self._changed(err, f"Password set for “{user}”."))


# ----------------------------------------------------------------------------- Windows PCs
class WindowsPage(Page):
    def __init__(self, win):
        super().__init__(win)
        self.found = Adw.PreferencesGroup(title="Windows computers on your network", description="Press Scan to look for them.")
        self.scan_btn = Gtk.Button(label="Scan", valign=Gtk.Align.CENTER)
        self.scan_btn.add_css_class("suggested-action")
        self.scan_btn.connect("clicked", lambda *_: self.scan())
        self.found.set_header_suffix(self.scan_btn)
        self.add(self.found)
        self.mounted = Adw.PreferencesGroup(title="Windows folders on this computer", description="They connect when you open them and disconnect when idle.")
        self.add(self.mounted)
        self.found_rows: list = []
        self.mount_rows: list = []
        self.refresh_mounts()

    # -- scanning
    def scan(self) -> None:
        self.scan_btn.set_sensitive(False)
        self.found.set_description("Scanning…")
        in_background(lambda: self.backend.scan(6.0), self._scanned)

    def _scanned(self, hosts, error) -> None:
        self.scan_btn.set_sensitive(True)
        clear(self.found, self.found_rows)
        if error:
            self.found.set_description("Scan failed.")
            return self.fail(error)
        self.found.set_description("Select a computer to connect." if hosts else
                                   "No Windows computers answered. Is the PC on, with Network discovery enabled?")
        for h in hosts:
            row = Adw.ActionRow(title=esc(h["name"]), subtitle=esc(f"{h['address']}  ·  {h['group']}"), activatable=True)
            row.add_prefix(Gtk.Image.new_from_icon_name("computer-symbolic"))
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda _r, h=h: self.login_dialog(h))
            self.found.add(row)
            self.found_rows.append(row)

    # -- login -> choose share -> mount
    def login_dialog(self, host: dict) -> None:
        user = Adw.EntryRow(title="Windows username")
        pw = Adw.PasswordEntryRow(title="Password")
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        box.append(user)
        box.append(pw)
        dlg = Adw.AlertDialog(heading=f"Connect to {host['name']}",
                              body="Use the account you sign in to Windows with. For a Microsoft account, that is your email address.")
        dlg.set_extra_child(box)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("connect", "Connect")
        dlg.set_response_appearance("connect", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, resp):
            if resp == "connect":
                self.do_login(host, user.get_text(), pw.get_text())
        dlg.connect("response", on_response)
        dlg.present(self.win)

    def do_login(self, host: dict, user: str, password: str) -> None:
        user = user.strip()
        if not user or not password:
            return self.win.toast("Enter the username and password.", error=True)
        self.win.busy(f"Connecting to {host['name']}…")
        in_background(lambda: self.backend.remote_shares(host["address"], user, password),
                      lambda shares, err: self._logged_in(host, user, password, shares, err))

    def _logged_in(self, host, user, password, shares, error) -> None:
        self.win.idle()
        if error:
            return self.fail(error)
        if not shares:
            return self.win.toast("Logged in, but this PC shares no folders.", error=True)
        names = Gtk.StringList.new([s["name"] for s in shares])
        combo = Adw.ComboRow(title="Folder", model=names)
        local = Adw.EntryRow(title="Name on this computer")
        local.set_text(slug(f"{host['name']}-{shares[0]['name']}"))
        combo.connect("notify::selected", lambda *_: local.set_text(slug(f"{host['name']}-{names.get_string(combo.get_selected())}")))
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        box.append(combo)
        box.append(local)
        dlg = Adw.AlertDialog(heading="Which folder?", body=f"Logged in to {host['name']} as {user}.")
        dlg.set_extra_child(box)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("mount", "Connect folder")
        dlg.set_response_appearance("mount", Adw.ResponseAppearance.SUGGESTED)

        def on_response(_d, resp):
            if resp == "mount":
                self.do_mount(host, user, password, names.get_string(combo.get_selected()), local.get_text())
        dlg.connect("response", on_response)
        dlg.present(self.win)

    def do_mount(self, host: dict, user: str, password: str, share: str, name: str) -> None:
        name = name.strip()
        if not name:
            return self.win.toast("Give the folder a name.", error=True)
        self.win.busy("Connecting folder — you may be asked for your password…")
        in_background(lambda: self.backend.add_mount(name, host["address"], share, user, password), lambda _r, err: self._mounted(err, name))

    def _mounted(self, error, name: str) -> None:
        self.win.idle()
        if error:
            return self.fail(error)
        self.win.toast(f"“{name}” is ready. Press Open to browse it.")
        self.refresh_mounts()
        self.win.status.refresh()

    # -- mounted list
    def refresh_mounts(self) -> None:
        in_background(self.backend.mounts, self._show_mounts)

    def _show_mounts(self, mounts, error) -> None:
        clear(self.mounted, self.mount_rows)
        if error:
            return self.fail(error)
        if not mounts:
            row = Adw.ActionRow(title="No Windows folders connected", subtitle="Scan, pick a computer, then choose a folder.")
            self.mounted.add(row)
            self.mount_rows.append(row)
        for m in mounts:
            row = Adw.ActionRow(title=esc(m["name"]), subtitle=esc(f"//{m['host']}/{m['share']}  →  {m['mountpoint']}"))
            row.add_prefix(Gtk.Image.new_from_icon_name("folder-remote-symbolic"))
            open_btn = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
            open_btn.connect("clicked", lambda _b, p=m["mountpoint"]: Gio.AppInfo.launch_default_for_uri(f"file://{p}", None))
            rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Disconnect and forget")
            rm.add_css_class("flat")
            rm.connect("clicked", lambda _b, n=m["name"]: self.confirm_remove(n))
            row.add_suffix(open_btn)
            row.add_suffix(rm)
            self.mounted.add(row)
            self.mount_rows.append(row)

    def confirm_remove(self, name: str) -> None:
        dlg = Adw.AlertDialog(heading=f"Disconnect “{name}”?", body="The saved login is deleted. Files on the Windows PC are not touched.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("remove", "Disconnect")
        dlg.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)

        dlg.connect("response", lambda _d, resp: self.do_unmount(name) if resp == "remove" else None)
        dlg.present(self.win)

    def do_unmount(self, name: str) -> None:
        self.win.busy("Disconnecting…")
        in_background(lambda: self.backend.remove_mount(name), lambda _r, err: (self.win.idle(), self.fail(err) if err else self.refresh_mounts()))


# ----------------------------------------------------------------------------- Window
class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, backend: Backend):
        super().__init__(application=app, title="LAN Share", default_width=760, default_height=700)
        self.backend = backend
        self.overlay = Adw.ToastOverlay()
        self.progress = Gtk.ProgressBar(visible=False)
        self.progress.add_css_class("osd")

        self.stack = Adw.ViewStack()
        self.status = StatusPage(self)
        self.sharing = SharePage(self)
        self.windows = WindowsPage(self)
        self.stack.add_titled_with_icon(self.sharing, "share", "Share", "folder-publicshare-symbolic")
        self.stack.add_titled_with_icon(self.windows, "windows", "Windows PCs", "computer-symbolic")
        self.stack.add_titled_with_icon(self.status, "status", "Health", "emblem-system-symbolic")

        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        bottom = Adw.ViewSwitcherBar(stack=self.stack)
        bottom.set_reveal(False)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.add_top_bar(self.progress)
        view.set_content(self.stack)
        view.add_bottom_bar(bottom)
        self.overlay.set_child(view)
        self.set_content(self.overlay)

        # narrow windows (phones, tiled): move the switcher to the bottom
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 540px"))
        bp.add_setter(bottom, "reveal", True)
        bp.add_setter(switcher, "visible", False)
        self.add_breakpoint(bp)

        self._pulse_id = 0

    def toast(self, message: str, error: bool = False) -> None:
        t = Adw.Toast(title=message, timeout=8 if error else 4)
        self.overlay.add_toast(t)

    def busy(self, message: str) -> None:
        self.toast(message)
        self.progress.set_visible(True)
        self._pulse_id = GLib.timeout_add(120, lambda: (self.progress.pulse(), True)[1])

    def idle(self) -> None:
        if self._pulse_id:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = 0
        self.progress.set_visible(False)


def run(backend: Optional[Backend] = None) -> int:
    app = Adw.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
    be = backend or Backend()
    app.connect("activate", lambda a: MainWindow(a, be).present())
    return app.run(None)
