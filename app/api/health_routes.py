"""Health-check routes — liveness, DB readiness, sanitized config."""

from __future__ import annotations

from fastapi import APIRouter

from app.config import settings
from app.db.database import ping_database

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
async def health() -> dict[str, str]:
    return {"status": "ok", "app": settings.app_name, "env": settings.app_env}


@router.get("/db")
async def health_db() -> dict[str, str]:
    try:
        ok = await ping_database()
        return {"status": "ok" if ok else "degraded"}
    except Exception as exc:
        return {"status": "down", "error": str(exc)}


@router.get("/config")
async def health_config() -> dict[str, object]:
    """Surfaces non-sensitive config so a sysadmin can verify deployment."""
    return {
        "app_name": settings.app_name,
        "env": settings.app_env,
        "debug": settings.debug,
        "demo_mode": settings.demo_mode,
        "log_level": settings.log_level,
        "gemini_model": settings.gemini_model,
        "twilio_phone_number": settings.twilio_phone_number,
        "public_base_url": settings.public_base_url,
        "twilio_configured": bool(
            settings.twilio_account_sid and settings.twilio_auth_token
        ),
        "gemini_configured": bool(settings.gemini_key),
    }
