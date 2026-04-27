"""Escalation tool — hands the call to a human team."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResolutionStatus
from app.db.repositories import (
    AgentToolEventRepository,
    CallRepository,
    EscalationRepository,
)
from app.db.schemas import EscalateToHumanInput
from app.observability.logger import log_dataflow, log_event_panel


async def escalate_to_human(
    session: AsyncSession, payload: EscalateToHumanInput
) -> dict[str, Any]:
    try:
        call_uuid = uuid.UUID(payload.call_id)
    except ValueError:
        return {"success": False, "message": "invalid call_id"}

    esc = await EscalationRepository(session).create(
        call_id=call_uuid,
        reason=payload.reason,
        assigned_team=payload.assigned_team,
    )
    await CallRepository(session).update_status(
        call_uuid, resolution_status=ResolutionStatus.escalated
    )
    await AgentToolEventRepository(session).add(
        call_id=call_uuid,
        tool_name="escalate_to_human",
        input_json=payload.model_dump(),
        output_json={"escalation_id": str(esc.id)},
        success=True,
    )

    log_event_panel(
        "ESCALATION CREATED",
        {
            "Call": str(call_uuid),
            "Team": payload.assigned_team,
            "Reason": payload.reason,
        },
        style="yellow",
        icon_key="escalation",
    )
    log_dataflow(
        "tool.escalation.create",
        f"escalated to {payload.assigned_team}",
        payload={"escalation_id": str(esc.id)},
    )

    return {
        "success": True,
        "escalation_id": str(esc.id),
        "assigned_team": payload.assigned_team,
        "message": "Escalation created. A human agent will follow up.",
    }
