#!/usr/bin/env bash
# Probe the Meraki external RTSP streams from a device on the camera VLAN.
#
# Run this on the Perth relay/laptop, not on the Melbourne VM. Meraki RTSP is
# LAN-only, unencrypted, TCP :9000, one stream per camera.
set -uo pipefail

CAMERAS=(
  "Q2GV-MGCS-QRM5 192.168.6.58"
  "Q2GV-52D5-YTLK 192.168.6.40"
  "Q2GV-XPS8-82TX 192.168.6.59"
)

if ! command -v ffprobe >/dev/null 2>&1; then
  echo "ERROR: ffprobe is not installed" >&2
  exit 1
fi

fail=0
for entry in "${CAMERAS[@]}"; do
  read -r serial ip <<<"$entry"
  echo "== $serial ($ip) =="
  if timeout 10 ffprobe -v error -rtsp_transport tcp \
      -select_streams v:0 \
      -show_entries stream=codec_name,width,height,avg_frame_rate \
      -of default=nw=1 "rtsp://${ip}:9000/live"; then
    echo "   OK"
  else
    echo "   FAILED (check VLAN, camera RTSP setting, and TCP :9000)"
    fail=1
  fi
done

exit "$fail"
