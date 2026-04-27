"""Repository layer — all DB queries live here, services consume them."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AgentToolEvent,
    Call,
    CallDebugEvent,
    CallDirection,
    CallMessage,
    CallStatus,
    Customer,
    Escalation,
    EscalationStatus,
    ResolutionStatus,
    Speaker,
    SupportTicket,
    TicketPriority,
    TicketStatus,
)
from app.utils.time_utils import utcnow


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


class CustomerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_phone(self, phone: str) -> Customer | None:
        stmt = select(Customer).where(Customer.phone_number == phone)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_or_create(
        self,
        *,
        phone: str,
        name: str | None = None,
        preferred_language: str | None = None,
    ) -> tuple[Customer, bool]:
        existing = await self.get_by_phone(phone)
        if existing:
            return existing, False
        customer = Customer(
            phone_number=phone,
            name=name,
            preferred_language=preferred_language,
        )
        self.session.add(customer)
        await self.session.flush()
        return customer, True


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------


class CallRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        twilio_call_sid: str | None,
        direction: CallDirection,
        customer_phone: str | None,
        twilio_from: str | None,
        twilio_to: str | None,
        customer_id: uuid.UUID | None = None,
        raw_metadata: dict | None = None,
    ) -> Call:
        call = Call(
            twilio_call_sid=twilio_call_sid,
            direction=direction,
            status=CallStatus.started,
            customer_phone=customer_phone,
            twilio_from=twilio_from,
            twilio_to=twilio_to,
            customer_id=customer_id,
            raw_metadata=raw_metadata,
        )
        self.session.add(call)
        await self.session.flush()
        return call

    async def get(self, call_id: uuid.UUID) -> Call | None:
        return await self.session.get(Call, call_id)

    async def get_by_sid(self, sid: str) -> Call | None:
        stmt = select(Call).where(Call.twilio_call_sid == sid)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def update_status(
        self,
        call_id: uuid.UUID,
        *,
        status: CallStatus | None = None,
        language: str | None = None,
        issue_type: str | None = None,
        resolution_status: ResolutionStatus | None = None,
        summary: str | None = None,
        sentiment: str | None = None,
        ended_at: datetime | None = None,
        duration_seconds: int | None = None,
    ) -> Call | None:
        call = await self.get(call_id)
        if not call:
            return None
        if status is not None:
            call.status = status
        if language is not None:
            call.language = language
        if issue_type is not None:
            call.issue_type = issue_type
        if resolution_status is not None:
            call.resolution_status = resolution_status
        if summary is not None:
            call.summary = summary
        if sentiment is not None:
            call.sentiment = sentiment
        if ended_at is not None:
            call.ended_at = ended_at
        if duration_seconds is not None:
            call.duration_seconds = duration_seconds
        await self.session.flush()
        return call

    async def list_recent(self, limit: int = 50) -> list[Call]:
        stmt = select(Call).order_by(desc(Call.created_at)).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class CallMessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(
        self,
        *,
        call_id: uuid.UUID,
        speaker: Speaker,
        text: str,
        language: str | None = None,
        audio_event_id: str | None = None,
        raw_payload: dict | None = None,
    ) -> CallMessage:
        msg = CallMessage(
            call_id=call_id,
            speaker=speaker,
            text=text,
            language=language,
            audio_event_id=audio_event_id,
            raw_payload=raw_payload,
        )
        self.session.add(msg)
        await self.session.flush()
        return msg

    async def list_for_call(self, call_id: uuid.UUID) -> list[CallMessage]:
        stmt = (
            select(CallMessage)
            .where(CallMessage.call_id == call_id)
            .order_by(CallMessage.timestamp)
        )
        return list((await self.session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------


class SupportTicketRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def next_ticket_number(self) -> str:
        from app.utils.time_utils import date_compact

        prefix = f"JX-{date_compact()}-"
        stmt = (
            select(SupportTicket.ticket_number)
            .where(SupportTicket.ticket_number.like(f"{prefix}%"))
            .order_by(desc(SupportTicket.ticket_number))
            .limit(1)
        )
        last = (await self.session.execute(stmt)).scalar_one_or_none()
        seq = 1
        if last:
            try:
                seq = int(last.split("-")[-1]) + 1
            except ValueError:
                seq = 1
        return f"{prefix}{seq:04d}"

    async def create(
        self,
        *,
        ticket_number: str,
        issue_type: str,
        issue_summary: str,
        priority: TicketPriority = TicketPriority.normal,
        customer_id: uuid.UUID | None = None,
        call_id: uuid.UUID | None = None,
    ) -> SupportTicket:
        ticket = SupportTicket(
            ticket_number=ticket_number,
            issue_type=issue_type,
            issue_summary=issue_summary,
            priority=priority,
            customer_id=customer_id,
            call_id=call_id,
            status=TicketStatus.open,
        )
        self.session.add(ticket)
        await self.session.flush()
        return ticket

    async def list_recent(self, limit: int = 50) -> list[SupportTicket]:
        stmt = select(SupportTicket).order_by(desc(SupportTicket.created_at)).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# Escalations
# ---------------------------------------------------------------------------


class EscalationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        call_id: uuid.UUID,
        reason: str,
        assigned_team: str = "tier-2-support",
        ticket_id: uuid.UUID | None = None,
    ) -> Escalation:
        esc = Escalation(
            call_id=call_id,
            reason=reason,
            assigned_team=assigned_team,
            ticket_id=ticket_id,
            status=EscalationStatus.pending,
        )
        self.session.add(esc)
        await self.session.flush()
        return esc


# ---------------------------------------------------------------------------
# Tool events + debug events
# ---------------------------------------------------------------------------


class AgentToolEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(
        self,
        *,
        call_id: uuid.UUID,
        tool_name: str,
        input_json: dict | None = None,
        output_json: dict | None = None,
        success: bool = True,
        error_message: str | None = None,
    ) -> AgentToolEvent:
        evt = AgentToolEvent(
            call_id=call_id,
            tool_name=tool_name,
            input_json=input_json,
            output_json=output_json,
            success=success,
            error_message=error_message,
        )
        self.session.add(evt)
        await self.session.flush()
        return evt


class CallDebugEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(
        self,
        *,
        event_type: str,
        event_stage: str,
        message: str,
        call_id: uuid.UUID | None = None,
        twilio_call_sid: str | None = None,
        payload: dict | None = None,
    ) -> CallDebugEvent:
        evt = CallDebugEvent(
            call_id=call_id,
            twilio_call_sid=twilio_call_sid,
            event_type=event_type,
            event_stage=event_stage,
            message=message,
            payload=payload,
        )
        self.session.add(evt)
        await self.session.flush()
        return evt

    async def list_recent(self, limit: int = 100) -> list[CallDebugEvent]:
        stmt = select(CallDebugEvent).order_by(desc(CallDebugEvent.created_at)).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())
