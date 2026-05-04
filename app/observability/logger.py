"""Structured logger that pairs Python logging with Rich for visibility."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from rich.logging import RichHandler

from app.config import settings
from app.observability.rich_console import (
    console,
    render_db_row_table,
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
    persist: bool = False,
) -> None:
    """Single entrypoint for dataflow stage logs (twilio.*, gemini.*, db.*, ...).

    When ``persist=True`` (or the stage matches ``_PERSIST_PREFIXES`` below),
    the event is also written to ``call_debug_events`` on a background task
    so it never blocks the realtime path. ``stage`` is split into
    ``event_type.event_stage`` on the first dot for the DB columns.
    """
    logger = get_logger("jurinex.dataflow")
    log_fn = getattr(logger, level, logger.info)
    prefix = _trace_prefix()
    log_fn(f"{prefix} [bold magenta]{stage}[/bold magenta] {message}")

    if payload and settings.debug:
        # Truncate big payloads in console; full payload is stored in DB by services.
        preview = {k: _shorten(v) for k, v in payload.items()}
        logger.debug(f"{prefix} [dim]payload[/dim] {preview}")

    if persist or _should_persist(stage):
        _spawn_persist(stage=stage, message=message, payload=payload)


# Stages whose dataflow events are durable enough to write to call_debug_events.
# Anything *not* on this list stays console-only to keep DB writes cheap.
_PERSIST_PREFIXES: tuple[str, ...] = (
    "twilio.media.start",
    "twilio.media.stop",
    "twilio.call.status",
    "twilio.hangup",
    "gemini.session",
    "gemini.receive_loop",
    "gemini.transcript",
    "watchdog.",
    "tool.dispatch",
    "tool.ticket",
    "tool.escalation",
    "tool.end_call",
    "call.summary",
    "recorder.armed",
    "gcs.uploaded",
    "gcs.skipped",
)


def _should_persist(stage: str) -> bool:
    return any(stage.startswith(prefix) for prefix in _PERSIST_PREFIXES)


def _spawn_persist(
    *, stage: str, message: str, payload: dict[str, Any] | None
) -> None:
    """Fire-and-forget DB write so the realtime path is never blocked."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # not in an async context — skip persistence quietly
    loop.create_task(_persist_dataflow(stage=stage, message=message, payload=payload))


async def _persist_dataflow(
    *, stage: str, message: str, payload: dict[str, Any] | None
) -> None:
    # Local imports to avoid an import cycle (db ↔ observability).
    from app.db.database import session_scope
    from app.db.repositories import CallDebugEventRepository

    ctx = get_trace()
    event_type, _, event_stage = stage.partition(".")
    if not event_stage:
        event_stage = event_type

    safe_payload: dict[str, Any] | None = None
    if payload:
        try:
            import json as _json

            _json.dumps(payload, default=str)
            safe_payload = payload
        except Exception:
            safe_payload = {"_repr": repr(payload)[:500]}

    try:
        async with session_scope() as session:
            await CallDebugEventRepository(session).add(
                event_type=event_type,
                event_stage=event_stage,
                message=message[:2000],
                twilio_call_sid=ctx.call_sid,
                payload=safe_payload,
            )
    except Exception as exc:
        # Persistence is best-effort; never raise into the caller.
        get_logger("jurinex.dataflow.persist").debug(
            f"failed to persist debug event ({stage}): {exc}"
        )


def log_event_panel(
    title: str,
    fields: dict[str, Any],
    *,
    style: str = "cyan",
    icon_key: str | None = None,
) -> None:
    render_event_panel(title, fields, style=style, icon_key=icon_key)


def log_db_row(
    *,
    table_name: str,
    operation: str,
    columns: dict[str, Any],
    style: str = "blue",
    icon_key: str | None = "db",
) -> None:
    """Render a persisted DB row as a labelled table in the console.

    Use this from repository writers when you want the actual row contents
    visible alongside the dataflow logs. The repository continues to emit
    its own `*.persisted` dataflow line as well — this is purely the human-
    readable companion for terminal observation.
    """
    render_db_row_table(
        table_name=table_name,
        operation=operation,
        columns=columns,
        style=style,
        icon_key=icon_key,
    )


def log_error(title: str, message: str, fields: dict[str, Any] | None = None) -> None:
    render_error_panel(title, message, fields)
    get_logger("jurinex.error").error(f"{title}: {message}")


def _shorten(value: Any, limit: int = 200) -> Any:
    text = repr(value)
    if len(text) > limit:
        return text[:limit] + "…"
    return value
