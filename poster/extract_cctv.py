"""Pull the real camera frames used for the poster's "camera wall" (tile 1).

Reads the original Meraki exports under /models/danger_zone/data/demo and writes small
JPEGs to poster/assets/cctv/. Two moments (quiet, busy) from each of the three cameras.
"""

import subprocess
from pathlib import Path

from PIL import Image

DEMO = Path("/models/danger_zone/data/demo")
OUT = Path(__file__).resolve().parent / "assets" / "cctv"
CROP_DEMO = "crop=1256:1080:0:0,"  # Demo Area exports have a dark band on the right

# name, clip, seconds in, video filter prefix
FRAMES = [
    ("demo-quiet", "demo-area-1.mp4", 3, CROP_DEMO),
    ("workshop-quiet", "workshop-1.mp4", 6, ""),
    ("coffee-quiet", "coffee-1.mp4", 4, ""),
    ("demo-busy", "demo-area-2.mp4", 15, CROP_DEMO),
    ("workshop-busy", "workshop-1.mp4", 42, ""),
    ("coffee-busy", "coffee-1.mp4", 190, ""),
]

OUT.mkdir(parents=True, exist_ok=True)
for name, clip, ts, vf in FRAMES:
    tmp = OUT / f"{name}.tmp.jpg"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", str(ts), "-i", str(DEMO / clip), "-frames:v", "1",
         "-vf", f"{vf}scale=560:-2", "-q:v", "3", str(tmp)],
        check=True,
    )  # fmt: skip
    img = Image.open(tmp).convert("RGB")
    img.save(OUT / f"{name}.jpg", quality=72, optimize=True)
    tmp.unlink()
    print(name, img.size, (OUT / f"{name}.jpg").stat().st_size // 1024, "KB")
