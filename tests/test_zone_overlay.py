"""Tests for the zone overlay drawing helpers."""

from __future__ import annotations

import io

from PIL import Image

from app.services.zone import draw_zone_overlay, normalised_to_pixel


def _solid_jpeg(
    width: int = 320,
    height: int = 240,
    colour: tuple[int, int, int] = (40, 40, 40),
) -> bytes:
    img = Image.new("RGB", (width, height), colour)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_normalised_to_pixel_rounds():
    pts = normalised_to_pixel([(0.0, 0.0), (1.0, 1.0), (0.5, 0.25)], 100, 200)
    assert pts == [(0, 0), (100, 200), (50, 50)]


def test_draw_zone_overlay_returns_valid_jpeg():
    src = _solid_jpeg()
    out = draw_zone_overlay(
        src,
        [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)],
        label="Restricted",
    )
    # Should be a valid JPEG that Pillow can re-open.
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG"
    assert img.size == (320, 240)


def test_draw_zone_overlay_paints_red_pixels():
    """The overlay should noticeably change pixels inside the polygon."""
    src = _solid_jpeg(colour=(40, 40, 40))
    out = draw_zone_overlay(src, [(0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)])
    img = Image.open(io.BytesIO(out)).convert("RGB")
    # Sample a pixel near the centre — should be redder than the original.
    r, g, b = img.getpixel((160, 120))
    assert r > 60
    assert r > g and r > b
