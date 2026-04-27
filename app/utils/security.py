"""Auth helpers for protecting admin/debug routes with a simple API key."""

from __future__ import annotations

from fastapi import Header, HTTPException, status

from app.config import settings


async def require_admin_api_key(x_admin_api_key: str | None = Header(default=None)) -> None:
    """Reject requests that don't carry the configured admin API key."""
    if not settings.admin_api_key:
        # If unset, fail closed in production; allow in dev for convenience.
        if settings.is_production:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "admin api key not configured")
        return
    if x_admin_api_key != settings.admin_api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid admin api key")


def mask_phone(phone: str | None) -> str:
    if not phone:
        return "-"
    if len(phone) <= 4:
        return phone
    return phone[:3] + "*" * (len(phone) - 6) + phone[-3:]
