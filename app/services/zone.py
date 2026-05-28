"""Geometry + image annotation helpers for camera zones.

Polygons in config/cameras.yaml are stored as normalised (0..1) (x, y)
coordinates relative to the snapshot. This module converts them to pixel
coords and draws a visible overlay on a snapshot so the VLM can clearly
'see' the zone we want it to reason about.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

Polygon = list[tuple[float, float]]


def normalised_to_pixel(polygon: Polygon, width: int, height: int) -> list[tuple[int, int]]:
    """Convert normalised (0..1) polygon coords to pixel coords for an image."""
    return [(int(round(x * width)), int(round(y * height))) for x, y in polygon]


def draw_zone_overlay(
    image_bytes: bytes,
    polygon: Polygon,
    *,
    label: str = "",
    outline: tuple[int, int, int, int] = (255, 60, 60, 220),
    fill: tuple[int, int, int, int] = (255, 60, 60, 60),
    line_width: int = 4,
) -> bytes:
    """Return JPEG bytes of the snapshot with the polygon outlined and tinted."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    pts = normalised_to_pixel(polygon, img.width, img.height)
    if len(pts) >= 3:
        draw.polygon(pts, outline=outline, fill=fill, width=line_width)
    elif len(pts) == 2:
        draw.rectangle(pts, outline=outline, fill=fill, width=line_width)

    if label:
        # Anchor label near the polygon's top-left corner.
        x = min(p[0] for p in pts)
        y = max(0, min(p[1] for p in pts) - 22)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
        except OSError:
            font = ImageFont.load_default()
        draw.rectangle((x - 2, y - 2, x + 8 * len(label) + 8, y + 20), fill=(255, 60, 60, 220))
        draw.text((x + 4, y), label, fill=(255, 255, 255, 255), font=font)

    combined = Image.alpha_composite(img, overlay).convert("RGB")
    out = io.BytesIO()
    combined.save(out, format="JPEG", quality=88)
    return out.getvalue()


def annotate_snapshot_file(
    src: str | Path,
    dest: str | Path,
    polygon: Polygon,
    label: str = "",
) -> Path:
    """Convenience wrapper: read src, draw overlay, write to dest."""
    src_path = Path(src)
    dest_path = Path(dest)
    data = draw_zone_overlay(src_path.read_bytes(), polygon, label=label)
    dest_path.write_bytes(data)
    return dest_path
