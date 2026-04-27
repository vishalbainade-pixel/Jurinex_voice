"""Gemini Live API client with a clean async interface.

Two operating modes:

1. **Real session** (`DEMO_MODE=false` and a Gemini key is set) —
   opens a `client.aio.live.connect(...)` session against the configured
   `GEMINI_MODEL`, with `response_modalities=["AUDIO"]` and the configured
   prebuilt voice (`GEMINI_VOICE`).
2. **Simulator** (`DEMO_MODE=true` or no key) — deterministic Hindi/Marathi/
   English text replies + a synthetic `create_support_ticket` tool call. Useful
   for unit tests and the `/debug/simulate-conversation` endpoint.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from app.config import settings
from app.observability.logger import log_dataflow, log_event_panel
from app.realtime.events import GeminiEvent


class GeminiLiveClient:
    """Async wrapper around the Gemini Live API."""

    def __init__(self) -> None:
        self._session_id: str | None = None
        self._system_prompt: str | None = None
        self._closed: bool = False
        self._inbox: asyncio.Queue[GeminiEvent] = asyncio.Queue()

        # Real-session state
        self._client: Any | None = None
        self._connect_cm: Any | None = None
        self._real_session: Any | None = None
        self._receive_task: asyncio.Task | None = None
        self._real_mode: bool = False
        # Set to True once the live session has closed (e.g. with WS 1008).
        # Subsequent sends become no-ops so we don't spam warnings 50/s.
        self._send_disabled: bool = False
        self._send_disabled_reason: str = ""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, session_id: str, system_prompt: str) -> None:
        self._session_id = session_id
        self._system_prompt = system_prompt

        if not settings.gemini_key or settings.demo_mode:
            log_event_panel(
                "GEMINI SESSION (SIMULATED)",
                {
                    "session_id": session_id[:8],
                    "model": settings.gemini_model,
                    "voice": settings.gemini_voice,
                    "reason": "DEMO_MODE or missing GEMINI_API_KEY",
                },
                style="magenta",
                icon_key="gemini",
            )
            await self._inbox.put(GeminiEvent(type="session_open"))
            return

        try:
            await self._open_real_session(session_id, system_prompt)
        except Exception as exc:
            log_dataflow(
                "gemini.session.create",
                f"failed to open live session: {exc}",
                level="error",
            )
            await self._inbox.put(GeminiEvent(type="error", error=str(exc)))

    async def _open_real_session(self, session_id: str, system_prompt: str) -> None:
        from google import genai
        from google.genai import types

        self._client = genai.Client(api_key=settings.gemini_key)

        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=types.Content(
                parts=[types.Part(text=system_prompt)]
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=settings.gemini_voice
                    )
                )
            ),
        )

        self._connect_cm = self._client.aio.live.connect(
            model=settings.gemini_model, config=config
        )
        self._real_session = await self._connect_cm.__aenter__()
        self._real_mode = True
        self._receive_task = asyncio.create_task(self._receive_loop())

        log_event_panel(
            "GEMINI SESSION STARTED",
            {
                "session_id": session_id[:8],
                "model": settings.gemini_model,
                "voice": settings.gemini_voice,
            },
            style="cyan",
            icon_key="gemini",
        )
        await self._inbox.put(GeminiEvent(type="session_open"))

    def _disable_session(self, reason: str) -> None:
        """Mark the live session as dead — log once, drop further sends."""
        if self._send_disabled:
            return
        self._send_disabled = True
        self._send_disabled_reason = reason
        log_event_panel(
            "GEMINI SESSION DEAD",
            {
                "reason": reason[:200],
                "model": settings.gemini_model,
                "voice": settings.gemini_voice,
                "hint": (
                    "Common causes: (1) API key not authorized for this model; "
                    "(2) voice name not supported by this model; "
                    "(3) model name unavailable to the project. "
                    "Try a stable model like gemini-2.0-flash-live-001 first."
                ),
            },
            style="red",
            icon_key="error",
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()

        if self._connect_cm is not None:
            try:
                await self._connect_cm.__aexit__(None, None, None)
            except Exception:
                pass

        await self._inbox.put(GeminiEvent(type="session_close"))
        log_dataflow("gemini.session.close", "session closed")

    # ------------------------------------------------------------------
    # Send (caller → Gemini)
    # ------------------------------------------------------------------

    async def send_audio(self, audio_bytes: bytes, mime_type: str) -> None:
        if not audio_bytes:
            return

        if not self._real_mode or self._real_session is None:
            return  # simulator ignores audio frames

        if self._send_disabled:
            return  # session is dead — silently drop, error already logged once

        from google.genai import types

        sess = self._real_session
        blob = types.Blob(data=audio_bytes, mime_type=mime_type)

        # google-genai's Live API method name has shifted across versions.
        # Try the newest spelling first, fall back to older ones.
        attempts = (
            ("send_realtime_input", {"audio": blob}),
            ("send_realtime_input", {"media": blob}),
            ("send", {"input": blob, "end_of_turn": False}),
            ("send", {"input": blob}),
        )
        last_err: Exception | None = None
        for method_name, kwargs in attempts:
            method = getattr(sess, method_name, None)
            if method is None:
                continue
            try:
                await method(**kwargs)
                return
            except TypeError as exc:
                last_err = exc  # signature mismatch — try next shape
                continue
            except Exception as exc:
                last_err = exc
                break
        if last_err is not None:
            self._disable_session(f"audio send failed: {last_err}")

    async def send_text(self, text: str) -> None:
        if not text:
            return
        log_dataflow("gemini.text.input", text[:120])

        if not self._real_mode or self._real_session is None:
            await self._simulate_response(text)
            return

        if self._send_disabled:
            return

        from google.genai import types

        sess = self._real_session
        content = types.Content(role="user", parts=[types.Part(text=text)])

        attempts = (
            ("send_client_content", {"turns": [content], "turn_complete": True}),
            ("send_client_content", {"turns": content, "turn_complete": True}),
            ("send", {"input": content, "end_of_turn": True}),
            ("send", {"input": text, "end_of_turn": True}),
        )
        last_err: Exception | None = None
        for method_name, kwargs in attempts:
            method = getattr(sess, method_name, None)
            if method is None:
                continue
            try:
                await method(**kwargs)
                return
            except TypeError as exc:
                last_err = exc
                continue
            except Exception as exc:
                last_err = exc
                break
        if last_err is not None:
            self._disable_session(f"text send failed: {last_err}")
        if last_err is not None:
            log_dataflow("gemini.text.input_error", str(last_err), level="warning")

    # ------------------------------------------------------------------
    # Receive loop (Gemini → caller)
    # ------------------------------------------------------------------

    async def receive_events(self) -> AsyncIterator[GeminiEvent]:
        while not self._closed:
            event = await self._inbox.get()
            if event.type == "session_close":
                break
            yield event

    async def _receive_loop(self) -> None:
        """Drain the live session and translate to GeminiEvent.

        ``session.receive()`` in google-genai >= 1.x yields messages for the
        *current* model turn and then returns. The underlying WS is still
        alive — we have to call ``receive()`` again to listen for the next
        turn. Without this loop the agent would only respond once.
        """
        assert self._real_session is not None
        while not self._closed and not self._send_disabled:
            try:
                async for response in self._real_session.receive():
                    self._extract_response(response)
                log_dataflow(
                    "gemini.turn.complete",
                    "turn done, awaiting next",
                    level="debug",
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_dataflow("gemini.receive_loop_error", str(exc), level="warning")
                await self._inbox.put(GeminiEvent(type="error", error=str(exc)))
                self._disable_session(f"receive failed: {exc}")
                return

    def _extract_response(self, response: Any) -> None:
        # `response.data` and `response.text` already aggregate every
        # inline_data / text part in `server_content.model_turn.parts`.
        # Walking the nested path on top would duplicate every chunk
        # (the "speaking twice" echo).

        data = getattr(response, "data", None)
        if data:
            self._inbox.put_nowait(
                GeminiEvent(
                    type="audio",
                    audio=data,
                    audio_mime_type="audio/pcm;rate=24000",
                )
            )

        text = getattr(response, "text", None)
        if text:
            self._inbox.put_nowait(GeminiEvent(type="text", text=text))

        tool_call = getattr(response, "tool_call", None)
        if tool_call and getattr(tool_call, "function_calls", None):
            for fc in tool_call.function_calls:
                self._inbox.put_nowait(
                    GeminiEvent(
                        type="tool_call",
                        tool_name=getattr(fc, "name", "") or "",
                        tool_args=dict(getattr(fc, "args", {}) or {}),
                    )
                )

    # ------------------------------------------------------------------
    # Simulator (DEMO_MODE / no key)
    # ------------------------------------------------------------------

    async def _simulate_response(self, user_text: str) -> None:
        text = user_text.strip().lower()
        reply: str
        tool_call: tuple[str, dict[str, Any]] | None = None

        if any(t in text for t in ("hindi", "हिंदी", "हिन्दी")):
            reply = (
                "ठीक है, मैं आपकी मदद Hindi में करूँगी। "
                "कृपया अपनी समस्या बताइए।"
            )
        elif "marathi" in text or "मराठी" in text:
            reply = (
                "ठीक आहे, मी तुम्हाला Marathi मध्ये मदत करते. "
                "कृपया तुमची समस्या सांगा."
            )
        elif "english" in text:
            reply = "Sure, I'll continue in English. Please tell me how I can help you today."
        elif any(k in text for k in ("otp", "ओटीपी", "ओटीपि", "ओटीपी नहीं")):
            reply = (
                "मुझे खेद है कि आपको OTP नहीं मिल रहा है। "
                "मैं आपके लिए एक support ticket बना देती हूँ ताकि team जल्दी से इसे देख सके।"
            )
            tool_call = (
                "create_support_ticket",
                {
                    "issue_type": "OTP_NOT_RECEIVED",
                    "issue_summary": "Customer reports OTP is not being received.",
                    "priority": "high",
                    "language": "Hindi",
                },
            )
        elif any(k in text for k in ("ticket बना", "create ticket", "बना दीजिए")):
            reply = "जी ज़रूर, मैं अभी आपके लिए ticket बना देती हूँ।"
            tool_call = (
                "create_support_ticket",
                {
                    "issue_type": "GENERAL_SUPPORT",
                    "issue_summary": "Customer requested ticket creation during call.",
                    "priority": "normal",
                    "language": "Hindi",
                },
            )
        else:
            reply = (
                "मैंने आपकी बात समझी। क्या आप थोड़ा और विस्तार से बता सकते हैं "
                "ताकि मैं सही तरीके से मदद कर सकूँ?"
            )

        await self._inbox.put(GeminiEvent(type="text", text=reply))
        if tool_call:
            name, args = tool_call
            await self._inbox.put(
                GeminiEvent(type="tool_call", tool_name=name, tool_args=args)
            )
