"""Service helpers for support tickets (thin wrappers around the tool layer)."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import SupportTicketRepository
from app.db.schemas import CreateSupportTicketInput, CreateSupportTicketOutput
from app.tools.ticket_tools import create_support_ticket


class TicketService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        payload: CreateSupportTicketInput,
        *,
        call_id: uuid.UUID | None = None,
    ) -> CreateSupportTicketOutput:
        return await create_support_ticket(self.session, payload, call_id=call_id)

    async def list_recent(self, limit: int = 50):
        return await SupportTicketRepository(self.session).list_recent(limit)
