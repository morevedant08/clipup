"""Tests for StubEngine (real ffmpeg 9:16 clipping) and Celery worker pipeline."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditLog, Job, JobStatus, RightsBasis, User
from app.services.engines.base import ClipSettings, get_clip_engine
from app.services.engines.local import LocalEngine
from app.services.engines.stub import StubEngine, compute_even_segments
from app.services.rights import RightsGateError
from app.workers.tasks import execute_clipping_job


def _create_synthetic_landscape_video(target: Path, duration_sec: int = 6) -> Path:
    """Create a tiny real 1280x720 MP4 using ffmpeg for deterministic testing."""
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=duration={duration_sec}:size=640x360:rate=10",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={duration_sec}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(target),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    return target


def _probe_dimensions(video_path: Path) -> tuple[int, int]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            str(video_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    w_str, h_str = proc.stdout.strip().split("x")
    return int(w_str), int(h_str)


def test_compute_even_segments_handles_long_and_short_videos() -> None:
    segs = compute_even_segments(total_duration=120.0, clip_count=3, clip_duration=30.0)
    assert segs == [(0.0, 30.0), (45.0, 75.0), (90.0, 120.0)]

    short_segs = compute_even_segments(total_duration=6.0, clip_count=3, clip_duration=30.0)
    assert short_segs == [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)]


def test_local_engine_is_documented_todo() -> None:
    engine = get_clip_engine("local")
    assert isinstance(engine, LocalEngine)
    with pytest.raises(NotImplementedError):
        engine.create_clips(Path("dummy.mp4"), ClipSettings(output_dir=Path("out")))


def test_worker_blocks_rejected_job_before_downloading(
    db_session: Session,
    test_user: User,
    test_settings: Settings,
) -> None:
    rejected_job = Job(
        user_id=test_user.id,
        source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        youtube_video_id="dQw4w9WgXcQ",
        rights_basis=None,
        rights_evidence_json={"allowed": False},
        rejection_reason="No rights basis matched.",
        status=JobStatus.REJECTED.value,
    )
    db_session.add(rejected_job)
    db_session.commit()

    downloader_called = False

    def _should_never_run(job: Job, out_dir: Path) -> Path:
        nonlocal downloader_called
        downloader_called = True
        return out_dir / "never.mp4"

    with pytest.raises(RightsGateError):
        execute_clipping_job(db_session, rejected_job.id, downloader=_should_never_run)

    assert downloader_called is False
    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.job_id == rejected_job.id, AuditLog.action == "worker.rights_blocked")
        .first()
    )
    assert audit is not None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_worker_end_to_end_with_stub_engine_and_media_stream(
    client: TestClient,
    db_session: Session,
    test_user: User,
    auth_headers: dict[str, str],
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    sample_source = _create_synthetic_landscape_video(tmp_path / "source.mp4", duration_sec=6)

    attribution = (
        'Source: "Open Science" by Open Edu '
        "(https://www.youtube.com/watch?v=ccVideo9999) — "
        "Licensed under Creative Commons Attribution (CC BY)."
    )
    job = Job(
        user_id=test_user.id,
        source_url="https://www.youtube.com/watch?v=ccVideo9999",
        youtube_video_id="ccVideo9999",
        source_channel_id="UC_OPEN_EDU_00000000001",
        source_title="Open Science",
        source_duration_sec=6,
        rights_basis=RightsBasis.CC.value,
        rights_evidence_json={
            "allowed": True,
            "basis": "cc",
            "requires_attribution": True,
            "attribution_text": attribution,
        },
        settings_json={"clip_count": 3, "clip_duration_sec": 2, "aspect_ratio": "9:16"},
        status=JobStatus.QUEUED.value,
    )
    db_session.add(job)
    db_session.commit()

    def _fake_downloader(target_job: Job, download_dir: Path) -> Path:
        download_dir.mkdir(parents=True, exist_ok=True)
        dest = download_dir / f"{target_job.youtube_video_id}.mp4"
        shutil.copy2(sample_source, dest)
        return dest

    completed = execute_clipping_job(
        db_session,
        job.id,
        downloader=_fake_downloader,
        engine=StubEngine(),
    )

    assert completed.status == JobStatus.READY.value
    assert len(completed.clips) == 3

    for clip in completed.clips:
        abs_clip = test_settings.storage_path / clip.file_path
        assert abs_clip.is_file()
        width, height = _probe_dimensions(abs_clip)
        assert (width, height) == (1080, 1920)
        assert clip.attribution_text == attribution
        assert clip.review_status == "pending"

    # Verify audit trail entries were recorded
    actions = {
        row.action
        for row in db_session.query(AuditLog).filter(AuditLog.job_id == job.id).all()
    }
    assert {"job.processing", "job.downloaded", "job.ready"}.issubset(actions)

    # Verify API streams the real clip MP4
    first_clip = completed.clips[0]
    media_resp = client.get(
        f"/api/jobs/{job.id}/clips/{first_clip.id}/media",
        headers=auth_headers,
    )
    assert media_resp.status_code == 200
    assert media_resp.headers["content-type"] == "video/mp4"
    assert len(media_resp.content) > 1000

