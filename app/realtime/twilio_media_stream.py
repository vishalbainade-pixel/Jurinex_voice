"""Bridge Twilio Media Streams ↔ Gemini Live ↔ Database."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from app.db.database import session_scope
from app.db.models import CallDirection, CallStatus, Speaker
from app.db.repositories import CallDebugEventRepository, CallRepository
from app.observability.logger import log_dataflow, log_event_panel, log_error
from app.observability.trace_context import new_trace, update_trace
from app.prompts import JURINEX_PREETI_SYSTEM_PROMPT
from app.realtime.audio_codec import (
    Pcm24kToMulaw8k,
    chunk_mulaw_for_twilio,
    decode_twilio_payload,
    encode_twilio_payload,
    mulaw8k_to_pcm16_16k,
)
from app.realtime.events import GeminiEvent
from app.realtime.session_manager import CallSession, session_manager
from app.services.transcript_service import TranscriptService
from app.services.tool_dispatcher import dispatch_tool_call


class TwilioMediaStreamHandler:
    """Owns the FastAPI WebSocket connection to a Twilio Media Stream."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.stream_sid: str | None = None
        self.call_sid: str | None = None
        self.session: CallSession | None = None
        self._gemini_task: asyncio.Task | None = None
        # Stateful resampler for the Gemini→Twilio direction (24k PCM → 8k μ-law)
        self._out_resampler = Pcm24kToMulaw8k()
        # Mic-side buffer: batch ~100ms of PCM16/16k per send to Gemini.
        # 16000 Hz × 2 bytes × 0.1s = 3200 bytes per chunk. Sending every
        # 20ms (50/s) overwhelms the Live websocket and causes ping timeouts.
        self._mic_buffer: bytearray = bytearray()
        self._mic_buffer_threshold: int = 3200

    # ------------------------------------------------------------------
    # Top-level handler
    # ------------------------------------------------------------------

    async def handle(self) -> None:
        await self.websocket.accept()
        new_trace(direction="inbound")
        log_dataflow("twilio.websocket.accepted", "media stream websocket accepted")

        try:
            await self._loop()
        except WebSocketDisconnect:
            log_dataflow("twilio.websocket.disconnected", "twilio closed websocket")
        except Exception as exc:  # pragma: no cover - safety net
            log_error("MEDIA STREAM ERROR", str(exc))
        finally:
            await self._teardown()

    # ------------------------------------------------------------------
    # Twilio event loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        async for raw in self.websocket.iter_text():
            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                log_dataflow("twilio.media.invalid_json", raw[:100], level="warning")
                continue

            event = evt.get("event")
            if event == "connected":
                log_dataflow("twilio.media.connected", "Twilio handshake")
            elif event == "start":
                await self._on_start(evt)
            elif event == "media":
                await self._on_media(evt)
            elif event == "mark":
                log_dataflow("twilio.media.mark", evt.get("mark", {}).get("name", ""))
            elif event == "stop":
                log_dataflow("twilio.media.stop", "stop event received")
                break
            else:
                log_dataflow("twilio.media.unknown", str(event))

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_start(self, evt: dict[str, Any]) -> None:
        start = evt.get("start", {})
        self.stream_sid = start.get("streamSid")
        self.call_sid = start.get("callSid")
        custom_params = start.get("customParameters") or {}
        direction_str = (custom_params.get("direction") or "inbound").lower()
        direction = (
            CallDirection.outbound if direction_str == "outbound" else CallDirection.inbound
        )

        update_trace(call_sid=self.call_sid, direction=direction.value)

        async with session_scope() as session:
            call = await CallRepository(session).create(
                twilio_call_sid=self.call_sid,
                direction=direction,
                customer_phone=custom_params.get("from"),
                twilio_from=custom_params.get("from"),
                twilio_to=custom_params.get("to"),
                raw_metadata={"twilio_start": start},
            )
            await CallDebugEventRepository(session).add(
                event_type="twilio",
                event_stage="media.start",
                message="Twilio media stream started",
                call_id=call.id,
                twilio_call_sid=self.call_sid,
                payload=start,
            )

        self.session = await session_manager.create(
            call_db_id=call.id,
            twilio_call_sid=self.call_sid,
            direction=direction.value,
            customer_phone=custom_params.get("from"),
        )
        log_event_panel(
            "CALL STARTED",
            {
                "Direction": direction.value,
                "Call SID": self.call_sid,
                "Stream SID": self.stream_sid,
                "From": custom_params.get("from") or "-",
                "To": custom_params.get("to") or "-",
                "Session ID": self.session.session_id[:8],
            },
            style="cyan",
            icon_key="call_start",
        )

        await self.session.gemini.connect(self.session.session_id, JURINEX_PREETI_SYSTEM_PROMPT)
        self._gemini_task = asyncio.create_task(self._consume_gemini_events())
        # Note: we used to send a priming `send_client_content` text turn here
        # to make Preeti greet first. Some Live model + audio-modality combos
        # reject pre-audio text content with WS close 1008. The greeting now
        # has to be triggered by the caller's first utterance (or by reinforcing
        # "begin by greeting" inside the system prompt).

    async def _on_media(self, evt: dict[str, Any]) -> None:
        if not self.session:
            return
        payload = evt.get("media", {}).get("payload")
        if not payload:
            return

        mulaw = decode_twilio_payload(payload)
        pcm16_16k = mulaw8k_to_pcm16_16k(mulaw)
        self._mic_buffer.extend(pcm16_16k)

        if len(self._mic_buffer) >= self._mic_buffer_threshold:
            chunk = bytes(self._mic_buffer)
            self._mic_buffer.clear()
            log_dataflow(
                "twilio.media.flush",
                f"flush {len(chunk)}b PCM16/16k → gemini",
                level="debug",
            )
            await self.session.gemini.send_audio(
                chunk, mime_type="audio/pcm;rate=16000"
            )

    # ------------------------------------------------------------------
    # Gemini → Twilio
    # ------------------------------------------------------------------

    async def _consume_gemini_events(self) -> None:
        assert self.session is not None
        async for event in self.session.gemini.receive_events():
            try:
                await self._handle_gemini_event(event)
            except Exception as exc:
                log_error("GEMINI EVENT HANDLER", str(exc))

    async def _handle_gemini_event(self, event: GeminiEvent) -> None:
        assert self.session is not None
        if event.type == "session_open":
            log_dataflow("gemini.session.open", "ready")
            return
        if event.type == "text":
            text = event.text or ""
            log_dataflow("gemini.response.text", text[:160])
            async with session_scope() as session:
                await TranscriptService(session).save_message(
                    call_id=self.session.call_db_id,
                    speaker=Speaker.agent,
                    text=text,
                    language=self.session.language,
                )
            await self._send_text_back_to_twilio(text)
        elif event.type == "audio":
            if event.audio:
                mulaw = self._out_resampler.convert(event.audio)
                for frame in chunk_mulaw_for_twilio(mulaw):
                    await self._send_audio_back_to_twilio(encode_twilio_payload(frame))
        elif event.type == "tool_call":
            await self._handle_tool_call(event)
        elif event.type == "error":
            log_error("GEMINI ERROR", event.error or "unknown")

    async def _handle_tool_call(self, event: GeminiEvent) -> None:
        assert self.session is not None
        log_dataflow(
            "gemini.tool_call",
            f"{event.tool_name}",
            payload=event.tool_args,
        )
        async with session_scope() as session:
            result = await dispatch_tool_call(
                session=session,
                call_id=self.session.call_db_id,
                tool_name=event.tool_name or "",
                arguments=event.tool_args or {},
            )
        # Tell the agent the tool result so it can confirm to caller (simulated here).
        await self.session.gemini.send_text(
            f"Tool {event.tool_name} returned: {json.dumps(result, default=str)[:200]}"
        )

    async def _send_text_back_to_twilio(self, text: str) -> None:
        # Twilio Media Streams don't render text natively; we send a 'mark' event
        # so downstream tooling can correlate. Real audio playback is via 'media'.
        if not self.stream_sid:
            return
        try:
            await self.websocket.send_text(
                json.dumps(
                    {
                        "event": "mark",
                        "streamSid": self.stream_sid,
                        "mark": {"name": f"agent-text:{text[:60]}"},
                    }
                )
            )
        except Exception:
            pass

    async def _send_audio_back_to_twilio(self, payload_b64: str) -> None:
        if not self.stream_sid:
            return
        try:
            await self.websocket.send_text(
                json.dumps(
                    {
                        "event": "media",
                        "streamSid": self.stream_sid,
                        "media": {"payload": payload_b64},
                    }
                )
            )
            log_dataflow("twilio.media.outbound", f"sent {len(payload_b64)}b base64")
        except Exception as exc:
            log_dataflow("twilio.media.outbound_error", str(exc), level="warning")

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def _teardown(self) -> None:
        if self.session:
            # Flush any leftover buffered mic audio so the last utterance
            # isn't dropped on hang-up.
            if self._mic_buffer:
                tail = bytes(self._mic_buffer)
                self._mic_buffer.clear()
                try:
                    await self.session.gemini.send_audio(
                        tail, mime_type="audio/pcm;rate=16000"
                    )
                except Exception:
                    pass

            await self.session.gemini.close()
            if self._gemini_task and not self._gemini_task.done():
                self._gemini_task.cancel()

            # Persist call end + summary
            from app.services.call_service import CallService
            from app.services.summary_service import SummaryService

            async with session_scope() as session:
                if self.session.call_db_id:
                    await CallService(session).mark_completed(
                        self.session.call_db_id,
                    )
                    summary = await SummaryService(session).build_summary(
                        self.session.call_db_id
                    )
                    await CallRepository(session).update_status(
                        self.session.call_db_id,
                        summary=summary,
                        status=CallStatus.completed,
                    )

            log_event_panel(
                "CALL ENDED",
                {
                    "Call SID": self.session.twilio_call_sid,
                    "Session": self.session.session_id[:8],
                },
                style="green",
                icon_key="call_end",
            )
            await session_manager.remove(self.session.session_id)
        try:
            await self.websocket.close()
        except Exception:
            pass
