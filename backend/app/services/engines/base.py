"""Abstract ClipEngine interface and factory for swappable video clipping backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ClipSettings:
    """Normalized clipping configuration passed to any ClipEngine implementation."""

    output_dir: Path
    clip_count: int = 3
    clip_duration_sec: int = 30
    min_clip_duration_sec: int = 20
    max_clip_duration_sec: int = 60
    aspect_ratio: str = "9:16"
    blurred_background: bool = False
    source_title: str | None = None
    job_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ClipResult:
    """Single generated clip artifact returned by a ClipEngine."""

    file_path: Path
    start_sec: float
    end_sec: float
    score: float
    title: str | None = None
    reason: str | None = None
    transcript_excerpt: str | None = None
    metadata_json: dict[str, Any] = field(default_factory=dict)


class ClipEngineError(RuntimeError):
    """Raised when a clipping engine fails to process a source video."""


class ClipEngine(ABC):
    """Abstract base class for video clipping engines.

    Implementations (StubEngine, LocalEngine, or future third-party clipping APIs)
    must accept a local rights-verified source video path and return generated
    vertical clip results.
    """

    name: str = "base"

    @abstractmethod
    def create_clips(
        self,
        source_path: Path,
        settings: ClipSettings,
    ) -> list[ClipResult]:
        """Generate vertical short clips from a local source video file."""


def get_clip_engine(engine_name: str | None = None) -> ClipEngine:
    """Instantiate the configured ClipEngine by name (defaults to settings.clip_engine)."""
    from app.config import get_settings
    from app.services.engines.local import LocalEngine
    from app.services.engines.stub import StubEngine

    selected = (engine_name or get_settings().clip_engine or "stub").strip().lower()
    if selected == "stub":
        return StubEngine()
    if selected == "local":
        return LocalEngine()
    raise ValueError(
        f"Unsupported clip engine '{selected}'. Supported engines: stub, local."
    )

