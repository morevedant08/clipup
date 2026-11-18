"""Verify Alembic initial migration and ORM model relationships."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Campaign,
    CampaignSource,
    Clip,
    Job,
    JobStatus,
    Permission,
    RightsBasis,
    User,
    YouTubeChannel,
)


def test_alembic_upgrade_downgrade_and_orm_roundtrip(
    tmp_path: Path, monkeypatch
) -> None:
    db_file = tmp_path / "migration_test.db"
    db_url = f"sqlite:///{db_file}"
    monkeypatch.setenv("DATABASE_URL", db_url)

    alembic_ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    alembic_cfg = Config(str(alembic_ini))
    alembic_cfg.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
    )

    command.upgrade(alembic_cfg, "head")
    engine = create_engine(db_url)
    tables = set(inspect(engine).get_table_names())
    expected_tables = {
        "users",
        "youtube_channels",
        "campaigns",
        "campaign_sources",
        "permissions",
        "jobs",
        "clips",
        "uploads",
        "audit_log",
        "alembic_version",
    }
    assert expected_tables.issubset(tables)

    # Verify downgrade and re-upgrade succeed cleanly
    command.downgrade(alembic_cfg, "base")
    tables_after_down = set(inspect(engine).get_table_names())
    assert "jobs" not in tables_after_down

    command.upgrade(alembic_cfg, "head")

    with Session(engine) as session:
        user = User(email="creator@example.com", name="Creator")
        session.add(user)
        session.flush()

        channel = YouTubeChannel(
            user_id=user.id,
            channel_id="UC_TEST_123",
            title="Creator Channel",
            encrypted_refresh_token="gAAAAAB_encrypted_placeholder",
            scopes="https://www.googleapis.com/auth/youtube.readonly",
        )
        campaign = Campaign(
            owner_user_id=user.id,
            name="Launch Campaign",
            rules_text="Include campaign hashtag.",
        )
        session.add_all([channel, campaign])
        session.flush()

        source = CampaignSource(
            campaign_id=campaign.id,
            source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            normalized_video_id="dQw4w9WgXcQ",
        )
        perm = Permission(
            user_id=user.id,
            channel_id="UC_OTHER_999",
            document_path="/storage/permissions/perm.pdf",
            document_sha256="a" * 64,
            original_filename="perm.pdf",
        )
        job = Job(
            user_id=user.id,
            source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            youtube_video_id="dQw4w9WgXcQ",
            campaign_id=campaign.id,
            rights_basis=RightsBasis.CAMPAIGN.value,
            rights_evidence_json={"basis": "campaign", "matched_source": source.source_url},
            settings_json={"clip_count": 3},
            status=JobStatus.QUEUED.value,
        )
        session.add_all([source, perm, job])
        session.flush()

        clip = Clip(
            job_id=job.id,
            file_path="clips/clip_1.mp4",
            start_sec=0.0,
            end_sec=30.0,
            score=0.9,
        )
        audit = AuditLog(
            user_id=user.id,
            job_id=job.id,
            action="rights.checked",
            details_json={"allowed": True, "basis": "campaign"},
        )
        session.add_all([clip, audit])
        session.commit()

        reloaded_job = session.get(Job, job.id)
        assert reloaded_job is not None
        assert len(reloaded_job.clips) == 1
        assert reloaded_job.campaign is not None
        assert reloaded_job.campaign.name == "Launch Campaign"

