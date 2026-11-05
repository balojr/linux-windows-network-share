#!/usr/bin/env bash
# Tests get.sh (the one-line installer) on every distro image against a local mock of GitHub's release URLs.
# Uses the artifacts in dist/ (run tests/matrix/test_packages.sh first). Pipes the script into sh, like the real one-liner.
set -u
cd "$(dirname "$0")/../.."
OUT=${ONELINER_OUT:-/tmp/lan-share-oneliner}; mkdir -p "$OUT"
PORT=${PORT:-8765}
TAG=v$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
D="docker run --rm --network host --security-opt label=disable -v $PWD/get.sh:/get.sh:ro -e LAN_SHARE_RELEASES_URL=http://127.0.0.1:$PORT"

serve() { python3 tests/matrix/fake_releases.py dist "$TAG" "$PORT" "$@" & SRV=$!; sleep 1; }
stop() { kill "$SRV" 2>/dev/null; wait "$SRV" 2>/dev/null; }

prep() { case "$1" in   # a real user already has curl; install it (and sudo) only where missing
  debian*|ubuntu*) echo "apt-get update -qq >/dev/null && apt-get install -y -qq curl ca-certificates sudo >/dev/null" ;;
  fedora*|rocky*)  echo "command -v curl >/dev/null || dnf install -y -q curl >/dev/null 2>&1; command -v sudo >/dev/null || dnf install -y -q sudo >/dev/null 2>&1" ;;
  archlinux*)      echo "pacman -Sy --noconfirm curl sudo >/dev/null 2>&1" ;;
  opensuse*)       echo "zypper --non-interactive refresh >/dev/null 2>&1; zypper --non-interactive install curl sudo >/dev/null 2>&1" ;;
esac; }

IMAGES=(debian:stable-slim debian:bookworm-slim ubuntu:24.04 ubuntu:22.04 fedora:latest rockylinux:9 archlinux:latest opensuse/leap:latest opensuse/tumbleweed:latest)
serve
echo "== happy path: cat get.sh | sh, on every distro"
pids=()
for img in "${IMAGES[@]}"; do
  ( log="$OUT/ok-$(echo "$img" | tr '/:' '__').log"
    $D "$img" sh -c "set -e; $(prep "$img"); cat /get.sh | sh; lan-share --version; lan-share detect | head -1; echo ONELINER-OK" >"$log" 2>&1
    printf '%-28s %s\n' "$img" "$(tail -1 "$log")" ) &
  pids+=($!)
done
wait "${pids[@]}"

echo "== dry run changes nothing"
$D ubuntu:24.04 sh -c "$(prep ubuntu:24.04); cat /get.sh | sh -s -- --dry-run >/tmp/o 2>&1; cat /tmp/o | sed 's/^/   /'; ! command -v lan-share >/dev/null && echo DRYRUN-OK" 2>&1 | tail -6

echo "== tarball path"
$D debian:stable-slim sh -c "$(prep debian)  ; cat /get.sh | sh -s -- --tarball 2>&1 | tail -3; lan-share --version && test -x /usr/lib/lan-share/lan-share-helper && echo TARBALL-OK" 2>&1 | tail -4

echo "== non-root user with sudo (script must use sudo only for the install)"
$D ubuntu:24.04 sh -c "$(prep ubuntu:24.04); useradd -m bob; echo 'bob ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/bob; su bob -c 'cat /get.sh | sh' 2>&1 | tail -3; su bob -c 'lan-share --version' && echo SUDO-OK" 2>&1 | tail -4

echo "== explicit version, and a version that does not exist"
$D fedora:latest sh -c "$(prep fedora); sh /get.sh --version ${TAG#v} >/dev/null 2>&1 && lan-share --version && echo VERSION-OK; sh /get.sh --version 9.9.9 >/tmp/o 2>&1; echo \"missing version exit=\$?\"; tail -1 /tmp/o" 2>&1 | tail -3
stop

echo "== tampered package must be refused"
serve --tamper "lan-share_${TAG#v}_all.deb"
$D ubuntu:24.04 sh -c "$(prep ubuntu:24.04); cat /get.sh | sh >/tmp/o 2>&1; echo \"exit=\$?\"; tail -2 /tmp/o; command -v lan-share >/dev/null && echo INSTALLED-ANYWAY || echo NOT-INSTALLED-OK" 2>&1 | tail -4
stop
