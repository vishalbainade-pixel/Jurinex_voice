"""Writer for the admin-owned ``voice_calendar_bookings`` audit table.

One row per ``calendar_book`` execution. Linked to the originating
``voice_tool_executions`` row via ``tool_execution_id`` so the dashboard
can show: tool call → booking → google_event_id.

Schema (verified live):

    id                  uuid       NOT NULL
    session_id          uuid       NULL
    agent_id            uuid       NULL
    tool_execution_id   uuid       NULL
    google_event_id     text       NULL
    google_calendar_id  text       NOT NULL
    summary             text       NULL
    description         text       NULL
    start_time          timestamptz NOT NULL
    end_time            timestamptz NOT NULL
    attendee_name       text       NULL
    attendee_email      text       NULL
    attendee_phone      text       NULL
    status              text       NOT NULL    -- 'booked' | 'failed'
    metadata            jsonb      NOT NULL
    created_at          timestamptz NOT NULL
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import log_db_row, log_dataflow


class VoiceCalendarBookingsRepository:
    _INSERT_SQL = text(
        """
        INSERT INTO voice_calendar_bookings (
            id, session_id, agent_id, tool_execution_id,
            google_event_id, google_calendar_id, summary, description,
            start_time, end_time,
            attendee_name, attendee_email, attendee_phone,
            status, metadata, created_at
        )
        VALUES (
            :id, :session_id, :agent_id, :tool_execution_id,
            :google_event_id, :google_calendar_id, :summary, :description,
            :start_time, :end_time,
            :attendee_name, :attendee_email, :attendee_phone,
            :status, CAST(:metadata AS jsonb), NOW()
        )
        """
    )

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert(
        self,
        *,
        google_event_id: str | None,
        google_calendar_id: str,
        summary: str | None,
        description: str | None,
        start_time: datetime,
        end_time: datetime,
        attendee_name: str | None,
        attendee_email: str | None,
        attendee_phone: str | None,
        status: str,
        metadata: dict[str, Any],
        session_id: uuid.UUID | None = None,
        agent_id: uuid.UUID | None = None,
        tool_execution_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        booking_id = uuid.uuid4()
        await self.session.execute(
            self._INSERT_SQL,
            {
                "id": booking_id,
                "session_id": session_id,
                "agent_id": agent_id,
                "tool_execution_id": tool_execution_id,
                "google_event_id": google_event_id,
                "google_calendar_id": google_calendar_id,
                "summary": summary,
                "description": description,
                "start_time": start_time,
                "end_time": end_time,
                "attendee_name": attendee_name,
                "attendee_email": attendee_email,
                "attendee_phone": attendee_phone,
                "status": status,
                "metadata": json.dumps(metadata, default=str),
            },
        )
        log_dataflow(
            "calendar.booking.persisted",
            f"id={booking_id} status={status} "
            f"google_event_id={google_event_id} "
            f"calendar={google_calendar_id}",
        )

        # Render the persisted row as a Rich table so the operator can see
        # exactly what landed in voice_calendar_bookings without opening psql.
        # Status drives the panel colour: green for booked, red for failed.
        log_db_row(
            table_name="voice_calendar_bookings",
            operation=f"INSERT (status={status})",
            columns={
                "id": booking_id,
                "session_id": session_id,
                "agent_id": agent_id,
                "tool_execution_id": tool_execution_id,
                "google_event_id": google_event_id,
                "google_calendar_id": google_calendar_id,
                "summary": summary,
                "start_time": start_time.isoformat() if start_time else None,
                "end_time": end_time.isoformat() if end_time else None,
                "attendee_name": attendee_name,
                "attendee_email": attendee_email,
                "attendee_phone": attendee_phone,
                "status": status,
                "metadata": metadata,
                "description": description,
            },
            style=("green" if status == "booked" else "red"),
            icon_key="tool" if status == "booked" else "error",
        )
        return booking_id
