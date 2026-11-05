#!/bin/sh
# Create dist/lan-share-VERSION.tar.gz (the source tarball the rpm/PKGBUILD builds consume).
set -eu
cd "$(dirname "$0")/.."
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)
mkdir -p dist
tar --exclude=__pycache__ --exclude='*.pyc' --transform "s|^\.|lan-share-$VERSION|" \
    -czf "dist/lan-share-$VERSION.tar.gz" ./src ./packaging ./Makefile ./pyproject.toml ./README.md ./LICENSE ./tests
echo "dist/lan-share-$VERSION.tar.gz"
