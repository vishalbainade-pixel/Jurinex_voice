"""In-memory registry of active call sessions (Twilio media stream ↔ Gemini)."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

from app.realtime.gemini_live_client import GeminiLiveClient


@dataclass
class CallSession:
    session_id: str
    call_db_id: uuid.UUID | None = None
    twilio_call_sid: str | None = None
    direction: str = "inbound"
    customer_phone: str | None = None
    language: str | None = None
    gemini: GeminiLiveClient = field(default_factory=GeminiLiveClient)
    closed: bool = False


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, CallSession] = {}
        self._lock = asyncio.Lock()

    async def create(self, *, session_id: str | None = None, **kwargs) -> CallSession:
        sess = CallSession(session_id=session_id or uuid.uuid4().hex, **kwargs)
        async with self._lock:
            self._sessions[sess.session_id] = sess
        return sess

    async def get(self, session_id: str) -> CallSession | None:
        async with self._lock:
            return self._sessions.get(session_id)

    async def remove(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)

    def all(self) -> list[CallSession]:
        return list(self._sessions.values())


session_manager = SessionManager()
