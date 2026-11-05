#!/bin/sh
# lan-share installer: downloads the right package from the latest GitHub release, verifies its
# SHA-256 checksum, and installs it with your distribution's package manager.
#
#   curl -fsSL https://github.com/balojr/linux-windows-network-share/releases/latest/download/get.sh | sh
#   curl -fsSL .../get.sh | sh -s -- --dry-run        # show what it would do, change nothing
#
# It only installs the package. It never runs `lan-share setup --apply`; that stays your decision.
# Run as a normal user: sudo is used only for the final install step.
set -eu

RELEASES_URL=${LAN_SHARE_RELEASES_URL:-https://github.com/balojr/linux-windows-network-share/releases}
VERSION=${LAN_SHARE_VERSION:-}
DRY_RUN=0
FORCE_TARBALL=0

say() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
	cat <<'EOF'
Usage: get.sh [--dry-run] [--tarball] [--version X.Y.Z]

  --dry-run        show the package and commands, download nothing but the checksum list, install nothing
  --tarball        install from the source tarball (packaging/install.sh) instead of a native package
  --version X.Y.Z  install this release instead of the latest (also: LAN_SHARE_VERSION=X.Y.Z)

Needs: curl (or wget with --version), tar, sha256sum, and root or sudo.
EOF
}

while [ $# -gt 0 ]; do
	case $1 in
	--dry-run) DRY_RUN=1 ;;
	--tarball) FORCE_TARBALL=1 ;;
	--version) shift; [ $# -gt 0 ] || die "--version needs a value"; VERSION=$1 ;;
	-h | --help) usage; exit 0 ;;
	*) usage >&2; die "unknown option: $1" ;;
	esac
	shift
done

have() { command -v "$1" >/dev/null 2>&1; }

fetch() { # url destination
	if have curl; then
		curl -fsSL -o "$2" "$1" || return 1
	elif have wget; then
		wget -q -O "$2" "$1" || return 1
	else
		die "need curl or wget"
	fi
}

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM

# --- which release -----------------------------------------------------------------------------
if [ -z "$VERSION" ]; then
	have curl || die "resolving the latest release needs curl; with wget pass --version X.Y.Z"
	say "Finding the latest release"
	final=$(curl -fsSL -o /dev/null -w '%{url_effective}' "$RELEASES_URL/latest") ||
		die "could not reach $RELEASES_URL/latest (no network, no releases yet, or a private repository?)"
	TAG=${final##*/}
else
	TAG=v${VERSION#v}
fi
case $TAG in v[0-9]*) ;; *) die "unexpected release tag '$TAG'" ;; esac
BASE="$RELEASES_URL/download/$TAG"
say "Release $TAG"

# --- which package -----------------------------------------------------------------------------
osr=
if [ -r /etc/os-release ]; then
	osr=$( . /etc/os-release; printf ' %s %s ' "${ID:-}" "${ID_LIKE:-}")
fi
KIND=tarball
if [ "$FORCE_TARBALL" -eq 0 ]; then
	case $osr in
	*" suse "* | *" opensuse"* | *" sles "*) KIND=rpm-suse ;;
	*" fedora "* | *" rhel "* | *" centos "*) KIND=rpm ;;
	*" debian "* | *" ubuntu "*) KIND=deb ;;
	*" arch "*) KIND=arch ;;
	esac
fi
case $KIND in
deb) PATTERN='_all\.deb$' ;;
rpm | rpm-suse) PATTERN='\.noarch\.rpm$' ;;
arch) PATTERN='-any\.pkg\.tar\.zst$' ;;
tarball) PATTERN='\.tar\.gz$' ;;
esac
say "System: ${osr:-unknown}-> installing as: $KIND"

# Native packages declare python3 as a dependency, so the package manager supplies it; the tarball does not.
if [ "$KIND" = tarball ]; then
	have python3 || die "python3 (3.9 or newer) is required for the tarball install; install it and run this again"
fi

SUDO=
if [ "$(id -u)" -ne 0 ]; then
	have sudo || die "run as root, or install sudo"
	SUDO=sudo
fi

# --- download and verify -----------------------------------------------------------------------
fetch "$BASE/SHA256SUMS" "$TMP/SHA256SUMS" ||
	die "could not download $BASE/SHA256SUMS (is the release there, and the repository public?)"
# (sed, not awk: minimal images lack awk; -e, because the pattern starts with "-" for some packages)
FILE=$(sed -e 's/^[^ ]* *//' -e 's/^\*//' "$TMP/SHA256SUMS" | grep -E -e "$PATTERN" | head -n 1 || true)
[ -n "$FILE" ] || die "release $TAG has no package matching $PATTERN"

case $KIND in
deb) INSTALL="$SUDO apt-get install -y ./$FILE" ;;
rpm) INSTALL="$SUDO dnf install -y ./$FILE" ;;
rpm-suse) INSTALL="$SUDO zypper --non-interactive install --allow-unsigned-rpm ./$FILE" ;;
arch) INSTALL="$SUDO pacman -U --noconfirm ./$FILE" ;;
tarball) INSTALL="tar xzf $FILE && cd lan-share-*/ && $SUDO ./packaging/install.sh install" ;;
esac

if [ "$DRY_RUN" -eq 1 ]; then
	say "Dry run: would download $BASE/$FILE"
	say "Dry run: would verify it against SHA256SUMS, then run (in a temporary directory):"
	printf '      %s\n' "$INSTALL"
	exit 0
fi

say "Downloading $FILE"
fetch "$BASE/$FILE" "$TMP/$FILE" || die "could not download $BASE/$FILE"
say "Verifying the SHA-256 checksum"
(cd "$TMP" && grep -F "  $FILE" SHA256SUMS | sha256sum -c -) || die "checksum mismatch: refusing to install $FILE"

say "Installing (you may be asked for your password)"
(cd "$TMP" && eval "$INSTALL")

have lan-share || die "installed, but 'lan-share' is not on your PATH"
VER=$(lan-share --version 2>&1) || die "installed, but 'lan-share --version' failed: $VER"
say "Installed: $VER"
cat <<'EOF'

Next:
  lan-share setup                 preview everything setup would change (changes nothing)
  sudo lan-share setup --apply    apply it
  lan-share gui                   open the graphical app (needs GTK 4 + libadwaita 1.5+)
  lan-share doctor                diagnose problems
EOF
