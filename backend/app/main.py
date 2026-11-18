"""Clipper FastAPI application entry point."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.campaigns import router as campaigns_router
from app.api.health import router as health_router
from app.api.jobs import router as jobs_router
from app.api.permissions import router as permissions_router
from app.config import get_settings


def configure_logging(level: str) -> None:
    """Configure application-wide logging without exposing sensitive headers or tokens."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    # Prevent verbose third-party loggers from printing auth headers or tokens
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def create_app() -> FastAPI:
    """Build and configure the FastAPI application instance."""
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=f"{settings.app_name} API",
        version="0.1.0",
        description="Rights-safe automated video clipping platform API.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(campaigns_router)
    app.include_router(permissions_router)
    app.include_router(jobs_router)
    return app


app = create_app()

