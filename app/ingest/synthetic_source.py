"""Synthetic source — a scripted demo scene with no camera required.

Generates a workshop frame with (or without) a worker wearing a hard hat and
hi-vis vest. The default script cycles through *empty → compliant → violation*,
so a PPE demo always has something to detect at a predictable moment instead
of depending on someone walking past a real camera.

This is deliberately schematic. It exists to prove the pipeline end-to-end and
to keep the demo alive if the network or camera is unavailable; real footage
should go through :class:`~app.ingest.file_source.FileSource` or
:class:`~app.ingest.rtsp_source.RtspSource`.
"""

from __future__ import annotations

import asyncio
import io
import math
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from PIL import Image, ImageDraw

from app.ingest.base import Frame


@dataclass(frozen=True, slots=True)
class Phase:
    """One segment of the scripted timeline."""

    duration: float
    person: bool = False
    hard_hat: bool = False
    hi_vis: bool = False


DEFAULT_SCRIPT: tuple[Phase, ...] = (
    Phase(6.0),
    Phase(10.0, person=True, hard_hat=True, hi_vis=True),
    Phase(14.0, person=True, hard_hat=False, hi_vis=True),
    Phase(6.0),
)


def phases_from_config(raw: object) -> tuple[Phase, ...]:
    """Build a phase list from the ``source.script`` YAML value.

    Falls back to :data:`DEFAULT_SCRIPT` when the value is missing or empty.
    """
    if not isinstance(raw, list) or not raw:
        return DEFAULT_SCRIPT
    phases: list[Phase] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            duration = float(entry.get("duration", 0))
        except (TypeError, ValueError):
            continue
        if duration <= 0:
            continue
        phases.append(
            Phase(
                duration=duration,
                person=bool(entry.get("person", False)),
                hard_hat=bool(entry.get("hard_hat", False)),
                hi_vis=bool(entry.get("hi_vis", False)),
            )
        )
    return tuple(phases) or DEFAULT_SCRIPT


# ---- rendering -------------------------------------------------------------

_WALL = (44, 50, 60)
_WALL_PANEL = (52, 59, 70)
_FLOOR = (74, 77, 82)
_FLOOR_LINE = (62, 65, 70)
_SKIN = (224, 172, 140)
_HAIR = (48, 36, 30)
_SHIRT = (58, 92, 148)
_TROUSERS = (42, 47, 58)
_BOOT = (28, 28, 30)
_HELMET = (250, 205, 20)
_VEST = (255, 118, 18)
_VEST_STRIPE = (232, 232, 226)


