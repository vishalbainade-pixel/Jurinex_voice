"""Provider-neutral event types passed between Gemini and the call session."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

EventType = Literal[
    "session_open",
    "audio",
    "text",
    "tool_call",
    "session_close",
    "error",
]


@dataclass
class GeminiEvent:
    type: EventType
    text: str | None = None
    audio: bytes | None = None
    audio_mime_type: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None
    error: str | None = None
