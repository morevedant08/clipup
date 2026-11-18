"""Audit logging helper that guarantees sensitive fields are redacted."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog

_SENSITIVE_KEYS = frozenset(
    {
        "refresh_token",
        "access_token",
        "id_token",
        "encrypted_refresh_token",
        "client_secret",
        "authorization",
        "password",
        "secret",
        "token",
    }
)


def _sanitize_details(data: Any) -> Any:
    """Recursively redact any sensitive keys before writing to audit_log."""
    if isinstance(data, dict):
        sanitized: dict[str, Any] = {}
        for key, value in data.items():
            if str(key).lower() in _SENSITIVE_KEYS:
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = _sanitize_details(value)
        return sanitized
    if isinstance(data, list):
        return [_sanitize_details(item) for item in data]
    return data


def record_audit(
    db: Session,
    action: str,
    *,
    user_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Insert an audit_log record into the current SQLAlchemy session."""
    entry = AuditLog(
        user_id=user_id,
        job_id=job_id,
        action=action,
        details_json=_sanitize_details(details or {}),
    )
    db.add(entry)
    return entry
