#!/bin/sh
# Build what the website's launch command downloads: dist/watchtower.tar.gz + dist/version.
# Run from the repo root on a committed tree:  deploy/build.sh [OUT_DIR]
set -eu
OUT="${1:-dist}"
mkdir -p "$OUT"
V="$(git describe --tags --always --dirty 2>/dev/null || date +%Y%m%d%H%M)"
git archive --format=tar HEAD fms vision config/event.example.yaml config/vision.example.yaml \
  config/vision.mock.yaml launch.py requirements-run.txt requirements.txt LICENSE README.md \
  | gzip -9 > "$OUT/watchtower.tar.gz.tmp"
mv "$OUT/watchtower.tar.gz.tmp" "$OUT/watchtower.tar.gz"
echo "$V" > "$OUT/version"
echo "built $OUT/watchtower.tar.gz ($V)"
