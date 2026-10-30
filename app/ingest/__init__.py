"""Source-agnostic video ingest.

Public surface::

    from app.ingest import Frame, FrameSource, ingest_manager
"""

from __future__ import annotations

from app.ingest.base import Frame, FrameRing, FrameSource, JpegStreamParser, RateGate
from app.ingest.file_source import FileSource
from app.ingest.manager import IngestManager, build_source, ingest_manager
from app.ingest.rtsp_source import RtspSource
from app.ingest.synthetic_source import DEFAULT_SCRIPT, Phase, SyntheticSource

__all__ = [
    "DEFAULT_SCRIPT",
    "FileSource",
    "Frame",
    "FrameRing",
    "FrameSource",
    "IngestManager",
    "JpegStreamParser",
    "Phase",
    "RateGate",
    "RtspSource",
    "SyntheticSource",
    "build_source",
    "ingest_manager",
]
