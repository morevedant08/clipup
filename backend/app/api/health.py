"""FastAPI health check endpoints."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

from app.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health")
def health_check() -> dict[str, str]:
    """Liveness probe confirming the API process is running."""
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app_name,
        "env": settings.app_env,
    }


@router.get("/health/ready")
def readiness_check() -> dict[str, Any]:
    """Readiness probe reporting dependency health (database and Redis)."""
    checks: dict[str, str] = {}
    overall_ok = True

    # Database check (lazy import so scaffold works before DB step)
    try:
        from sqlalchemy import text

        from app.database import engine

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except ImportError:
        checks["database"] = "not_configured"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Readiness database check failed: %s", type(exc).__name__)
        checks["database"] = "error"
        overall_ok = False

    # Redis check
    try:
        import redis

        settings = get_settings()
        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1)
        client.ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Readiness redis check failed: %s", type(exc).__name__)
        checks["redis"] = "error"
        overall_ok = False

    return {
        "status": "ok" if overall_ok else "degraded",
        "checks": checks,
    }

