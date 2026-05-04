"""Writer for the admin-owned ``voice_debug_events`` table.

This is a pipeline trace table the admin dashboard uses to render a
"what happened during this call" timeline. We keep it deliberately
high-signal — one row per major bridge stage (session open/close, tool
dispatch, agent swap, post-call extraction, watchdog hangup) rather
than spamming a row per audio frame.

Schema (verified live):

    id          uuid       NOT NULL
    trace_id    text       NULL    -- our session_id hex (matches voice_tool_executions.session_id when castable)
    agent_id    uuid       NULL
    document_id uuid       NULL    -- only used by KB-side flows
    event_type  text       NOT NULL
    event_stage text       NULL
    message     text       NOT NULL
    payload     jsonb      NULL
    created_at  timestamptz NOT NULL
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import log_dataflow


class VoiceDebugEventsRepository:
    _INSERT_SQL = text(
        """
        INSERT INTO voice_debug_events (
            id, trace_id, agent_id, document_id,
            event_type, event_stage, message, payload, created_at
        )
        VALUES (
            :id, :trace_id, :agent_id, :document_id,
            :event_type, :event_stage, :message,
            CAST(:payload AS jsonb), NOW()
        )
        """
    )

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def emit(
        self,
        *,
        event_type: str,
        message: str,
        event_stage: str | None = None,
        trace_id: str | None = None,
        agent_id: uuid.UUID | None = None,
        document_id: uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        row_id = uuid.uuid4()
        await self.session.execute(
            self._INSERT_SQL,
            {
                "id": row_id,
                "trace_id": trace_id,
                "agent_id": agent_id,
                "document_id": document_id,
                "event_type": event_type,
                "event_stage": event_stage,
                "message": message,
                "payload": (
                    json.dumps(payload, default=str) if payload is not None else None
                ),
            },
        )
        log_dataflow(
            "debug_event.persisted",
            f"type={event_type} stage={event_stage} msg={message[:80]}",
            level="debug",
        )
        return row_id
