#!/usr/bin/env bash
# Join every numbered clip for a camera into one looping replay file.
#
# Usage: scripts/build_demo_reel.sh [name] [crop]
#   name  base name in data/demo (default: demo-area) -> demo-area-1.mp4, -2, ...
#   crop  ffmpeg crop filter (default: crop=1256:1080:0:0, removes the dark band
#         on the right of Meraki MV12W Demo Area dashboard exports; use
#         crop=1920:1080:0:0 for cameras without one)
#
# Optional trimming: data/demo/trims.txt, one segment per line:
#     coffee-1.mp4 0 25
#     coffee-1.mp4 100 208
# A clip with trim lines contributes only those segments (seconds); a clip with
# none is used whole. Use it to cut empty stretches out of the loop.
#
# Sources wider than 1920 px (e.g. 4K phone video) are downscaled to 1920 and 10 fps.
# Clips are re-encoded so mixed sources join cleanly, with a keyframe every second
# so the dashboard's scrub bar can seek quickly. Originals are untouched.
set -euo pipefail

DIR="${DEMO_DIR:-/models/danger_zone/data/demo}"
NAME="${1:-demo-area}"
CROP="${2:-crop=1256:1080:0:0}"
TRIMS="$DIR/trims.txt"

mapfile -t CLIPS < <(ls "$DIR"/"$NAME"-[0-9]*.mp4 2>/dev/null | sort -V)
if [ "${#CLIPS[@]}" -eq 0 ]; then
  echo "No clips matching $DIR/$NAME-N.mp4" >&2
  exit 1
fi

LIST="$(mktemp)"
trap 'rm -f "$LIST"' EXIT
for c in "${CLIPS[@]}"; do
  base="$(basename "$c")"
  segs=""
  if [ -f "$TRIMS" ]; then
    segs="$(awk -v f="$base" '$1==f && NF>=3 && $1!~/^#/ {print $2" "$3}' "$TRIMS")"
  fi
  if [ -n "$segs" ]; then
    while read -r start end; do
      printf "file '%s'\ninpoint %s\noutpoint %s\n" "$c" "$start" "$end" >>"$LIST"
    done <<<"$segs"
  else
    printf "file '%s'\n" "$c" >>"$LIST"
  fi
done

OUT="$DIR/$NAME.mp4"
TMP="$DIR/.$NAME.building.mp4"
ffmpeg -v error -y -f concat -safe 0 -i "$LIST" \
  -vf "$CROP,scale='min(iw,1920)':-2,fps=10" -an -c:v libx264 -preset veryfast -crf 20 \
  -g 10 -keyint_min 10 -sc_threshold 0 \
  -pix_fmt yuv420p -movflags +faststart "$TMP"
mv "$TMP" "$OUT"

echo "Built $OUT from ${#CLIPS[@]} clip(s):"
printf '  %s\n' "${CLIPS[@]##*/}"
ffprobe -v error -show_entries format=duration:stream=width,height -of default=nw=1 "$OUT"
echo "Restart danger-zone to reload: sudo systemctl restart danger-zone.service"
