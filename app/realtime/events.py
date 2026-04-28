"""Provider-neutral event types passed between Gemini and the call session."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

EventType = Literal[
    "session_open",
    "audio",
    "text",
    "input_transcript",   # caller's speech transcribed by Gemini
    "output_transcript",  # agent's spoken reply transcribed by Gemini
    "tool_call",
    "interrupt",          # caller barged in — flush pending agent audio
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
    tool_call_id: str | None = None  # FunctionCall.id — needed by send_tool_response
    raw: dict[str, Any] | None = None
    error: str | None = None
