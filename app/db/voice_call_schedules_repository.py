"""Read/write access to ``voice_call_schedules`` (admin-owned outbound queue).

Ownership split (per docs/SCHEDULER.md):

  * Admin app          → INSERT new rows (status='pending'), UPDATE metadata
                         columns, soft-cancel by setting status='cancelled'
                         while still pending/queued.
  * Call-agent runtime → owns ``status``, ``attempts``, ``last_attempt_at``,
                         ``last_error``, ``twilio_call_sid``, ``call_id``
                         from the moment a row is claimed onwards.

This repository implements every write the call-agent needs:

  * ``claim_due_row()``            — SELECT … FOR UPDATE SKIP LOCKED + flip to 'queued'
  * ``mark_dialing()``             — flip 'queued' → 'in_progress' (+ Twilio SID)
  * ``mark_completed(call_id)``    — flip 'in_progress' → 'completed'
  * ``mark_no_answer_or_requeue()` — re-queue with backoff or move to 'no_answer'
  * ``mark_failed(error)``         — terminal hard-error
  * ``find_by_id(schedule_id)``    — used when a Twilio webhook references the row
  * ``find_by_call_sid(...)``      — useful when the bridge only has the Twilio SID
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import log_db_row, log_dataflow


# ---------------------------------------------------------------------------
# Typed result object
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ScheduledCall:
    """Row from ``voice_call_schedules`` packaged for the bridge to consume."""

    id: uuid.UUID
    agent_id: uuid.UUID
    recipient_name: str | None
    recipient_phone: str
    recipient_email: str | None
    scheduled_at: datetime
    timezone: str
    status: str
    attempts: int
    max_attempts: int
    last_attempt_at: datetime | None
    last_error: str | None
    twilio_call_sid: str | None
    call_id: uuid.UUID | None
    notes: str | None
    metadata: dict[str, Any]
    batch_id: uuid.UUID | None
    source: str
    created_by: str | None


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class VoiceCallSchedulesRepository:
    """All call-agent writes against ``voice_call_schedules`` live here."""

    _SELECT_COLS = (
        "id, agent_id, recipient_name, recipient_phone, recipient_email, "
        "scheduled_at, timezone, status, attempts, max_attempts, "
        "last_attempt_at, last_error, twilio_call_sid, call_id, notes, "
        "metadata, batch_id, source, created_by"
    )

    _CLAIM_SELECT_SQL = text(
        f"""
        SELECT {_SELECT_COLS}
        FROM voice_call_schedules
        WHERE status = 'pending'
          AND scheduled_at <= now()
        ORDER BY scheduled_at ASC, created_at ASC
        FOR UPDATE SKIP LOCKED
        LIMIT 1
        """
    )

    _CLAIM_UPDATE_SQL = text(
        """
        UPDATE voice_call_schedules
           SET status     = 'queued',
               updated_at = now()
         WHERE id = :id
         RETURNING id
        """
    )

    _MARK_DIALING_SQL = text(
        """
        UPDATE voice_call_schedules
           SET status          = 'in_progress',
               attempts        = attempts + 1,
               last_attempt_at = now(),
               twilio_call_sid = :twilio_call_sid,
               last_error      = NULL,
               updated_at      = now()
         WHERE id = :id
           AND status = 'queued'
         RETURNING attempts
        """
    )

    _MARK_COMPLETED_SQL = text(
        """
        UPDATE voice_call_schedules
           SET status     = 'completed',
               call_id    = :call_id,
               updated_at = now()
         WHERE id = :id
         RETURNING attempts
        """
    )

    _REQUEUE_SQL = text(
        """
        UPDATE voice_call_schedules
           SET status       = 'pending',
               scheduled_at = now() + (interval '15 minutes' * power(2, attempts - 1)),
               last_error   = :last_error,
               updated_at   = now()
         WHERE id = :id
           AND attempts < max_attempts
         RETURNING attempts, max_attempts, scheduled_at
        """
    )

    _MARK_NO_ANSWER_SQL = text(
        """
        UPDATE voice_call_schedules
           SET status     = 'no_answer',
               last_error = :last_error,
               updated_at = now()
         WHERE id = :id
         RETURNING attempts
        """
    )

    _MARK_FAILED_SQL = text(
        """
        UPDATE voice_call_schedules
           SET status     = 'failed',
               last_error = :last_error,
               updated_at = now()
         WHERE id = :id
         RETURNING attempts
        """
    )

    _SELECT_BY_ID_SQL = text(
        f"""
        SELECT {_SELECT_COLS}
        FROM voice_call_schedules
        WHERE id = :id
        LIMIT 1
        """
    )

    _SELECT_BY_SID_SQL = text(
        f"""
        SELECT {_SELECT_COLS}
        FROM voice_call_schedules
        WHERE twilio_call_sid = :sid
        ORDER BY updated_at DESC
        LIMIT 1
        """
    )

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Claim (poller — single transaction)
    # ------------------------------------------------------------------

    async def claim_due_row(self) -> ScheduledCall | None:
        """Pop the next due row atomically.

        Pattern is the standard Postgres job-queue:
            SELECT … FOR UPDATE SKIP LOCKED LIMIT 1
            UPDATE … SET status='queued'

        Both run inside ONE transaction so multiple workers can poll
        concurrently without claiming the same row twice.
        """
        result = await self.session.execute(self._CLAIM_SELECT_SQL)
        row = result.mappings().first()
        if row is None:
            return None

        # Flip to 'queued' inside the same transaction.
        await self.session.execute(self._CLAIM_UPDATE_SQL, {"id": row["id"]})

        scheduled = _row_to_scheduled(row)
        log_dataflow(
            "scheduler.claim",
            f"id={scheduled.id} → {scheduled.recipient_phone} "
            f"(scheduled_at={scheduled.scheduled_at.isoformat()} "
            f"attempts={scheduled.attempts}/{scheduled.max_attempts})",
        )
        log_db_row(
            table_name="voice_call_schedules",
            operation="UPDATE (status=queued)",
            columns={
                "id": scheduled.id,
                "agent_id": scheduled.agent_id,
                "recipient_phone": scheduled.recipient_phone,
                "recipient_name": scheduled.recipient_name,
                "scheduled_at": scheduled.scheduled_at.isoformat(),
                "attempts": f"{scheduled.attempts}/{scheduled.max_attempts}",
                "source": scheduled.source,
                "batch_id": scheduled.batch_id,
                "notes": scheduled.notes,
            },
            style="cyan",
            icon_key="db",
        )
        return scheduled

    # ------------------------------------------------------------------
    # Lifecycle UPDATEs (after the dial fires)
    # ------------------------------------------------------------------

    async def mark_dialing(
        self,
        *,
        schedule_id: uuid.UUID,
        twilio_call_sid: str,
    ) -> bool:
        """Flip queued → in_progress and record the Twilio SID.

        Gated on ``status='queued'`` so an admin cancel that landed
        between claim and dial wins the race (we get 0 rows back and
        the caller skips the dial).
        """
        result = await self.session.execute(
            self._MARK_DIALING_SQL,
            {"id": schedule_id, "twilio_call_sid": twilio_call_sid},
        )
        row = result.first()
        if row is None:
            log_dataflow(
                "scheduler.dial.cancelled",
                f"id={schedule_id} no longer in 'queued' — admin cancel beat us",
                level="warning",
            )
            return False
        log_dataflow(
            "scheduler.dial.dispatched",
            f"id={schedule_id} sid={twilio_call_sid} attempts={row.attempts}",
        )
        log_db_row(
            table_name="voice_call_schedules",
            operation="UPDATE (status=in_progress)",
            columns={
                "id": schedule_id,
                "twilio_call_sid": twilio_call_sid,
                "attempts": row.attempts,
                "status": "in_progress",
            },
            style="green",
            icon_key="tool",
        )
        return True

    async def mark_completed(
        self,
        *,
        schedule_id: uuid.UUID,
        call_id: uuid.UUID | None,
    ) -> None:
        await self.session.execute(
            self._MARK_COMPLETED_SQL,
            {"id": schedule_id, "call_id": call_id},
        )
        log_dataflow(
            "scheduler.complete",
            f"id={schedule_id} call_id={call_id}",
        )
        log_db_row(
            table_name="voice_call_schedules",
            operation="UPDATE (status=completed)",
            columns={
                "id": schedule_id,
                "call_id": call_id,
                "status": "completed",
            },
            style="green",
            icon_key="tool",
        )

    async def mark_no_answer_or_requeue(
        self,
        *,
        schedule_id: uuid.UUID,
        last_error: str,
    ) -> str:
        """Re-queue with exponential backoff; if attempts exhausted, terminate.

        Returns the new status: ``'pending'`` (re-queued) or ``'no_answer'``.
        """
        result = await self.session.execute(
            self._REQUEUE_SQL,
            {"id": schedule_id, "last_error": last_error[:2000]},
        )
        row = result.first()
        if row is not None:
            log_dataflow(
                "scheduler.requeue",
                f"id={schedule_id} attempts={row.attempts}/{row.max_attempts} "
                f"next_at={row.scheduled_at.isoformat()} error={last_error[:80]}",
            )
            log_db_row(
                table_name="voice_call_schedules",
                operation="UPDATE (re-queue with backoff)",
                columns={
                    "id": schedule_id,
                    "status": "pending",
                    "attempts": f"{row.attempts}/{row.max_attempts}",
                    "next_scheduled_at": row.scheduled_at.isoformat(),
                    "last_error": last_error,
                },
                style="yellow",
                icon_key="warn",
            )
            return "pending"

        # attempts exhausted → terminal no_answer
        result = await self.session.execute(
            self._MARK_NO_ANSWER_SQL,
            {"id": schedule_id, "last_error": last_error[:2000]},
        )
        row = result.first()
        log_dataflow(
            "scheduler.no_answer",
            f"id={schedule_id} attempts exhausted ({row.attempts if row else '?'})",
            level="warning",
        )
        log_db_row(
            table_name="voice_call_schedules",
            operation="UPDATE (status=no_answer — exhausted)",
            columns={
                "id": schedule_id,
                "status": "no_answer",
                "attempts": row.attempts if row else None,
                "last_error": last_error,
            },
            style="red",
            icon_key="error",
        )
        return "no_answer"

    async def mark_failed(
        self,
        *,
        schedule_id: uuid.UUID,
        last_error: str,
    ) -> None:
        result = await self.session.execute(
            self._MARK_FAILED_SQL,
            {"id": schedule_id, "last_error": last_error[:2000]},
        )
        row = result.first()
        log_dataflow(
            "scheduler.failed",
            f"id={schedule_id} terminal error: {last_error[:160]}",
            level="error",
        )
        log_db_row(
            table_name="voice_call_schedules",
            operation="UPDATE (status=failed)",
            columns={
                "id": schedule_id,
                "status": "failed",
                "attempts": row.attempts if row else None,
                "last_error": last_error,
            },
            style="red",
            icon_key="error",
        )

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    async def find_by_id(self, schedule_id: uuid.UUID) -> ScheduledCall | None:
        result = await self.session.execute(
            self._SELECT_BY_ID_SQL, {"id": schedule_id}
        )
        row = result.mappings().first()
        return _row_to_scheduled(row) if row else None

    async def find_by_call_sid(self, twilio_call_sid: str) -> ScheduledCall | None:
        result = await self.session.execute(
            self._SELECT_BY_SID_SQL, {"sid": twilio_call_sid}
        )
        row = result.mappings().first()
        return _row_to_scheduled(row) if row else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_scheduled(row: Any) -> ScheduledCall:
    return ScheduledCall(
        id=row["id"],
        agent_id=row["agent_id"],
        recipient_name=row["recipient_name"],
        recipient_phone=row["recipient_phone"],
        recipient_email=row["recipient_email"],
        scheduled_at=row["scheduled_at"],
        timezone=row["timezone"],
        status=row["status"],
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        last_attempt_at=row["last_attempt_at"],
        last_error=row["last_error"],
        twilio_call_sid=row["twilio_call_sid"],
        call_id=row["call_id"],
        notes=row["notes"],
        metadata=dict(row["metadata"] or {}),
        batch_id=row["batch_id"],
        source=row["source"],
        created_by=row["created_by"],
    )
