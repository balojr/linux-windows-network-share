"""Runs INSIDE a distro container (as root) to verify lan-share's per-distro assumptions.

Prints PASS/FAIL/INFO lines. Exit status is the number of FAILs.
"""
import glob
import os
import subprocess
import sys

from lan_share import config, distro as D, doctor, samba
from lan_share.network import detect_lan
from lan_share.runner import Context, which

fails = 0


def report(kind, msg):
    global fails
    fails += kind == "FAIL"
    print(f"{kind} {msg}", flush=True)


def check(cond, msg):
    report("PASS" if cond else "FAIL", msg)
    return cond


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


ctx = Context()
d = D.detect(ctx)
report("INFO", f"{d.name}: family={d.family} pm={d.pkg_manager} python={sys.version.split()[0]}")
check(d.family != "unknown", "family detected")

# --- 1. refresh metadata, then try the packages our table names -----------------
refresh = {"apt": ["apt-get", "update", "-qq"], "dnf": ["dnf", "-q", "makecache"],
           "pacman": ["pacman", "-Sy", "--noconfirm"], "zypper": ["zypper", "--non-interactive", "refresh"]}[d.pkg_manager]
sh(refresh)

logicals = ["samba", "avahi", "cifs", "smbclient"] + (["selinux-tools"] if d.package("selinux-tools") else [])
pkgs = {}
for l in logicals:
    name = d.package(l)
    if name:
        pkgs[l] = name

# availability of each name individually (so a wrong name is pinpointed)
avail_cmd = {"apt": lambda p: ["apt-cache", "show", p], "dnf": lambda p: ["dnf", "-q", "info", p],
             "pacman": lambda p: ["pacman", "-Si", p], "zypper": lambda p: ["zypper", "--non-interactive", "info", p]}[d.pkg_manager]
bad = set()
for l, p in pkgs.items():
    r = sh(avail_cmd(p))
    ok = r.returncode == 0 and (d.pkg_manager != "apt" or r.stdout.strip())
    if not check(ok, f"package '{p}' exists in repos (for {l})"):
        bad.add(l)

# --- 2. install exactly what setup would install ---------------------------------
install = D.install_command(d, [p for l, p in pkgs.items() if l not in bad])
env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
r = subprocess.run(install, capture_output=True, text=True, env=env)
if not check(r.returncode == 0, "install command succeeds: " + " ".join(install[-8:])):
    print("\n".join((r.stdout + r.stderr).splitlines()[-12:]))

for l in logicals:
    if l in pkgs:
        check(D.is_installed(l), f"{l}: expected binary present ({'/'.join(D.BINARIES[l])})")

# --- 3. service unit names and wsdd args mechanism -------------------------------
unit_dirs = ["/usr/lib/systemd/system", "/lib/systemd/system", "/etc/systemd/system", "/usr/local/lib/systemd/system"]
def unit_file(name):
    for u in unit_dirs:
        if os.path.exists(f"{u}/{name}.service"):
            return f"{u}/{name}.service"

for key, unit in d.service_names.items():
    if key == "wsdd" and "wsdd" not in pkgs:
        continue
    check(unit_file(unit), f"systemd unit {unit}.service exists (for {key})")

# --- 3b. bundled wsdd works on THIS distro's Python and our unit text is sane
from lan_share import wsdd_service as W
import tempfile, time, signal
check(sh([sys.executable, str(W.VENDORED), "--help"]).returncode == 0, f"bundled wsdd --help runs on python {sys.version.split()[0]}")
ifaces = [i.name for i in detect_lan()] or ["eth0"]
unit = W.render_unit(which("python3") or sys.executable, d.service_names["samba"], ifaces, "WORKGROUP")
check("ExecStart=" in unit and f"-i {ifaces[0]}" in unit, "unit renders for this distro")
# Host mode exactly as the unit launches it (chroot into an empty dir, IPv4, LAN iface), for ~3s.
chroot = tempfile.mkdtemp()
cmd = [sys.executable, str(W.VENDORED), "--shortlog", f"--chroot={chroot}", "-4", "-i", ifaces[0], "-w", "WORKGROUP", "-v"]
proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env={**os.environ, "PYTHONUNBUFFERED": "1"})
time.sleep(3)
alive = proc.poll() is None
proc.send_signal(signal.SIGINT)
try:
    out, _ = proc.communicate(timeout=5)
except subprocess.TimeoutExpired:
    proc.kill(); out, _ = proc.communicate()
check(alive, "bundled wsdd host mode stays running (unit-style args, chroot)")
check("joined multicast group" in out, "bundled wsdd joined the WS-Discovery multicast group")
if not alive or "joined multicast group" not in out:
    print("\n".join(out.splitlines()[-8:]))

# --- 4. generated Samba config accepted by the REAL testparm, after the distro's own smb.conf
if which("testparm"):
    lan = detect_lan()
    report("INFO", f"container LAN interfaces: {[i.name for i in lan]}")
    os.makedirs("/srv/test", exist_ok=True)
    cfg = config.Config()
    cfg.add_share(config.Share("test", "/srv/test", ["root"], read_only=False))
    os.makedirs("/etc/samba", exist_ok=True)
    smb_conf_existed = os.path.exists("/etc/samba/smb.conf")
    report("INFO", f"distro ships /etc/samba/smb.conf: {smb_conf_existed}")
    from lan_share.runner import write_file
    write_file(ctx.path(samba.CONF_PATH), samba.render(cfg, lan or []))
    samba.ensure_include(ctx)
    r = sh(["testparm", "-s", "--suppress-prompt"])
    check(r.returncode == 0, "testparm accepts smb.conf + lan-share include")
    if r.returncode != 0:
        print(r.stderr[-800:])
    g = sh(["testparm", "-s", "--suppress-prompt", "--section-name=global"]).stdout
    p = doctor.parse_testparm(g)
    check(p.get("bind interfaces only", "").lower() == "yes", "effective: bind interfaces only = Yes")
    check(p.get("hosts deny", "").lower() == "all", "effective: hosts deny = ALL")
    check(p.get("server signing", "").lower() in ("required", "mandatory"), f"effective: server signing required (got {p.get('server signing')!r})")
    full = sh(["testparm", "-s", "--suppress-prompt"]).stdout
    # distro-shipped shares (printers etc.) must not have lost the global hosts restriction semantics
    check("[test]" in full, "our [test] share is present in the effective config")
    report("INFO", "shares present: " + ", ".join(ln.strip("[]") for ln in full.splitlines() if ln.startswith("[")))

# --- 5. SELinux tooling name (only meaningful on fedora family) --------------------
if d.package("selinux-tools"):
    check(which("semanage"), "semanage provided by policycoreutils-python-utils")

sys.exit(min(fails, 100))
