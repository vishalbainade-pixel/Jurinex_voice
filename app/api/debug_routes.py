"""Debug endpoints — primarily the demo conversation simulator."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.db.models import CallDirection, CallStatus, ResolutionStatus, Speaker
from app.db.repositories import CallMessageRepository, CallRepository, CustomerRepository
from app.db.schemas import (
    CreateSupportTicketInput,
    SimulateConversationRequest,
    SimulateConversationResponse,
)
from app.observability.logger import log_event_panel
from app.observability.trace_context import new_trace
from app.prompts import JURINEX_PREETI_SYSTEM_PROMPT
from app.realtime.gemini_live_client import GeminiLiveClient
from app.services.transcript_service import TranscriptService
from app.services.tool_dispatcher import dispatch_tool_call
from app.utils.phone import normalize_e164
from app.utils.time_utils import utcnow

router = APIRouter(prefix="/debug", tags=["debug"])


@router.post("/simulate-conversation", response_model=SimulateConversationResponse)
async def simulate_conversation(
    payload: SimulateConversationRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SimulateConversationResponse:
    try:
        phone = normalize_e164(payload.phone_number)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e

    new_trace(direction="simulated", customer_phone=phone)

    customer, _ = await CustomerRepository(session).get_or_create(
        phone=phone,
        name=payload.customer_name,
    )
    call = await CallRepository(session).create(
        twilio_call_sid=None,
        direction=CallDirection.inbound,
        customer_phone=phone,
        twilio_from=phone,
        twilio_to="+SIMULATED",
        customer_id=customer.id,
        raw_metadata={"mode": "simulated"},
    )
    await session.commit()

    log_event_panel(
        "SIMULATED CALL STARTED",
        {"From": phone, "Customer": payload.customer_name or "-", "Call": str(call.id)},
        style="magenta",
        icon_key="call_start",
    )

    gemini = GeminiLiveClient()
    await gemini.connect(call.id.hex, JURINEX_PREETI_SYSTEM_PROMPT)

    transcript_svc = TranscriptService(session)
    ticket_created = False
    ticket_number: str | None = None
    transcript: list[dict] = []
    detected_language: str | None = None

    for turn in payload.messages:
        await transcript_svc.save_message(
            call_id=call.id,
            speaker=Speaker.customer,
            text=turn,
            language=detected_language,
        )
        transcript.append({"speaker": "customer", "text": turn})
        await gemini.send_text(turn)

        # Pull whatever events the simulator queued for this turn.
        while not gemini._inbox.empty():  # type: ignore[attr-defined]
            event = await gemini._inbox.get()  # type: ignore[attr-defined]
            if event.type == "text" and event.text:
                if "Hindi" in event.text or "हिंदी" in event.text or "हिन्दी" in event.text:
                    detected_language = "Hindi"
                elif "Marathi" in event.text or "मराठी" in event.text:
                    detected_language = "Marathi"
                elif "English" in event.text:
                    detected_language = "English"
                await transcript_svc.save_message(
                    call_id=call.id,
                    speaker=Speaker.agent,
                    text=event.text,
                    language=detected_language,
                )
                transcript.append({"speaker": "agent", "text": event.text})
            elif event.type == "tool_call" and event.tool_name == "create_support_ticket":
                args = dict(event.tool_args or {})
                args.setdefault("phone_number", phone)
                args.setdefault("customer_name", payload.customer_name)
                args.setdefault("language", detected_language or "English")
                tool_input = CreateSupportTicketInput(**args)
                tool_result = await dispatch_tool_call(
                    session=session,
                    call_id=call.id,
                    tool_name="create_support_ticket",
                    arguments=tool_input.model_dump(),
                )
                if tool_result.get("success"):
                    ticket_created = True
                    ticket_number = tool_result.get("ticket_number")
                transcript.append({"speaker": "tool", "text": str(tool_result)})

    await gemini.close()

    duration = int((utcnow() - call.started_at).total_seconds())
    await CallRepository(session).update_status(
        call.id,
        status=CallStatus.completed,
        ended_at=utcnow(),
        duration_seconds=duration,
        language=detected_language,
        resolution_status=(
            ResolutionStatus.resolved if ticket_created else ResolutionStatus.unknown
        ),
        summary=f"Simulated conversation: {len(payload.messages)} customer turn(s).",
    )
    await session.commit()

    log_event_panel(
        "SIMULATED CALL ENDED",
        {
            "Call": str(call.id),
            "Turns": len(transcript),
            "Ticket": ticket_number or "-",
        },
        style="green",
        icon_key="call_end",
    )

    return SimulateConversationResponse(
        call_id=str(call.id),
        transcript=transcript,
        ticket_created=ticket_created,
        ticket_number=ticket_number,
    )
