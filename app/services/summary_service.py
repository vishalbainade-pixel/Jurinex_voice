"""Build a short, deterministic summary of a call from its transcript."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import CallMessageRepository
from app.observability.logger import log_dataflow


class SummaryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def build_summary(self, call_id: uuid.UUID) -> str:
        messages = await CallMessageRepository(self.session).list_for_call(call_id)
        if not messages:
            return "No conversation captured."

        first_customer = next((m.text for m in messages if m.speaker.value == "customer"), None)
        last_agent = next(
            (m.text for m in reversed(messages) if m.speaker.value == "agent"),
            None,
        )
        summary = (
            f"Customer reported: {first_customer or '(no customer message)'}\n"
            f"Agent closed with: {last_agent or '(no agent message)'}\n"
            f"Total turns: {len(messages)}"
        )
        log_dataflow(
            "call.summary.created",
            f"{len(messages)} turns",
            payload={"call_id": str(call_id)},
        )
        return summary
