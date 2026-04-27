"""FastAPI app entrypoint for Jurinex_call_agent."""

from __future__ import annotations

from fastapi import FastAPI

from app.api import admin_routes, debug_routes, health_routes, twilio_routes
from app.config import settings
from app.lifecycle import lifespan


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )

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
