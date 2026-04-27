"""Admin / operator routes — outbound dialing + read-only inspection."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.db.repositories import (
    CallDebugEventRepository,
    CallMessageRepository,
    CallRepository,
    SupportTicketRepository,
)
from app.db.schemas import (
    CallSummary,
    DebugEvent,
    OutboundCallRequest,
    OutboundCallResponse,
    TicketSummary,
)
from app.services.call_service import CallService
from app.utils.security import require_admin_api_key

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin_api_key)],
)


@router.post("/outbound-call", response_model=OutboundCallResponse)
async def outbound_call(
    payload: OutboundCallRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OutboundCallResponse:
    try:
        return await CallService(session).place_outbound(payload)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


@router.get("/calls", response_model=list[CallSummary])
async def list_calls(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 50,
) -> list[CallSummary]:
    calls = await CallRepository(session).list_recent(limit=limit)
    return [CallSummary.model_validate(c) for c in calls]


@router.get("/calls/{call_id}")
async def get_call(
    call_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    call = await CallRepository(session).get(call_id)
    if not call:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "call not found")
    messages = await CallMessageRepository(session).list_for_call(call_id)
    return {
        "call": CallSummary.model_validate(call).model_dump(mode="json"),
        "messages": [
            {
                "id": str(m.id),
                "speaker": m.speaker.value,
                "language": m.language,
                "text": m.text,
                "timestamp": m.timestamp.isoformat(),
            }
            for m in messages
        ],
    }


@router.get("/tickets", response_model=list[TicketSummary])
async def list_tickets(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 50,
) -> list[TicketSummary]:
    tickets = await SupportTicketRepository(session).list_recent(limit=limit)
    return [TicketSummary.model_validate(t) for t in tickets]


@router.get("/debug-events", response_model=list[DebugEvent])
async def list_debug_events(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 100,
) -> list[DebugEvent]:
    events = await CallDebugEventRepository(session).list_recent(limit=limit)
    return [DebugEvent.model_validate(e) for e in events]
