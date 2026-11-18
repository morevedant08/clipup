"""Export all ORM models so Alembic metadata discovery sees every table."""

from app.models.audit_log import AuditLog
from app.models.base import Base
from app.models.campaign import Campaign, CampaignSource
from app.models.clip import Clip, ReviewStatus
from app.models.job import Job, JobStatus, RightsBasis
from app.models.permission import Permission
from app.models.upload import Upload, UploadPrivacy, UploadStatus
from app.models.user import User
from app.models.youtube_channel import YouTubeChannel

__all__ = [
    "AuditLog",
    "Base",
    "Campaign",
    "CampaignSource",
    "Clip",
    "Job",
    "JobStatus",
    "Permission",
    "ReviewStatus",
    "RightsBasis",
    "Upload",
    "UploadPrivacy",
    "UploadStatus",
    "User",
    "YouTubeChannel",
]
