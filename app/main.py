"""FastAPI app entrypoint for Jurinex_call_agent."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import admin_routes, debug_routes, health_routes, twilio_routes
from app.config import settings
from app.lifecycle import lifespan


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )

    # Serve static assets (e.g. pre-rendered greeting.wav for Twilio <Play>)
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(health_routes.router)
    app.include_router(twilio_routes.router)
    app.include_router(admin_routes.router)
    app.include_router(debug_routes.router)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "app": settings.app_name,
            "agent": "Preeti",
            "status": "ok",
            "docs": "/docs",
        }

    return app


app = create_app()
