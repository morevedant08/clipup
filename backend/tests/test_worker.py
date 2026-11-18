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
from app.services.youtube import (
    VideoDownloadError,
    _build_yt_dlp_command,
    _summarize_yt_dlp_error,
    download_rights_verified_video,
)
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


def test_build_yt_dlp_command_enables_deno_js_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/deno" if name == "deno" else None)
    cmd = _build_yt_dlp_command(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "/tmp/dQw4w9WgXcQ.%(ext)s",
    )
    assert "--js-runtimes" in cmd
    idx = cmd.index("--js-runtimes")
    assert cmd[idx + 1] == "deno:/usr/local/bin/deno"

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    fallback_cmd = _build_yt_dlp_command(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "/tmp/dQw4w9WgXcQ.%(ext)s",
    )
    assert fallback_cmd[fallback_cmd.index("--js-runtimes") + 1] == "deno"


def test_download_rights_verified_video_enforces_rights_gate_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    subprocess_called = False

    def _fail_if_called(*args, **kwargs):
        nonlocal subprocess_called
        subprocess_called = True
        raise AssertionError("subprocess.run must never execute before rights pass")

    monkeypatch.setattr(subprocess, "run", _fail_if_called)

    unapproved_job = Job(
        user_id=None,
        source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        youtube_video_id="dQw4w9WgXcQ",
        rights_basis=None,
        rights_evidence_json={"allowed": False},
        status=JobStatus.REJECTED.value,
    )
    with pytest.raises(RightsGateError):
        download_rights_verified_video(unapproved_job, tmp_path)
    assert subprocess_called is False


def test_failed_download_stores_short_ui_message_and_logs_full_stderr(
    db_session: Session,
    test_user: User,
    test_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    job = Job(
        user_id=test_user.id,
        source_url="https://www.youtube.com/watch?v=abc12345678",
        youtube_video_id="abc12345678",
        source_channel_id="UC_OWNED_11111111111111",
        source_title="Demo Video",
        rights_basis=RightsBasis.OWNER.value,
        rights_evidence_json={"allowed": True, "basis": "owner"},
        status=JobStatus.QUEUED.value,
    )
    db_session.add(job)
    db_session.commit()

    verbose_stderr = (
        "WARNING: [youtube] No supported JavaScript runtime could be found. "
        "Only deno is enabled by default; to use another runtime add --js-runtimes...\n"
        "ERROR: [youtube] abc12345678: This video is not available\n"
        "Traceback (most recent call last): internal stack trace line 1..."
    )

    def _failing_downloader(target_job: Job, out_dir: Path) -> Path:
        raise VideoDownloadError(
            _summarize_yt_dlp_error(verbose_stderr),
            detail=verbose_stderr,
        )

    with caplog.at_level("ERROR", logger="app.workers.tasks"):
        failed = execute_clipping_job(
            db_session,
            job.id,
            downloader=_failing_downloader,
        )

    assert failed.status == JobStatus.FAILED.value
    assert failed.error_message is not None
    assert "Deno" in failed.error_message
    assert "Traceback" not in failed.error_message
    assert "\n" not in failed.error_message
    assert len(failed.error_message) < 200
    assert "internal stack trace line 1" in caplog.text


def test_summarize_yt_dlp_error_detects_bot_check_and_rate_limit() -> None:
    bot_msg = _summarize_yt_dlp_error(
        "ERROR: [youtube] xyz: Sign in to confirm you're not a bot."
    )
    assert "sign-in" in bot_msg.lower() or "bot check" in bot_msg.lower()

    rate_msg = _summarize_yt_dlp_error("ERROR: HTTP Error 429: Too Many Requests")
    assert "rate-limit" in rate_msg.lower()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_worker_processes_uploaded_source_file_without_invoking_yt_dlp(
    db_session: Session,
    test_user: User,
    test_settings: Settings,
    tmp_path: Path,
) -> None:
    job = Job(
        user_id=test_user.id,
        source_url="https://www.youtube.com/watch?v=ownUpload12",
        youtube_video_id="ownUpload12",
        source_channel_id="UC_OWNED_11111111111111",
        source_title="Uploaded Source Demo",
        source_duration_sec=6,
        rights_basis=RightsBasis.OWNER.value,
        rights_evidence_json={"allowed": True, "basis": "owner"},
        settings_json={},
        status=JobStatus.QUEUED.value,
    )
    db_session.add(job)
    db_session.flush()

    upload_dir = test_settings.storage_path / "uploads" / str(job.id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    uploaded_file = _create_synthetic_landscape_video(upload_dir / "source.mp4", duration_sec=6)

    job.settings_json = {
        "clip_count": 2,
        "clip_duration_sec": 3,
        "aspect_ratio": "9:16",
        "source_mode": "uploaded_file",
        "uploaded_source_path": str(uploaded_file.relative_to(test_settings.storage_path)),
        "uploaded_source_filename": "demo.mp4",
    }
    db_session.commit()

    downloader_called = False

    def _downloader_must_not_run(target_job: Job, out_dir: Path) -> Path:
        nonlocal downloader_called
        downloader_called = True
        raise AssertionError("yt-dlp downloader must be skipped for uploaded source files")

    completed = execute_clipping_job(
        db_session,
        job.id,
        downloader=_downloader_must_not_run,
        engine=StubEngine(),
    )

    assert downloader_called is False
    assert completed.status == JobStatus.READY.value
    assert len(completed.clips) == 2
    for clip in completed.clips:
        abs_clip = test_settings.storage_path / clip.file_path
        assert abs_clip.is_file()
        assert _probe_dimensions(abs_clip) == (1080, 1920)

    actions = {
        row.action
        for row in db_session.query(AuditLog).filter(AuditLog.job_id == job.id).all()
    }
    assert {"job.processing", "job.source_ready", "job.ready"}.issubset(actions)
    assert "job.downloaded" not in actions

