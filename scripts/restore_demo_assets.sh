#!/usr/bin/env bash
# Restore a backup made by backup_demo_assets.sh into the repo and data directory.
# Usage: scripts/restore_demo_assets.sh BACKUP.tgz
# Overrides: REPO_DIR (default: this repo), DATA_DIR (default /models/danger_zone/data)
set -euo pipefail

ARCHIVE="${1:?usage: restore_demo_assets.sh BACKUP.tgz}"
REPO="${REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
DATA="${DATA_DIR:-/models/danger_zone/data}"
[ -f "$ARCHIVE" ] || { echo "no such file: $ARCHIVE" >&2; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
tar xzf "$ARCHIVE" -C "$tmp"
cat "$tmp/MANIFEST.txt" | head -6 >&2

mkdir -p "$REPO" "$DATA"
cp -a "$tmp/repo/." "$REPO/"
[ -d "$tmp/data" ] && cp -a "$tmp/data/." "$DATA/"

# verify what we restored against the manifest
fail=0
while read -r sum path; do
  case "$path" in
    repo/*) target="$REPO/${path#repo/}" ;;
    data/*) target="$DATA/${path#data/}" ;;
    *) continue ;;
  esac
  if [ "$(sha256sum "$target" 2>/dev/null | cut -d' ' -f1)" != "$sum" ]; then
    echo "MISMATCH: $target" >&2; fail=1
  fi
done < <(sed -n '/^sha256 of every file:/,$p' "$tmp/MANIFEST.txt" | tail -n +2)
[ "$fail" = 0 ] && echo "restored and verified into $REPO and $DATA" >&2
exit "$fail"
