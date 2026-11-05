#!/usr/bin/env bash
# Build .deb / .rpm / Arch packages in their native containers, then install each one into the
# distro containers it targets (letting the package manager resolve dependencies), run the CLI,
# and uninstall again. Output: $OUT/pkgs/ and per-target logs.
set -u
cd "$(dirname "$0")/../.."
OUT=${PKG_OUT:-/tmp/lan-share-pkgs}; mkdir -p "$OUT/pkgs" "$OUT/logs"
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
./packaging/dist.sh >/dev/null
D="docker run --rm --security-opt label=disable -e HOST_UID=$(id -u) -e HOST_GID=$(id -g)"
MNT="-v $PWD:/src:ro -v $OUT/pkgs:/out"

if [ -z "${SKIP_BUILD:-}" ]; then
echo "== building"
( $D $MNT debian:stable-slim sh -c '
  set -e; cp -r /src /tmp/b; cd /tmp/b
  DESTDIR=/tmp/pkg PREFIX=/usr ./packaging/install.sh install >/dev/null
  mkdir /tmp/pkg/DEBIAN
  cat > /tmp/pkg/DEBIAN/control <<CTL
Package: lan-share
Version: '$VERSION'
Architecture: all
Maintainer: lan-share contributors <noreply@example.invalid>
Section: net
Priority: optional
Depends: python3 (>= 3.9)
Recommends: samba, avahi-daemon, cifs-utils, python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1, pkexec | policykit-1
Description: Show this machine in the Windows Network tab and mount Windows shares
 Configures Samba, bundled WS-Discovery (wsdd), firewall and SELinux. Run
 "sudo lan-share setup" for a dry run, then add --apply.
CTL
  dpkg-deb --root-owner-group --build /tmp/pkg /out/lan-share_'$VERSION'_all.deb >/dev/null
  chown $HOST_UID:$HOST_GID /out/*' > "$OUT/logs/build-deb.log" 2>&1; echo "deb: $?" ) &
( $D $MNT fedora:latest sh -c '
  set -e; dnf install -y -q rpm-build tar gzip make >/dev/null
  mkdir -p /root/rpmbuild/SOURCES /root/rpmbuild/SPECS; cp /src/dist/lan-share-'$VERSION'.tar.gz /root/rpmbuild/SOURCES/
  cp /src/packaging/lan-share.spec /root/rpmbuild/SPECS/; rpmbuild -bb /root/rpmbuild/SPECS/lan-share.spec >/dev/null
  cp /root/rpmbuild/RPMS/noarch/*.rpm /out/; chown $HOST_UID:$HOST_GID /out/*' > "$OUT/logs/build-rpm.log" 2>&1; echo "rpm: $?" ) &
( $D $MNT archlinux:latest sh -c '
  set -e; pacman -Sy --noconfirm base-devel >/dev/null 2>&1; useradd -m builder
  install -d -o builder /home/builder/pkg; cp /src/packaging/PKGBUILD /src/dist/lan-share-'$VERSION'.tar.gz /home/builder/pkg/; chown -R builder /home/builder/pkg
  su builder -c "cd ~/pkg && makepkg -f --nodeps >/dev/null 2>&1"
  cp /home/builder/pkg/*.pkg.tar.zst /out/; chown $HOST_UID:$HOST_GID /out/*' > "$OUT/logs/build-arch.log" 2>&1; echo "arch: $?" ) &
wait
fi
ls -1 "$OUT/pkgs"

echo "== installing"
run_target() { # image pkgfile install-cmd remove-cmd
  img=$1; pkg=$2; inst=$3; rm=$4
  log="$OUT/logs/install-$(echo "$img" | tr '/:' '__').log"
  $D -v "$OUT/pkgs":/pkgs:ro "$img" sh -c "
    set -e
    $inst
    echo '--- lan-share --version'; lan-share --version
    echo '--- detect'; lan-share detect
    echo '--- setup (dry run)'; lan-share setup >/tmp/setup.out 2>&1; echo \"dry-run exit \$?\"; head -4 /tmp/setup.out
    echo '--- teardown (dry run)'; lan-share teardown >/tmp/td.out 2>&1; echo \"teardown exit \$?\"; grep -c 'dry run' /tmp/td.out
    echo '--- polkit'; test -x /usr/lib/lan-share/lan-share-helper && grep -q '/usr/lib/lan-share/lan-share-helper' /usr/share/polkit-1/actions/io.github.lan_share.policy || { echo 'NO HELPER/POLICY'; exit 1; }
    rc=0; /usr/lib/lan-share/lan-share-helper doctor --json >/dev/null 2>&1 || rc=\$?; test \$rc -le 1 || { echo 'HELPER BROKEN'; exit 1; }; echo 'helper runs'
    rc=0; /usr/lib/lan-share/lan-share-helper --root /x doctor --json >/dev/null 2>&1 || rc=\$?; test \$rc -eq 2 || { echo 'HELPER ACCEPTED --root'; exit 1; }; echo 'helper rejects --root'
    echo '--- gui'; test -f /usr/share/applications/io.github.lan_share.desktop || { echo 'NO DESKTOP FILE'; exit 1; }
    rc=0; out=\$(env -u DISPLAY -u WAYLAND_DISPLAY timeout 20 lan-share gui 2>&1) || rc=\$?
    case \"\$out\" in *'needs GTK 4 and libadwaita'*) echo \"GUI: unavailable here (rc \$rc): \$(echo \"\$out\" | head -c 90)\";; *) echo \"GUI: imports fine, no display (rc \$rc)\";; esac
    echo '--- doctor runs'; lan-share doctor >/dev/null 2>&1 || test \$? -eq 1; echo 'doctor ok'
    echo '--- uninstall'; $rm
    # (not command -v: dash answers from its hash cache even after the file is gone)
    test ! -e /usr/bin/lan-share || { echo 'LEFTOVER binary'; exit 1; }
    test ! -e /usr/share/polkit-1/actions/io.github.lan_share.policy || { echo 'LEFTOVER policy'; exit 1; }
    test ! -e /usr/lib/lan-share || { echo 'LEFTOVER libdir'; exit 1; }
    test ! -e /usr/share/applications/io.github.lan_share.desktop || { echo 'LEFTOVER desktop file'; exit 1; }
    echo 'ALL-OK'" > "$log" 2>&1
  printf '%-28s %s\n' "$img" "$(tail -1 "$log")"
}
DEB="lan-share_${VERSION}_all.deb"; RPM="lan-share-${VERSION}-1.noarch.rpm"; ARCH="lan-share-${VERSION}-1-any.pkg.tar.zst"
APT_I="apt-get update -qq >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq /pkgs/$DEB >/dev/null"
APT_R="DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq lan-share >/dev/null"
for img in debian:stable-slim debian:bookworm-slim ubuntu:22.04 ubuntu:24.04; do run_target "$img" "$DEB" "$APT_I" "$APT_R" & done
run_target fedora:latest "$RPM" "dnf install -y -q /pkgs/$RPM >/dev/null" "dnf remove -y -q lan-share >/dev/null" &
run_target rockylinux:9 "$RPM" "dnf install -y -q /pkgs/$RPM >/dev/null" "dnf remove -y -q lan-share >/dev/null" &
for img in opensuse/leap:latest opensuse/tumbleweed:latest; do
  run_target "$img" "$RPM" "zypper --non-interactive --no-gpg-checks install /pkgs/$RPM >/dev/null" "zypper --non-interactive remove lan-share >/dev/null" & done
run_target archlinux:latest "$ARCH" "pacman -Sy --noconfirm >/dev/null 2>&1; pacman -U --noconfirm /pkgs/$ARCH >/dev/null 2>&1" "pacman -R --noconfirm lan-share >/dev/null 2>&1" &
wait
