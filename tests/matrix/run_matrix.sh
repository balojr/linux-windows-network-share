#!/usr/bin/env bash
# Usage: tests/matrix/run_matrix.sh [image ...]   (default: the whole matrix, in parallel)
# Each image gets the project mounted read-only; nothing runs privileged.
set -u
cd "$(dirname "$0")/../.."
OUT=${MATRIX_OUT:-/tmp/lan-share-matrix}; mkdir -p "$OUT"
IMAGES=("$@"); [ ${#IMAGES[@]} -eq 0 ] && IMAGES=(debian:stable-slim ubuntu:24.04 ubuntu:22.04 fedora:latest rockylinux:9 archlinux:latest opensuse/leap:latest opensuse/tumbleweed:latest)

bootstrap() { case "$1" in
  debian*|ubuntu*) echo "printf '#!/bin/sh\nexit 101\n' >/usr/sbin/policy-rc.d; chmod +x /usr/sbin/policy-rc.d; apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 >/dev/null" ;;
  fedora*|rocky*|alma*) echo "command -v python3 >/dev/null || dnf install -y -q python3" ;;
  archlinux*) echo "pacman -Sy --noconfirm python >/dev/null" ;;
  opensuse*) echo "zypper --non-interactive refresh >/dev/null && zypper --non-interactive install python3 >/dev/null" ;;
esac; }

for img in "${IMAGES[@]}"; do
  log="$OUT/$(echo "$img" | tr '/:' '__').log"
  ( docker run --rm --security-opt label=disable -v "$PWD":/src:ro -e PYTHONPATH=/src/src -e PYTHONDONTWRITEBYTECODE=1 \
      "$img" sh -c "$(bootstrap "$img") && python3 /src/tests/matrix/verify_in_container.py" >"$log" 2>&1
    echo "EXIT $?" >>"$log" ) &
done
wait
for img in "${IMAGES[@]}"; do
  log="$OUT/$(echo "$img" | tr '/:' '__').log"
  printf '%-28s %s\n' "$img" "$(grep -c '^PASS' "$log") pass, $(grep -c '^FAIL' "$log") fail, $(tail -1 "$log")"
done
