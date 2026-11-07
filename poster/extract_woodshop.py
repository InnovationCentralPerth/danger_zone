"""Pull the four poster frames for the woodshop table-saw ladder (35% into each clip).

python poster/extract_woodshop.py
"""

import subprocess
from pathlib import Path

DEMO = Path("/models/danger_zone/data/demo")
OUT = Path(__file__).resolve().parent / "assets" / "frames"

for n in range(1, 5):
    clip = DEMO / f"woodshop-table-{n}.mp4"
    dur = float(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(clip),
            ]
        )
    )
    out = OUT / f"woodshop-{n}.jpg"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{dur * 0.35:.2f}", "-i", str(clip), "-frames:v", "1",
         "-vf", "scale=1100:-2", "-q:v", "3", str(out)],
        check=True,
    )  # fmt: skip
    print(out.name, out.stat().st_size // 1024, "KB")
