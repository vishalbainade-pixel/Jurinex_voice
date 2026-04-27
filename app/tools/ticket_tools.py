"""Support ticket creation tool."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TicketPriority
from app.db.repositories import (
    AgentToolEventRepository,
    CustomerRepository,
    SupportTicketRepository,
)
from app.db.schemas import CreateSupportTicketInput, CreateSupportTicketOutput
from app.observability.logger import log_dataflow, log_event_panel
from app.utils.phone import normalize_e164


async def create_support_ticket(
    session: AsyncSession,
    payload: CreateSupportTicketInput,
    *,
    call_id: uuid.UUID | None = None,
) -> CreateSupportTicketOutput:
    customer_id: uuid.UUID | None = None
    phone: str | None = None

    if payload.phone_number:
        try:
            phone = normalize_e164(payload.phone_number)
        except ValueError:
            phone = payload.phone_number  # store as-is, demo mode

    if phone:
        customer, _ = await CustomerRepository(session).get_or_create(
            phone=phone,
            name=payload.customer_name,
            preferred_language=payload.language,
        )
        customer_id = customer.id

    repo = SupportTicketRepository(session)
    ticket_number = await repo.next_ticket_number()
    ticket = await repo.create(
        ticket_number=ticket_number,
        issue_type=payload.issue_type,
        issue_summary=payload.issue_summary,
        priority=TicketPriority(payload.priority),
        customer_id=customer_id,
        call_id=call_id,
    )

    if call_id:
        await AgentToolEventRepository(session).add(
            call_id=call_id,
            tool_name="create_support_ticket",
            input_json=payload.model_dump(),
            output_json={"ticket_number": ticket.ticket_number},
            success=True,
        )

    log_event_panel(
        "TICKET CREATED",
        {
            "Ticket": ticket.ticket_number,
            "Issue": payload.issue_type,
            "Priority": payload.priority,
            "Language": payload.language,
            "Customer": payload.customer_name or "-",
        },
        style="green",
        icon_key="ticket",
    )
    log_dataflow(
        "tool.ticket.create",
        f"ticket {ticket.ticket_number} created",
        payload={"ticket_number": ticket.ticket_number, "issue_type": payload.issue_type},
    )

    return CreateSupportTicketOutput(
        success=True,
        ticket_number=ticket.ticket_number,
        message="Ticket created successfully",
    )
