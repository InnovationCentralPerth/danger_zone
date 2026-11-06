"""Remember where in a replay each persisted snapshot came from.

The ingest manager registers ``snapshot path -> replay position`` when it saves
a frame; ``create_incident`` looks the path up so the incident can say "this was
at 2:19 of the reel" without every agent having to carry the number around.
Bounded, in-memory, and only meaningful for seekable (file) sources.
"""

from __future__ import annotations

from collections import OrderedDict

_MAX_ENTRIES = 1024
_positions: OrderedDict[str, float] = OrderedDict()


def register(path: str, position: float) -> None:
    _positions[str(path)] = float(position)
    _positions.move_to_end(str(path))
    while len(_positions) > _MAX_ENTRIES:
        _positions.popitem(last=False)


def lookup(path: str | None) -> float | None:
    if not path:
        return None
    return _positions.get(str(path))


def clear() -> None:
    _positions.clear()
