"""Persist call transcript turns and surface them in logs."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Speaker
from app.db.repositories import CallMessageRepository
from app.observability.logger import log_dataflow


class TranscriptService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save_message(
        self,
        *,
        call_id: uuid.UUID | None,
        speaker: Speaker,
        text: str,
        language: str | None = None,
        raw_payload: dict | None = None,
    ) -> None:
        if not call_id:
            return
        await CallMessageRepository(self.session).add(
            call_id=call_id,
            speaker=speaker,
            text=text,
            language=language,
            raw_payload=raw_payload,
        )
        log_dataflow(
            "db.message.saved",
            f"{speaker.value}: {text[:100]}",
            payload={"language": language},
        )
