"""Structured logger that pairs Python logging with Rich for visibility."""

from __future__ import annotations

import logging
from typing import Any

from rich.logging import RichHandler

from app.config import settings
from app.observability.rich_console import (
    console,
    render_event_panel,
    render_error_panel,
)
from app.observability.trace_context import get_trace

_LOG_FORMAT = "%(message)s"
_DATE_FORMAT = "[%X]"

_configured = False


def configure_logging() -> None:
    """Install a single Rich-based handler at the level from settings."""
    global _configured
    if _configured:
        return

    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    handler = RichHandler(
        console=console,
        rich_tracebacks=True,
        show_path=False,
        markup=True,
        log_time_format=_DATE_FORMAT,
    )

    logging.basicConfig(
        level=level,
        format=_LOG_FORMAT,
        datefmt=_DATE_FORMAT,
        handlers=[handler],
        force=True,
    )

    # Quiet down extremely chatty libs.
    for noisy in (
        "websockets",
        "uvicorn.access",
        "httpx",
        "httpcore",
        "multipart",
        "multipart.multipart",
        "python_multipart",
        "python_multipart.multipart",
        "twilio.http_client",
        "urllib3",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)


def _trace_prefix() -> str:
    ctx = get_trace()
    parts = []
    if ctx.call_sid:
        parts.append(f"call={ctx.call_sid}")
    if ctx.direction:
        parts.append(ctx.direction)
    parts.append(f"sid={ctx.session_id[:8]}")
    return "[" + " ".join(parts) + "]"


def log_dataflow(
    stage: str,
    message: str,
    payload: dict[str, Any] | None = None,
    *,
    level: str = "info",
) -> None:
    """Single entrypoint for dataflow stage logs (twilio.*, gemini.*, db.*, ...)."""
    logger = get_logger("jurinex.dataflow")
    log_fn = getattr(logger, level, logger.info)
    prefix = _trace_prefix()
    log_fn(f"{prefix} [bold magenta]{stage}[/bold magenta] {message}")

    if payload and settings.debug:
        # Truncate big payloads in console; full payload is stored in DB by services.
        preview = {k: _shorten(v) for k, v in payload.items()}
        logger.debug(f"{prefix} [dim]payload[/dim] {preview}")


def log_event_panel(
    title: str,
    fields: dict[str, Any],
    *,
    style: str = "cyan",
    icon_key: str | None = None,
) -> None:
    render_event_panel(title, fields, style=style, icon_key=icon_key)


def log_error(title: str, message: str, fields: dict[str, Any] | None = None) -> None:
    render_error_panel(title, message, fields)
    get_logger("jurinex.error").error(f"{title}: {message}")


def _shorten(value: Any, limit: int = 200) -> Any:
    text = repr(value)
    if len(text) > limit:
        return text[:limit] + "…"
    return value
