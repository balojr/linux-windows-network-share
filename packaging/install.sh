#!/bin/sh
# Single install path for every package format and for manual installs. POSIX sh, no make needed.
#   sudo ./packaging/install.sh install                 -> /usr/bin/lan-share, /usr/lib/lan-share/lan_share
#   PREFIX=/usr/local ./packaging/install.sh install     -> under /usr/local
#   DESTDIR=/tmp/stage ./packaging/install.sh install    -> staging root for packagers
#   sudo ./packaging/install.sh uninstall
set -eu
cd "$(dirname "$0")/.."
NAME=lan-share
PREFIX=${PREFIX:-/usr}
LIBDIR=${LIBDIR:-$PREFIX/lib/$NAME}
DOCDIR=${DOCDIR:-$PREFIX/share/doc/$NAME}
DESTDIR=${DESTDIR:-}
# polkit only reads /usr/share/polkit-1/actions, whatever PREFIX is
POLKITDIR=${POLKITDIR:-/usr/share/polkit-1/actions}
HELPER=$LIBDIR/lan-share-helper

case "${1:-}" in
install)
	install -d "$DESTDIR$LIBDIR" "$DESTDIR$PREFIX/bin" "$DESTDIR$DOCDIR"
	cp -r src/lan_share "$DESTDIR$LIBDIR/"
	find "$DESTDIR$LIBDIR" -name __pycache__ -type d -prune -exec rm -rf {} +
	find "$DESTDIR$LIBDIR" -type d -exec chmod 755 {} +
	find "$DESTDIR$LIBDIR" -type f -exec chmod 644 {} +
	sed "s|@LIBDIR@|$LIBDIR|" packaging/lan-share-helper.in > "$DESTDIR$HELPER"
	chmod 755 "$DESTDIR$HELPER"
	install -d "$DESTDIR$POLKITDIR"
	sed "s|@HELPER@|$HELPER|" packaging/io.github.lan_share.policy.in > "$DESTDIR$POLKITDIR/io.github.lan_share.policy"
	chmod 644 "$DESTDIR$POLKITDIR/io.github.lan_share.policy"
	sed "s|@LIBDIR@|$LIBDIR|" packaging/lan-share.in > "$DESTDIR$PREFIX/bin/$NAME"
	chmod 755 "$DESTDIR$PREFIX/bin/$NAME"
	install -m 644 README.md LICENSE "$DESTDIR$DOCDIR/"
	install -d "$DESTDIR$PREFIX/share/applications"
	install -m 644 packaging/io.github.lan_share.desktop "$DESTDIR$PREFIX/share/applications/"
	echo "installed $NAME to $DESTDIR$PREFIX (run: $NAME detect)"
	;;
uninstall)
	rm -rf "$DESTDIR$LIBDIR" "$DESTDIR$DOCDIR" "$DESTDIR$PREFIX/bin/$NAME" "$DESTDIR$PREFIX/share/applications/io.github.lan_share.desktop" "$DESTDIR$POLKITDIR/io.github.lan_share.policy"
	echo "removed $NAME from $DESTDIR$PREFIX (system configuration made by 'setup' is left in place)"
	;;
*)
	echo "usage: $0 install|uninstall" >&2
	exit 2
	;;
esac
