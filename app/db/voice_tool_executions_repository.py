"""Writer for the admin-owned ``voice_tool_executions`` audit table.

Two-step lifecycle so the dashboard can render in-flight calls correctly:

  1. ``insert_pending(...)``  — INSERT a row when the model dispatches a tool
                                call (status='pending', input_json populated,
                                completed_at=NULL).
  2. ``mark_completed(...)``  — UPDATE the same row to status='completed' (or
                                'failed') with the output_json + latency_ms +
                                completed_at after the handler returns.

Schema (verified against live DB):

    id                uuid       NOT NULL
    session_id        uuid       NULL
    agent_id          uuid       NULL
    trace_id          uuid       NULL
    function_call_id  text       NULL
    tool_name         text       NOT NULL
    input_json        jsonb      NOT NULL
    output_json       jsonb      NULL
    status            text       NOT NULL    -- 'pending' | 'completed' | 'failed'
    error_message     text       NULL
    latency_ms        integer    NULL
    created_at        timestamptz NOT NULL
    completed_at      timestamptz NULL
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import log_dataflow


class VoiceToolExecutionsRepository:
    _INSERT_SQL = text(
        """
        INSERT INTO voice_tool_executions (
            id, session_id, agent_id, trace_id, function_call_id,
            tool_name, input_json, status, created_at
        )
        VALUES (
            :id, :session_id, :agent_id, :trace_id, :function_call_id,
            :tool_name, CAST(:input_json AS jsonb), 'pending', NOW()
        )
        """
    )

    _UPDATE_SQL = text(
        """
        UPDATE voice_tool_executions
        SET status        = :status,
            output_json   = CAST(:output_json AS jsonb),
            error_message = :error_message,
            latency_ms    = :latency_ms,
            completed_at  = NOW()
        WHERE id = :id
        """
    )

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_pending(
        self,
        *,
        execution_id: uuid.UUID,
        tool_name: str,
        input_payload: dict[str, Any],
        session_id: uuid.UUID | None = None,
        agent_id: uuid.UUID | None = None,
        trace_id: uuid.UUID | None = None,
        function_call_id: str | None = None,
    ) -> None:
        await self.session.execute(
            self._INSERT_SQL,
            {
                "id": execution_id,
                "session_id": session_id,
                "agent_id": agent_id,
                "trace_id": trace_id,
                "function_call_id": function_call_id,
                "tool_name": tool_name,
                "input_json": json.dumps(input_payload, default=str),
            },
        )
        log_dataflow(
            "tool_exec.pending",
            f"id={execution_id} tool={tool_name} agent={agent_id}",
        )

    async def mark_completed(
        self,
        *,
        execution_id: uuid.UUID,
        output_payload: dict[str, Any] | None,
        latency_ms: int | None,
        success: bool,
        error_message: str | None = None,
    ) -> None:
        status = "completed" if success else "failed"
        await self.session.execute(
            self._UPDATE_SQL,
            {
                "id": execution_id,
                "status": status,
                "output_json": (
                    json.dumps(output_payload, default=str)
                    if output_payload is not None
                    else None
                ),
                "error_message": error_message,
                "latency_ms": latency_ms,
            },
        )
        log_dataflow(
            "tool_exec.completed" if success else "tool_exec.failed",
            f"id={execution_id} latency_ms={latency_ms} status={status}",
        )
