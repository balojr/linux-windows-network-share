# lan-share v0.2.0

A feature release: Windows can now be allowed to **run programs** from a shared folder.

## What's new

**Run programs from a share.** By default Windows can read a share, but double-clicking an `.exe` or `.msi` on it fails with "you do not have permission". Samba only lets Windows *open a file for execution* when the file has Linux's execute bit, and files saved by a browser or copied in never do.

There is now a per-share switch, **off by default**, that sets Samba's `acl allow execute always = yes` for that share only. Samba never runs anything itself; this only lets Windows run the files.

- Command line: `lan-share share add NAME PATH --user U --allow-run`, or change an existing share with `sudo lan-share share set NAME --allow-run --apply` (undo with `--no-allow-run`). `share list` shows it.
- App: an *Allow running programs* switch in the add dialog and a labelled *Run programs* switch on every share. If the change is refused (for example the password prompt is cancelled) the switch goes back to what is really saved.
- Privileged helper: accepts exactly the new command shapes and rejects contradictory, missing or unknown options.
- Only enable it for folders you trust, and be careful if other people or programs can write into the folder. The README explains the cause, the share-wide fix, and the single-file alternative (`chmod u+x file`).

**One-line installer.** `get.sh` (attached to this release) finds the latest release, picks the package for your distribution, verifies its SHA-256 and installs it:

```bash
curl -fsSL https://github.com/balojr/linux-windows-network-share/releases/latest/download/get.sh | sh
```

It runs as your normal user, uses `sudo` only for the install, and never runs `lan-share setup --apply`. A read-it-first alternative and `--dry-run` are in the README.

## Upgrading

Existing `/etc/lan-share/config.json` files keep working; the new field defaults to off. Install the new package over the old one with your package manager (or run the one-liner), then enable the option on any share that needs it.

## Downloads

| Distribution | File |
|---|---|
| Debian, Ubuntu and derivatives | `lan-share_0.2.0_all.deb` |
| Fedora, RHEL / Rocky / Alma, openSUSE | `lan-share-0.2.0-1.noarch.rpm` |
| Arch, Manjaro | `lan-share-0.2.0-1-any.pkg.tar.zst` |
| Any other systemd distribution | `lan-share-0.2.0.tar.gz` (`sudo ./packaging/install.sh install`) |

Verify with `sha256sum -c SHA256SUMS`. **The packages are unsigned.** Installation details are in the [README](../README.md#install).

## What was verified

- 81 automated tests (up from 75), including the real GTK window headless. The new behaviour was mutation-checked: always emitting the setting, accepting contradictory helper options, not reverting the switch after a refusal, and firing a change while a row is built each make a test fail.
- Every distribution's real `testparm` accepts the new setting on the share that asked for it, and it is absent from shares that did not (Debian 12/13, Ubuntu 22.04/24.04, Fedora, Rocky 9, Arch, openSUSE Leap/Tumbleweed).
- The packages install, run and uninstall through each distribution's package manager on those 9 images, and the one-line installer was tested on all of them, including a dry run, a non-root user with sudo, and a tampered package that must be refused.

## Known limitations

- Containers cannot test systemd startup, real firewalls or Windows visibility: end-to-end verification is still **Fedora only**, against one Windows PC.
- The GUI needs libadwaita 1.5 or newer (not Ubuntu 22.04, Debian 12 or Rocky 9; the command line works there).
- The polkit prompt and the GUI's dialogs and switches are covered by tests but have not been click-tested on a real desktop.
- The one-line installer's checksum comes from the same release as the package, so it catches corrupt downloads, not a compromised release. Packages are unsigned.
- Firewall and SELinux changes made before `state.json` existed cannot be removed automatically by `teardown`.
