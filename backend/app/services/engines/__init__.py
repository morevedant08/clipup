"""Swappable video clipping engines."""

from app.services.engines.base import (
    ClipEngine,
    ClipEngineError,
    ClipResult,
    ClipSettings,
    get_clip_engine,
)
from app.services.engines.local import LocalEngine
from app.services.engines.stub import StubEngine

__all__ = [
    "ClipEngine",
    "ClipEngineError",
    "ClipResult",
    "ClipSettings",
    "LocalEngine",
    "StubEngine",
    "get_clip_engine",
]
