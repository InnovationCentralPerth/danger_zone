#!/usr/bin/env bash
# Back up everything the expo needs that is NOT in git:
#   - replay clips and the joined reels (data/demo, incl. trims.txt)
#   - camera / model config (config/cameras*.yaml, whitelist, models)
#   - .env and .meraki_key.env  (SECRETS - skip with --no-secrets)
#   - optionally the incident database (--with-db, ~160 MB)
# Incident snapshots (several GB) are deliberately left out.
#
# Usage:
#   scripts/backup_demo_assets.sh [OUTPUT|-] [--no-secrets] [--with-db]
#   OUTPUT defaults to '-' (stdout) so it can be streamed over ssh to a laptop:
#     ssh marrabld@xenterm-wl180 'danger_zone/scripts/backup_demo_assets.sh -' > backup.tgz
set -euo pipefail

REPO="${REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
DATA="${DATA_DIR:-/models/danger_zone/data}"
OUT="${1:--}"
[ $# -gt 0 ] && shift
secrets=1
db=0
for arg in "$@"; do
  case "$arg" in
    --no-secrets) secrets=0 ;;
    --with-db) db=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/repo/config" "$stage/data"

# config (skip the checked-in *.example.yaml files; they are in git)
for f in "$REPO"/config/*.yaml; do
  case "$f" in *.example.yaml) continue ;; esac
  [ -f "$f" ] && cp -p "$f" "$stage/repo/config/"
done
if [ "$secrets" = 1 ]; then
  for f in .env .meraki_key.env; do
    [ -f "$REPO/$f" ] && cp -p "$REPO/$f" "$stage/repo/$f"
  done
fi

# clips + reels: symlink into the stage and let tar follow it (no 200 MB copy)
if [ -d "$DATA/demo" ]; then
  ln -s "$DATA/demo" "$stage/data/demo"
else
  echo "warning: $DATA/demo not found, no clips in this backup" >&2
fi

# consistent copy of the live SQLite database (a plain cp can catch it mid-write)
if [ "$db" = 1 ] && [ -f "$DATA/app.db" ]; then
  python3 - "$DATA/app.db" "$stage/data/app.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PY
fi

commit="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
branch="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
{
  echo "Safety Sentry demo-asset backup"
  echo "created : $(date -u +%FT%TZ)  on $(hostname)"
  echo "git     : $branch @ $commit"
  echo "secrets : $([ "$secrets" = 1 ] && echo INCLUDED - keep this file private || echo excluded)"
  echo "database: $([ "$db" = 1 ] && echo included || echo not included)"
  echo
  echo "sha256 of every file:"
  (cd "$stage" && find -L repo data -type f -print0 | sort -z | xargs -0 sha256sum)
} > "$stage/MANIFEST.txt"

cat > "$stage/RESTORE.txt" <<'TXT'
Restore on the VM (or a rebuilt one) after `git clone` + `uv sync`:

  scripts/restore_demo_assets.sh /path/to/this-backup.tgz
  sudo systemctl restart danger-zone.service

Or by hand:
  mkdir /tmp/restore && tar xzf this-backup.tgz -C /tmp/restore
  cp -a /tmp/restore/repo/. /home/marrabld/danger_zone/
  mkdir -p /models/danger_zone/data && cp -a /tmp/restore/data/. /models/danger_zone/data/
TXT

if command -v pigz >/dev/null 2>&1; then gz=(pigz -1); else gz=(gzip -1); fi
if [ "$OUT" = "-" ]; then
  tar -C "$stage" -h -cf - repo data MANIFEST.txt RESTORE.txt | "${gz[@]}"
else
  tar -C "$stage" -h -cf - repo data MANIFEST.txt RESTORE.txt | "${gz[@]}" > "$OUT"
  echo "wrote $OUT ($(du -h "$OUT" | cut -f1))" >&2
  [ "$secrets" = 1 ] && echo "NOTE: this archive contains API keys - keep it private." >&2
fi
