"""App startup / shutdown hooks."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.db.database import engine, ping_database
from app.observability.logger import (
    configure_logging,
    log_dataflow,
    log_error,
    log_event_panel,
)


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[override]
    configure_logging()
    log_event_panel(
        "APP STARTING",
        {
            "App": settings.app_name,
            "Env": settings.app_env,
            "Demo Mode": settings.demo_mode,
            "Public Base URL": settings.public_base_url,
        },
        style="cyan",
        icon_key="info",
    )
    try:
        ok = await ping_database()
        if ok:
            log_dataflow("db.ping", "database reachable")
        else:
            log_dataflow("db.ping", "ping returned non-1", level="warning")
    except Exception as exc:
        log_error("DATABASE UNREACHABLE", str(exc))

    # Pre-load the eager greeting WAV → μ-law 8kHz so we can stream it
    # through the Twilio media WS instantly on call start (skips the slow
    # <Play>-then-<Connect> sequential path; Gemini Live cold-start can
    # now happen in parallel with greeting playback).
    try:
        from app.realtime.greeting_loader import load_greeting

        load_greeting()
    except Exception as exc:
        log_dataflow("greeting.load.error", str(exc), level="warning")

    yield

    log_event_panel("APP SHUTDOWN", {"App": settings.app_name}, style="yellow", icon_key="info")
    await engine.dispose()
