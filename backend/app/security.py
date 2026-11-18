"""Security utilities: JWT session tokens, OAuth state signing, and Fernet token encryption."""

from __future__ import annotations

import base64
import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class SecurityConfigError(RuntimeError):
    """Raised when required cryptographic configuration is missing or invalid."""


class TokenCipher:
    """Fernet symmetric cipher for encrypting YouTube OAuth refresh tokens at rest."""

    def __init__(self, key: str | bytes) -> None:
        key_bytes = key.encode("utf-8") if isinstance(key, str) else key
        self._fernet = Fernet(key_bytes)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> TokenCipher:
        """Build a TokenCipher from application settings.

        In production, TOKEN_ENCRYPTION_KEY must be explicitly provided.
        In non-production environments, falls back to a key derived from JWT_SECRET_KEY
        with a warning so local setups work smoothly.
        """
        cfg = settings or get_settings()
        if cfg.token_encryption_key:
            return cls(cfg.token_encryption_key)
        if cfg.app_env.lower() == "production":
            raise SecurityConfigError(
                "TOKEN_ENCRYPTION_KEY must be set in production."
            )
        logger.warning(
            "TOKEN_ENCRYPTION_KEY is not set; using derived development-only key."
        )
        digest = hashlib.sha256(cfg.jwt_secret_key.encode("utf-8")).digest()
        derived_key = base64.urlsafe_b64encode(digest)
        return cls(derived_key)

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a secret token string and return a URL-safe ciphertext string."""
        if not plaintext:
            raise ValueError("Cannot encrypt empty token.")
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a stored Fernet ciphertext back into plaintext."""
        try:
            return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Failed to decrypt stored refresh token.") from exc


def create_access_token(
    user_id: uuid.UUID,
    email: str,
    settings: Settings | None = None,
) -> str:
    """Issue a signed JWT session token for an authenticated user."""
    cfg = settings or get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=cfg.jwt_expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, cfg.jwt_secret_key, algorithm=cfg.jwt_algorithm)


def decode_access_token(
    token: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Decode and validate an access JWT."""
    cfg = settings or get_settings()
    payload = jwt.decode(
        token,
        cfg.jwt_secret_key,
        algorithms=[cfg.jwt_algorithm],
    )
    if payload.get("type") != "access" or not payload.get("sub"):
        raise jwt.InvalidTokenError("Invalid access token payload.")
    return payload


def create_oauth_state(
    purpose: str,
    *,
    user_id: uuid.UUID | None = None,
    expires_minutes: int = 10,
    settings: Settings | None = None,
) -> str:
    """Create a short-lived signed OAuth state token for CSRF protection."""
    cfg = settings or get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "purpose": purpose,
        "nonce": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expires_minutes)).timestamp()),
    }
    if user_id is not None:
        payload["uid"] = str(user_id)
    return jwt.encode(payload, cfg.jwt_secret_key, algorithm=cfg.jwt_algorithm)


def verify_oauth_state(
    state: str,
    expected_purpose: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Verify a signed OAuth state token and assert its purpose."""
    cfg = settings or get_settings()
    payload = jwt.decode(
        state,
        cfg.jwt_secret_key,
        algorithms=[cfg.jwt_algorithm],
    )
    if payload.get("purpose") != expected_purpose:
        raise jwt.InvalidTokenError("OAuth state purpose mismatch.")
    return payload

