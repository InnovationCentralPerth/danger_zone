#!/usr/bin/env bash
# Join every numbered clip for a camera into one looping replay file.
#
# Usage: scripts/build_demo_reel.sh [name] [crop]
#   name  base name in data/demo (default: demo-area) -> demo-area-1.mp4, -2, ...
#   crop  ffmpeg crop filter (default: crop=1256:1080:0:0, removes the dark band
#         on the right of Meraki MV12W dashboard exports)
#
# Clips are re-encoded so mixed sources join cleanly. Originals are untouched.
set -euo pipefail

DIR="${DEMO_DIR:-/models/danger_zone/data/demo}"
NAME="${1:-demo-area}"
CROP="${2:-crop=1256:1080:0:0}"

mapfile -t CLIPS < <(ls "$DIR"/"$NAME"-[0-9]*.mp4 2>/dev/null | sort -V)
if [ "${#CLIPS[@]}" -eq 0 ]; then
  echo "No clips matching $DIR/$NAME-N.mp4" >&2
  exit 1
fi

LIST="$(mktemp)"
trap 'rm -f "$LIST"' EXIT
for c in "${CLIPS[@]}"; do printf "file '%s'\n" "$c" >>"$LIST"; done

OUT="$DIR/$NAME.mp4"
TMP="$DIR/.$NAME.building.mp4"
ffmpeg -v error -y -f concat -safe 0 -i "$LIST" \
  -vf "$CROP,fps=10" -an -c:v libx264 -preset veryfast -crf 20 \
  -pix_fmt yuv420p -movflags +faststart "$TMP"
mv "$TMP" "$OUT"

echo "Built $OUT from ${#CLIPS[@]} clip(s):"
printf '  %s\n' "${CLIPS[@]##*/}"
ffprobe -v error -show_entries format=duration:stream=width,height -of default=nw=1 "$OUT"
echo "Restart danger-zone to reload: sudo systemctl restart danger-zone.service"
