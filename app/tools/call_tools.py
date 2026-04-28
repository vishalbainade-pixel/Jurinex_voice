"""Call lifecycle tool — let the agent gracefully signal end-of-call."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CallStatus
from app.db.repositories import AgentToolEventRepository, CallRepository
from app.db.schemas import EndCallInput
from app.observability.logger import log_dataflow
from app.utils.time_utils import utcnow


async def end_call(session: AsyncSession, payload: EndCallInput) -> dict[str, Any]:
    try:
        call_uuid = uuid.UUID(payload.call_id)
    except ValueError:
        return {"success": False, "message": "invalid call_id"}

    call = await CallRepository(session).get(call_uuid)
    if not call:
        return {"success": False, "message": "call not found"}

    duration: int | None = None
    if call.started_at:
        duration = int((utcnow() - call.started_at).total_seconds())

    await CallRepository(session).update_status(
        call_uuid,
        status=CallStatus.completed,
        ended_at=utcnow(),
        duration_seconds=duration,
    )
    await AgentToolEventRepository(session).add(
        call_id=call_uuid,
        tool_name="end_call",
        input_json=payload.model_dump(),
        output_json={"ended": True},
        success=True,
    )

    # Actually disconnect the Twilio leg if this is a real Twilio call.
    twilio_dropped = False
    if call.twilio_call_sid:
        from app.services.call_service import CallService

        twilio_dropped = CallService.hangup_twilio_call(call.twilio_call_sid)

    log_dataflow(
        "tool.end_call",
        f"agent ended call (twilio_dropped={twilio_dropped})",
        payload={"reason": payload.reason, "call_sid": call.twilio_call_sid},
    )
    return {
        "success": True,
        "twilio_dropped": twilio_dropped,
        "message": "Call ended gracefully.",
    }