def _draw_scene(draw: ImageDraw.ImageDraw, width: int, height: int, t: float) -> None:
    """Static workshop backdrop with a little parallax motion."""
    horizon = int(height * 0.62)
    draw.rectangle([0, 0, width, horizon], fill=_WALL)
    draw.rectangle([0, horizon, width, height], fill=_FLOOR)

    # Wall panels.
    panel_w = width // 6
    for i in range(6):
        x = i * panel_w
        draw.rectangle([x + 6, 20, x + panel_w - 6, horizon - 20], outline=_WALL_PANEL, width=3)

    # Floor perspective lines.
    for i in range(-4, 9):
        x_bottom = int(width * (0.5 + i * 0.16))
        x_horizon = int(width * 0.5 + (x_bottom - width * 0.5) * 0.15)
        draw.line([(x_horizon, horizon), (x_bottom, height)], fill=_FLOOR_LINE, width=2)

    # Crates on the left, a cone on the right.
    crate = int(height * 0.12)
    for i, (cx, cy) in enumerate(((0.08, 0.80), (0.16, 0.86), (0.10, 0.93))):
        x = int(width * cx)
        y = int(height * cy)
        shade = 118 + i * 10
        draw.rounded_rectangle(
            [x - crate, y - crate, x + crate, y + crate],
            radius=4,
            fill=(shade, shade - 8, shade - 20),
            outline=(70, 70, 72),
            width=3,
        )
    cone_x = int(width * 0.86)
    cone_y = int(height * 0.88)
    draw.polygon(
        [(cone_x, cone_y - crate), (cone_x - crate // 2, cone_y), (cone_x + crate // 2, cone_y)],
        fill=(240, 110, 20),
    )
    draw.rectangle(
        [cone_x - crate // 2 - 8, cone_y, cone_x + crate // 2 + 8, cone_y + 10],
        fill=(230, 230, 230),
    )


def _draw_person(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    phase: Phase,
    t: float,
) -> None:
    """Draw a schematic worker, optionally wearing a hard hat / hi-vis vest."""
    scale = height / 720.0
    feet_y = int(height * 0.90)
    sway = math.sin(t * 0.9) * 12 * scale
    bob = math.sin(t * 1.8) * 3 * scale
    cx = int(width * 0.55 + sway)
    feet_y += int(bob)

    head_r = int(38 * scale)
    head_cy = feet_y - int(372 * scale)
    shoulder_y = head_cy + head_r + int(18 * scale)
    hip_y = feet_y - int(170 * scale)
    torso_w = int(112 * scale)
    limb_w = int(30 * scale)

    # Ground shadow.
    draw.ellipse(
        [cx - 90 * scale, feet_y - 14 * scale, cx + 90 * scale, feet_y + 12 * scale],
        fill=(55, 57, 60),
    )

    # Legs and boots.
    for side in (-1, 1):
        leg_x = cx + side * int(24 * scale)
        draw.rounded_rectangle(
            [leg_x - limb_w // 2, hip_y, leg_x + limb_w // 2, feet_y - int(22 * scale)],
            radius=limb_w // 2,
            fill=_TROUSERS,
        )
        draw.rounded_rectangle(
            [leg_x - limb_w // 2 - 4, feet_y - int(26 * scale), leg_x + limb_w // 2 + 8, feet_y],
            radius=6,
            fill=_BOOT,
        )

    # Arms.
    for side in (-1, 1):
        arm_x = cx + side * int(58 * scale)
        draw.rounded_rectangle(
            [arm_x - limb_w // 2, shoulder_y, arm_x + limb_w // 2, hip_y + int(30 * scale)],
            radius=limb_w // 2,
            fill=_SHIRT,
        )
        draw.ellipse(
            [
                arm_x - limb_w // 2 - 2,
                hip_y + int(24 * scale),
                arm_x + limb_w // 2 + 2,
                hip_y + int(24 * scale) + limb_w,
            ],
            fill=_SKIN,
        )

    # Torso.
    draw.rounded_rectangle(
        [cx - torso_w // 2, shoulder_y, cx + torso_w // 2, hip_y + int(6 * scale)],
        radius=int(16 * scale),
        fill=_SHIRT,
    )

    # Hi-vis vest.
    if phase.hi_vis:
        draw.rounded_rectangle(
            [cx - torso_w // 2 + 6, shoulder_y + 4, cx + torso_w // 2 - 6, hip_y],
            radius=int(12 * scale),
            fill=_VEST,
        )
        for frac in (0.30, 0.62):
            y = shoulder_y + int((hip_y - shoulder_y) * frac)
            draw.rectangle(
                [cx - torso_w // 2 + 8, y, cx + torso_w // 2 - 8, y + int(10 * scale)],
                fill=_VEST_STRIPE,
            )

    # Neck + head.
    draw.rectangle(
        [cx - int(14 * scale), shoulder_y - int(14 * scale), cx + int(14 * scale), shoulder_y + 4],
        fill=_SKIN,
    )
    draw.ellipse(
        [cx - head_r, head_cy - head_r, cx + head_r, head_cy + head_r],
        fill=_SKIN,
    )

    # Face.
    eye_y = head_cy - int(6 * scale)
    for side in (-1, 1):
        ex = cx + side * int(14 * scale)
        draw.ellipse(
            [
                ex - int(4 * scale),
                eye_y - int(4 * scale),
                ex + int(4 * scale),
                eye_y + int(4 * scale),
            ],
            fill=(40, 40, 44),
        )
    draw.arc(
        [
            cx - int(16 * scale),
            head_cy + int(4 * scale),
            cx + int(16 * scale),
            head_cy + int(26 * scale),
        ],
        start=15,
        end=165,
        fill=(150, 90, 80),
        width=max(2, int(3 * scale)),
    )

    if phase.hard_hat:
        # Dome plus brim — deliberately unmistakable.
        draw.pieslice(
            [
                cx - head_r - int(8 * scale),
                head_cy - head_r - int(18 * scale),
                cx + head_r + int(8 * scale),
                head_cy + head_r - int(2 * scale),
            ],
            start=180,
            end=360,
            fill=_HELMET,
        )
        draw.rounded_rectangle(
            [
                cx - head_r - int(22 * scale),
                head_cy - head_r - int(2 * scale),
                cx + head_r + int(22 * scale),
                head_cy - head_r + int(14 * scale),
            ],
            radius=int(5 * scale),
            fill=_HELMET,
        )
    else:
        # Hair instead of a helmet.
        draw.pieslice(
            [cx - head_r, head_cy - head_r, cx + head_r, head_cy + head_r],
            start=180,
            end=360,
            fill=_HAIR,
        )


def render_frame(
    phase: Phase,
    t: float,
    width: int = 1280,
    height: int = 720,
    *,
    quality: int = 88,
) -> bytes:
    """Render one synthetic frame as JPEG bytes."""
    img = Image.new("RGB", (width, height), _WALL)
    draw = ImageDraw.Draw(img)
    _draw_scene(draw, width, height, t)
    if phase.person:
        _draw_person(draw, width, height, phase, t)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


# ---- source ----------------------------------------------------------------


class SyntheticSource:
    """A looped, scripted scene generated with Pillow."""

    def __init__(
        self,
        camera_id: str,
        *,
        fps: float = 5.0,
        width: int = 1280,
        height: int = 720,
        script: Sequence[Phase] | None = None,
    ) -> None:
        if fps <= 0:
            raise ValueError("fps must be > 0")
        self.camera_id = camera_id
        self.fps = fps
        self.width = width
        self.height = height
        self.script = tuple(script) if script else DEFAULT_SCRIPT
        self.cycle = sum(p.duration for p in self.script)
        self._closed = False

    def phase_at(self, t: float) -> Phase:
        """Return the phase active at elapsed time ``t`` (wraps)."""
        offset = t % self.cycle
        for phase in self.script:
            if offset < phase.duration:
                return phase
            offset -= phase.duration
        return self.script[-1]

    def stream(self) -> AsyncIterator[Frame]:
        return self._stream()

    async def aclose(self) -> None:
        self._closed = True

    async def _stream(self) -> AsyncIterator[Frame]:
        seq = 0
        started = time.monotonic()
        next_at = started
        interval = 1.0 / self.fps
        while not self._closed:
            now = time.monotonic()
            elapsed = now - started
            jpeg = render_frame(self.phase_at(elapsed), elapsed, self.width, self.height)
            yield Frame(
                camera_id=self.camera_id,
                seq=seq,
                source_ts=elapsed,
                ingest_ts=now,
                width=self.width,
                height=self.height,
                jpeg=jpeg,
            )
            seq += 1
            next_at += interval
            delay = next_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                next_at = time.monotonic()
