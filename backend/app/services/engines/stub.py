"""StubEngine: cuts N evenly spaced vertical 9:16 clips using ffmpeg."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from app.services.engines.base import ClipEngine, ClipEngineError, ClipResult, ClipSettings
from app.services.metadata import build_clip_title

logger = logging.getLogger(__name__)


def probe_video_duration(source_path: Path) -> float:
    """Return video duration in seconds using ffprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(source_path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        raise ClipEngineError(
            f"ffprobe failed for '{source_path.name}': {exc}"
        ) from exc

    raw = proc.stdout.strip()
    try:
        duration = float(raw)
    except ValueError as exc:
        raise ClipEngineError(f"Could not parse ffprobe duration: '{raw}'") from exc
    if duration <= 0:
        raise ClipEngineError("Source video has zero or negative duration.")
    return duration


def compute_even_segments(
    total_duration: float,
    clip_count: int,
    clip_duration: float,
) -> list[tuple[float, float]]:
    """Compute N evenly spaced (start_sec, end_sec) segments within total_duration."""
    if total_duration <= 0:
        raise ClipEngineError("Source video duration must be positive.")

    count = max(1, clip_count)
    effective_len = min(float(clip_duration), total_duration)

    if count == 1:
        start = max(0.0, (total_duration - effective_len) / 2.0)
        return [(round(start, 2), round(min(total_duration, start + effective_len), 2))]

    # If requested clips exceed total length, shrink per-clip duration to non-overlapping slices
    if count * effective_len > total_duration:
        slice_len = total_duration / count
        return [
            (round(idx * slice_len, 2), round((idx + 1) * slice_len, 2))
            for idx in range(count)
        ]

    # Spread N windows evenly across [0, total_duration - effective_len]
    max_start = total_duration - effective_len
    step = max_start / (count - 1)
    return [
        (round(idx * step, 2), round(min(total_duration, idx * step + effective_len), 2))
        for idx in range(count)
    ]


class StubEngine(ClipEngine):
    """Deterministic clipping engine that cuts N evenly spaced 9:16 vertical clips via ffmpeg."""

    name: str = "stub"

    def create_clips(
        self,
        source_path: Path,
        settings: ClipSettings,
    ) -> list[ClipResult]:
        """Cut evenly spaced clips and crop/scale each to 1080x1920 (9:16) MP4."""
        if not source_path.is_file():
            raise ClipEngineError(f"Source video file does not exist: {source_path}")

        settings.output_dir.mkdir(parents=True, exist_ok=True)
        total_duration = probe_video_duration(source_path)
        segments = compute_even_segments(
            total_duration=total_duration,
            clip_count=settings.clip_count,
            clip_duration=float(settings.clip_duration_sec),
        )

        # Center-crop & scale to 9:16 vertical (1080x1920)
        vf_filter = (
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,setsar=1"
        )

        results: list[ClipResult] = []
        total = len(segments)
        for idx, (start_sec, end_sec) in enumerate(segments, start=1):
            clip_duration = max(0.5, end_sec - start_sec)
            output_file = settings.output_dir / f"clip_{idx:02d}.mp4"
            cmd = [
                "ffmpeg",
                "-y",
                "-ss",
                f"{start_sec:.2f}",
                "-i",
                str(source_path),
                "-t",
                f"{clip_duration:.2f}",
                "-vf",
                vf_filter,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                str(output_file),
            ]
            logger.info(
                "StubEngine rendering clip %s/%s (%.2fs -> %.2fs)",
                idx,
                total,
                start_sec,
                end_sec,
            )
            try:
                subprocess.run(
                    cmd,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
            except (subprocess.SubprocessError, FileNotFoundError) as exc:
                stderr_hint = (
                    exc.stderr[-400:]
                    if isinstance(exc, subprocess.CalledProcessError) and exc.stderr
                    else str(exc)
                )
                raise ClipEngineError(
                    f"ffmpeg failed rendering clip {idx}: {stderr_hint}"
                ) from exc

            # Placeholder score distributed neatly across clips
            score = round(max(0.5, 0.92 - (idx - 1) * 0.05), 2)
            results.append(
                ClipResult(
                    file_path=output_file,
                    start_sec=start_sec,
                    end_sec=end_sec,
                    score=score,
                    title=build_clip_title(settings.source_title, idx, total),
                    reason=f"Evenly spaced segment {idx} of {total}",
                    transcript_excerpt=None,
                    metadata_json={"scorer": "stub"},
                )
            )

        return results
