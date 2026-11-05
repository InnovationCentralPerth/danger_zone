#!/usr/bin/env bash
# Verify the active demo profile's clips exist and are readable by ffmpeg.
#
# Usage:
#   DEMO_PROFILE=replay ./scripts/check_demo_assets.sh
#   ./scripts/check_demo_assets.sh config/cameras.replay.yaml
set -euo pipefail

cd "$(dirname "$0")/.."

profile="${DEMO_PROFILE:-replay}"
config="${1:-config/cameras.${profile}.yaml}"

if [[ ! -f "$config" ]]; then
  echo "ERROR: config not found: $config" >&2
  echo "Copy config/cameras.replay.example.yaml to $config first." >&2
  exit 1
fi

if ! command -v ffprobe >/dev/null 2>&1; then
  echo "ERROR: ffprobe is not installed" >&2
  exit 1
fi

# Prefer the repo venv (it has PyYAML); fall back to python3 on PATH.
PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if [[ -x .venv/bin/python ]]; then
    PYTHON=.venv/bin/python
  else
    PYTHON=python3
  fi
fi

echo "Checking $config"
"$PYTHON" - "$config" <<'PY'
import subprocess
import sys

import yaml

config = sys.argv[1]
with open(config) as f:
    cameras = (yaml.safe_load(f) or {}).get("cameras", [])

missing = 0
for cam in cameras:
    src = cam.get("source") or {}
    if src.get("type") != "file":
        print(f"  SKIP  {cam.get('serial')}: source={src.get('type')!r}")
        continue
    uri = src.get("uri")
    if not uri:
        print(f"  FAIL  {cam.get('serial')}: file source has no uri")
        missing += 1
        continue
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,width,height,avg_frame_rate",
                "-of",
                "default=nw=1",
                uri,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        print(f"  FAIL  {cam.get('serial')}: ffprobe timed out for {uri}")
        missing += 1
        continue
    if out.returncode != 0:
        print(f"  FAIL  {cam.get('serial')}: {out.stderr.strip() or 'ffprobe failed'}")
        missing += 1
        continue
    meta = " ".join(line for line in out.stdout.splitlines() if line)
    print(f"  OK    {cam.get('serial')}: {uri} ({meta})")

if missing:
    print(f"\n{missing} asset(s) missing or unreadable.")
    sys.exit(1)
print("\nAll file sources look good.")
PY
