# lan-share v0.1.0

First release. Makes a Linux computer appear in Windows Explorer's **Network** tab and serve files to Windows, and mounts Windows shared folders on Linux — from the command line or a GTK 4 app.

## Downloads

| Distribution | File |
|---|---|
| Debian, Ubuntu and derivatives | `lan-share_0.1.0_all.deb` |
| Fedora, RHEL / Rocky / Alma, openSUSE | `lan-share-0.1.0-1.noarch.rpm` |
| Arch, Manjaro | `lan-share-0.1.0-1-any.pkg.tar.zst` |
| Any other systemd distribution | `lan-share-0.1.0.tar.gz` (`sudo ./packaging/install.sh install`) |

Verify with `sha256sum -c SHA256SUMS`. **The packages are unsigned.** Install instructions: see the [README](../README.md#install).

## Highlights

- `lan-share setup` (dry run first) installs and configures Samba, WS-Discovery, firewall rules and SELinux labels, restricted to your LAN, SMB2+ with signing required, no guest access.
- Bundled `wsdd` v0.8 (MIT) so the Network tab works the same on every distribution, whatever the distro calls its own wsdd package.
- `scan` / `shares` / `mount` — open Windows folders on Linux on first access; they disconnect when idle and never block boot.
- `doctor` diagnoses the whole chain and prints a fix for each problem; `teardown` undoes `setup`, removing only what it added.
- GTK 4 / libadwaita app (needs libadwaita 1.5+) with a polkit-gated helper, so the app never runs as root.

## What was verified

- **Real machine:** Fedora 44 laptop with a real Windows PC — Network tab discovery, share access with Samba login, mounting a Windows share on Linux.
- **9 distribution images in containers** (Debian 12/13, Ubuntu 22.04/24.04, Fedora, Rocky 9, Arch, openSUSE Leap/Tumbleweed): real package names and installs, generated Samba config accepted by each distro's own `testparm`, bundled wsdd on Python 3.9–3.15, and install → run → uninstall of every package format.
- 75 automated tests, including the real GTK window headless; security-relevant tests were mutation-checked.

## Known limitations

- Containers cannot test systemd startup, real firewalls or Windows visibility: end-to-end verification is **Fedora only**.
- The GUI needs libadwaita ≥ 1.5 (not Ubuntu 22.04, Debian 12 or Rocky 9; the CLI works there).
- The polkit prompt and the GUI's dialogs are test-covered but not click-tested on a real desktop.
- One Windows PC tested; domain-joined machines and NAS devices untested.
- Firewall/SELinux changes made before this release recorded state cannot be undone automatically by `teardown`.
- Debian package lacks a `copyright` file; packages are unsigned.
