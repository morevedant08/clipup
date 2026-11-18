"""LocalEngine (TODO): planned Whisper + LLM highlight clipping engine."""

from __future__ import annotations

from pathlib import Path

from app.services.engines.base import ClipEngine, ClipResult, ClipSettings


class LocalEngine(ClipEngine):
    """Planned local AI clipping engine (Whisper transcription + LLM highlight detection).

    TODO (Phase 2):
      1. Extract audio track with ffmpeg (16 kHz mono WAV).
      2. Transcribe with faster-whisper / Whisper to produce word-level timestamps.
      3. Prompt an LLM with transcript + optional campaign rules to select high-retention
         segments with scores, titles, and hook captions.
      4. Detect active speaker face bounding boxes for dynamic 9:16 vertical reframing.
      5. Render final clips via ffmpeg with burned-in word-highlighted captions.

    Keeping this behind the ClipEngine ABC allows swapping between StubEngine,
    LocalEngine, or any third-party clipping API without touching worker or API code.
    """

    name: str = "local"

    def create_clips(
        self,
        source_path: Path,
        settings: ClipSettings,
    ) -> list[ClipResult]:
        """Raise NotImplementedError until Whisper + LLM pipeline is implemented."""
        raise NotImplementedError(
            "LocalEngine (Whisper + LLM) is a documented TODO. "
            "Set CLIP_ENGINE=stub to use StubEngine."
        )

