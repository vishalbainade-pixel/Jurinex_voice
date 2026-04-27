"""Seed a couple of demo customers + a sample ticket so the admin UI isn't empty."""

from __future__ import annotations

import asyncio

from app.db.database import session_scope
from app.db.models import TicketPriority
from app.db.repositories import CustomerRepository, SupportTicketRepository


async def main() -> None:
    async with session_scope() as session:
        c1, _ = await CustomerRepository(session).get_or_create(
            phone="+919226408823", name="Demo User", preferred_language="Hindi"
        )
        repo = SupportTicketRepository(session)
        ticket_number = await repo.next_ticket_number()
        await repo.create(
            ticket_number=ticket_number,
            issue_type="OTP_NOT_RECEIVED",
            issue_summary="Customer reports OTP not arriving on registered mobile.",
            priority=TicketPriority.high,
            customer_id=c1.id,
        )
    print("✅ seed complete")


if __name__ == "__main__":
    asyncio.run(main())
