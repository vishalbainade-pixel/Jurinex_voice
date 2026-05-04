"""Bridge Twilio Media Streams ↔ Gemini Live ↔ Database."""

from __future__ import annotations

import asyncio
import html
import json
import time
import uuid
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from app.config import settings
from app.db.database import session_scope
from app.db.models import CallDirection, CallStatus, Speaker
from app.db.platform_voices_repository import PlatformVoicesRepository
from app.db.prompt_fragments_repository import PromptFragmentsRepository
from app.db.repositories import CallDebugEventRepository, CallRepository
from app.db.voice_agent_repository import AgentBundle, VoiceAgentRepository
from app.db.voice_debug_events_repository import VoiceDebugEventsRepository
from app.observability.logger import log_dataflow, log_event_panel, log_error
from app.observability.trace_context import new_trace, update_trace
from app.prompts import JURINEX_PREETI_SYSTEM_PROMPT
from app.services.system_instruction_builder import (
    AssembledInstruction,
    SystemInstructionBuilder,
)
from app.realtime.audio_codec import (
    Pcm24kToMulaw8k,
    chunk_mulaw_for_twilio,
    decode_twilio_payload,
    encode_twilio_payload,
    mulaw8k_to_pcm16_16k,
)
from app.realtime.call_recorder import CallRecorder
from app.realtime.events import GeminiEvent
from app.realtime.session_manager import CallSession, session_manager
from app.services.call_service import CallService
from app.services.gcs_uploader import upload_call_recording
from app.services.transcript_service import TranscriptService
from app.services.tool_dispatcher import dispatch_tool_call
from app.utils.time_utils import utcnow


class TwilioMediaStreamHandler:
    """Owns the FastAPI WebSocket connection to a Twilio Media Stream."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.stream_sid: str | None = None
        self.call_sid: str | None = None
        self.session: CallSession | None = None
        self._gemini_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        # Stateful resampler for the Gemini→Twilio direction (24k PCM → 8k μ-law)
        self._out_resampler = Pcm24kToMulaw8k()
        # Mic-side buffer: batch ~100ms of PCM16/16k per send to Gemini.
        # 16000 Hz × 2 bytes × 0.1s = 3200 bytes per chunk. Sending every
        # 20ms (50/s) overwhelms the Live websocket and causes ping timeouts.
        self._mic_buffer: bytearray = bytearray()
        self._mic_buffer_threshold: int = 3200
        # Watchdog state — silence + max-duration enforcement.
        self._call_start_ts: float = 0.0
        self._last_mic_activity_ts: float = 0.0
        self._terminating: bool = False
        self._terminate_reason: str = ""
        # Optional in-memory call recorder; configured in _on_start once we
        # know the call_sid + start time.
        self.recorder: CallRecorder | None = None

        # True while the static greeting WAV is being streamed to Twilio.
        # During this window we do NOT forward caller audio to Gemini —
        # otherwise if the caller says anything ("hello", etc.), Gemini
        # generates a response that overlaps with the greeting playback.
        self._greeting_playing: bool = False

        # Shadow-RAG state. The Live model can't be relied on to call
        # search_knowledge_base itself, so we run KB search on every
        # caller utterance and inject top chunks back into Gemini's context.
        self._caller_transcript_buf: str = ""
        self._kb_inject_task: asyncio.Task | None = None
        self._last_injected_query: str = ""
        # If the model just called a tool itself (real RAG), skip shadow-RAG
        # for a window so we don't double-search on a partial transcript.
        self._tool_called_at: float = 0.0
        self._tool_quiet_window_seconds: float = 5.0
        # Last time we saw an output_transcript fragment from Gemini. While
        # this is recent, the model is actively speaking — injecting a
        # shadow-RAG prime now would restart its turn ("speaks 3 words then
        # restarts from word 1" pattern, especially during the transfer pitch).
        self._last_output_transcript_at: float = 0.0
        self._model_speaking_quiet_window_seconds: float = 1.5

        # Admin-driven config for THIS call. Resolved in _on_start by joining
        # voice_agents + voice_agent_configurations + voice_agent_transfer_configs.
        # Held on the handler so transfer / KB-shadow paths can read transfer
        # destination, language, max_duration, etc. without re-querying.
        self.bundle: AgentBundle | None = None
        self.assembled: AssembledInstruction | None = None
        # voice_call_schedules linkage — populated when the call originated
        # from the scheduler poller (Twilio Stream Parameter ``schedule_id``).
        # The teardown writes the schedule row's terminal status accordingly.
        self.schedule_id: "uuid.UUID | None" = None
        # Per-call thresholds derived from the bundle (admin call settings),
        # falling back to .env defaults when the bundle is missing.
        self._effective_max_seconds: int = settings.max_call_duration_seconds
        self._effective_silence_seconds: int = settings.silence_timeout_seconds

    # ------------------------------------------------------------------
    # Top-level handler
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # voice_debug_events helper (fire-and-forget — never blocks realtime)
    # ------------------------------------------------------------------

    def _debug_event(
        self,
        *,
        event_type: str,
        message: str,
        event_stage: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Schedule one ``voice_debug_events`` write without awaiting it.

        Caller-facing latency must NOT depend on this audit table being up.
        """

        async def _write() -> None:
            try:
                async with session_scope() as session:
                    await VoiceDebugEventsRepository(session).emit(
                        event_type=event_type,
                        message=message,
                        event_stage=event_stage,
                        trace_id=(self.session.session_id if self.session else None),
                        agent_id=(self.bundle.id if self.bundle else None),
                        payload=payload,
                    )
            except Exception as exc:
                log_dataflow(
                    "debug_event.write_error",
                    f"failed to persist {event_type}/{event_stage}: {exc}",
                    level="warning",
                )

        try:
            asyncio.create_task(_write())
        except RuntimeError:
            pass  # event loop closed mid-shutdown — drop on the floor

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
        import uuid as _uuid

        from app.realtime.gemini_live_client import GeminiLiveClient as _GeminiLiveClient
        from app.realtime.greeting_loader import get_greeting_mulaw

        start = evt.get("start", {})
        self.stream_sid = start.get("streamSid")
        self.call_sid = start.get("callSid")
        custom_params = start.get("customParameters") or {}
        direction_str = (custom_params.get("direction") or "inbound").lower()
        direction = (
            CallDirection.outbound if direction_str == "outbound" else CallDirection.inbound
        )

        update_trace(call_sid=self.call_sid, direction=direction.value)

        # ── Step 1. Pick the voice agent for THIS call.
        # Routing precedence (highest first):
        #   1. ``customParameters.agent_name`` from Twilio — admin sets it per
        #      phone number / Studio flow, so a single bridge can serve many
        #      voice agents (e.g. +91…1 → 'preeti', +91…2 → 'rohit_sales').
        #   2. ``settings.kb_agent_name`` (env default — single-agent mode).
        requested_agent = (
            (custom_params.get("agent_name") or "").strip()
            or settings.kb_agent_name
        )
        log_dataflow(
            "agent.routing.selected",
            f"requested={requested_agent!r} "
            f"source={'twilio_param' if custom_params.get('agent_name') else 'env'}",
        )

        # ── Scheduler linkage — when this call was placed by the outbound
        # poller, ``schedule_id`` arrives as a Stream parameter. Stash it so
        # the teardown can mark the voice_call_schedules row complete/failed.
        schedule_id_str = (custom_params.get("schedule_id") or "").strip()
        if schedule_id_str:
            try:
                self.schedule_id = uuid.UUID(schedule_id_str)
                log_dataflow(
                    "scheduler.bridge.linked",
                    f"schedule_id={self.schedule_id} call_sid={self.call_sid}",
                )
            except (TypeError, ValueError):
                log_dataflow(
                    "scheduler.bridge.bad_id",
                    f"schedule_id={schedule_id_str!r} is not a valid UUID — ignoring",
                    level="warning",
                )

        # ── Step 2. Load the agent bundle from the admin DB, then assemble
        # the live system instruction from the active fragments + tool prompts.
        # If the bundle can't be found OR the agent is inactive, fall back to
        # the static prompt file so the call still completes (degraded mode).
        async with session_scope() as session:
            self.bundle = await VoiceAgentRepository(session).load_active_bundle(
                requested_agent
            )
            if self.bundle is not None:
                builder = SystemInstructionBuilder(PromptFragmentsRepository(session))
                self.assembled = await builder.build(self.bundle)

        bundle = self.bundle
        assembled = self.assembled

        # Resolve effective model / voice / temperature for this call.
        if bundle is not None and bundle.live_model:
            live_model = bundle.live_model
        else:
            live_model = settings.gemini_model
        if bundle is not None and bundle.voice_name:
            voice_name = bundle.voice_name
        else:
            voice_name = settings.gemini_voice
        temperature = float(bundle.temperature) if bundle is not None else None

        # Validate the requested voice against the admin-curated catalogue
        # (platform_voices). An unknown voice silently kills the live session
        # with WS 1008 a few seconds in — fail loud here instead, fall back
        # to a known-good voice so the call still completes.
        _FALLBACK_VOICE = "Aoede"
        try:
            async with session_scope() as voice_session:
                voices_repo = PlatformVoicesRepository(voice_session)
                catalogue = await voices_repo.active_voices()
            if catalogue and voice_name not in catalogue:
                log_dataflow(
                    "voice.validation.failed",
                    f"voice {voice_name!r} not in platform_voices catalogue "
                    f"(active count={len(catalogue)}). Falling back to "
                    f"{_FALLBACK_VOICE!r}. Sample of valid: "
                    f"{sorted(list(catalogue))[:8]}",
                    level="error",
                )
                voice_name = _FALLBACK_VOICE
            elif catalogue:
                log_dataflow(
                    "voice.validation.ok",
                    f"voice={voice_name} confirmed in platform_voices "
                    f"(catalogue size={len(catalogue)})",
                    level="debug",
                )
        except Exception as exc:
            # Catalogue lookup failure shouldn't block the call.
            log_dataflow(
                "voice.validation.error",
                f"could not load platform_voices: {exc} — proceeding with "
                f"unvalidated voice={voice_name}",
                level="warning",
            )

        # Tool gating from the admin builder. Auto-include search_knowledge_base
        # when KB documents are selected (the admin doesn't expose it as a
        # toggle). end_call is always available — let the model end gracefully.
        if bundle is not None:
            enabled_tool_names: set[str] = set(bundle.enabled_function_keys)
            if bundle.knowledge_base_settings.get("document_ids"):
                enabled_tool_names.add("search_knowledge_base")
            enabled_tool_names.add("end_call")
        else:
            enabled_tool_names = set()

        # ── Step 2. Decide once whether we're going to pre-play the greeting
        # WAV. Pre-playing changes the system prompt (suppress Turn 1) AND
        # whether we prime Gemini to speak first (we don't, when greeting is
        # pre-played).
        greeting_mulaw = (
            get_greeting_mulaw() if settings.eager_greeting_enabled else None
        )
        greeting_will_play = greeting_mulaw is not None

        # ── Step 3. Build the final system prompt: persona + fragments + tools
        # from the DB, plus a runtime override when the static greeting will
        # cover Turn 1.
        if assembled is not None:
            base_prompt = assembled.text
            log_dataflow(
                "prompt.source",
                f"db-driven (bundle={bundle.name} chars={len(base_prompt)})",
            )
        else:
            base_prompt = JURINEX_PREETI_SYSTEM_PROMPT
            log_dataflow(
                "prompt.source",
                "static fallback (bundle missing/inactive)",
                level="warning",
            )

        if greeting_will_play:
            sys_prompt = base_prompt + (
                "\n\n=== RUNTIME OVERRIDE FOR THIS CALL ===\n"
                "A pre-recorded Hindi greeting is being played to the caller "
                "RIGHT NOW by another channel. It introduces you as Preeti "
                "and asks the caller to choose a language. So:\n"
                "- DO NOT speak Turn 1. SKIP IT ENTIRELY.\n"
                "- DO NOT say 'नमस्ते', 'Hello', 'Jurinex support', or "
                "'मैं Preeti बोल रही हूँ'. The caller is already hearing this.\n"
                "- Stay completely silent until the caller speaks to you.\n"
                "- Your FIRST utterance must be Turn 2 — a brief acknowledgement "
                "  of the caller's language pick and 'how can I help?'.\n"
                "Speaking the greeting now will create double audio overlapping "
                "with the recorded greeting. This is forbidden.\n"
            )
        else:
            sys_prompt = base_prompt

        # ── Step 4. Pre-warm: open the Gemini Live WS in parallel with the DB
        # writes. Cold-start network handshake is the slowest part of "first
        # speak" latency; running it concurrently shaves ~200-400 ms.
        pre_session_id = _uuid.uuid4().hex
        gemini = _GeminiLiveClient()
        gemini.on_session_dead = self._on_gemini_session_dead
        connect_task = asyncio.create_task(
            gemini.connect(
                pre_session_id,
                sys_prompt,
                live_model=live_model,
                voice_name=voice_name,
                temperature=temperature,
                enabled_tool_names=enabled_tool_names or None,
            )
        )

        async with session_scope() as session:
            call = await CallRepository(session).create(
                twilio_call_sid=self.call_sid,
                direction=direction,
                customer_phone=custom_params.get("from"),
                twilio_from=custom_params.get("from"),
                twilio_to=custom_params.get("to"),
                raw_metadata={
                    "twilio_start": start,
                    "agent_bundle": (
                        {
                            "id": str(bundle.id),
                            "name": bundle.name,
                            "live_model": live_model,
                            "voice_name": voice_name,
                            "voice_tag": bundle.voice_tag,
                            "tools_enabled": sorted(enabled_tool_names),
                            "languages": bundle.languages,
                        }
                        if bundle is not None
                        else None
                    ),
                },
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
            session_id=pre_session_id,  # match the id we pre-warmed Gemini with
            call_db_id=call.id,
            twilio_call_sid=self.call_sid,
            direction=direction.value,
            customer_phone=custom_params.get("from"),
            gemini=gemini,  # pre-warmed client
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
        self._debug_event(
            event_type="bridge",
            event_stage="call.started",
            message=(
                f"{direction.value} call to {custom_params.get('to') or '-'} "
                f"from {custom_params.get('from') or '-'} "
                f"agent={bundle.name if bundle else '(static fallback)'} "
                f"model={live_model} voice={voice_name}"
            ),
            payload={
                "call_sid": self.call_sid,
                "stream_sid": self.stream_sid,
                "agent": bundle.name if bundle else None,
                "live_model": live_model,
                "voice_name": voice_name,
                "tools": sorted(enabled_tool_names),
                "greeting_will_play": greeting_will_play,
            },
        )

        # Per-call audio recorder (uploaded to GCS at teardown)
        self.recorder = CallRecorder(
            call_sid=self.call_sid,
            started_at=utcnow(),
            enabled=settings.gcs_recordings_enabled and bool(settings.gcs_bucket),
        )
        if self.recorder.enabled:
            log_dataflow(
                "recorder.armed",
                f"recording → gs://{settings.gcs_bucket}/{self.recorder.gcs_folder()}/",
            )

        # Stream the pre-loaded greeting WAV back to Twilio NOW, in parallel
        # with the Gemini connect. By the time the greeting finishes (~10 s),
        # the Live session is fully warm and Preeti can respond instantly.
        # `greeting_will_play` was decided up top so the system prompt could
        # also be patched to suppress Gemini's own greeting (avoids overlap).
        if greeting_will_play:
            from app.realtime.greeting_loader import get_greeting_duration

            asyncio.create_task(self._play_greeting_via_stream(greeting_mulaw))
            log_dataflow(
                "greeting.play.scheduled",
                f"streaming {len(greeting_mulaw)}b μ-law "
                f"({get_greeting_duration():.2f}s) via Twilio WS",
            )

        # Wait for the pre-warmed Gemini handshake to finish (often already done).
        await connect_task
        self._gemini_task = asyncio.create_task(self._consume_gemini_events())

        # Watchdog start — silence + max-duration (A + B). When the admin
        # bundle specifies call.max_duration_minutes / end_on_silence_minutes,
        # those win over the .env defaults.
        max_seconds = settings.max_call_duration_seconds
        silence_seconds = settings.silence_timeout_seconds
        if bundle is not None:
            cs = bundle.call_settings
            md = cs.get("max_duration_minutes")
            if md:
                max_seconds = int(float(md) * 60)
            ssm = cs.get("end_on_silence_minutes")
            if ssm:
                silence_seconds = int(float(ssm) * 60)
        self._effective_max_seconds = max_seconds
        self._effective_silence_seconds = silence_seconds

        now = time.monotonic()
        self._call_start_ts = now
        self._last_mic_activity_ts = now
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        log_dataflow(
            "watchdog.armed",
            f"silence={silence_seconds}s max={max_seconds}s "
            f"auto_hangup_on_gemini_failure={settings.auto_hangup_on_gemini_failure}",
        )

        # Make Preeti speak first ONLY when we are NOT pre-playing a greeting.
        # When the static WAV is being streamed, priming Gemini would cause
        # her to generate her own greeting in parallel → echo / double audio.
        # In that case we leave Gemini silent; she'll speak when the caller
        # responds to the recorded greeting.
        if not greeting_will_play:
            await self.session.gemini.prime(
                "[The phone call has just been answered. Speak ONLY the Hindi "
                "opening greeting now and ask which language the caller would "
                "prefer (English, Hindi, or Marathi). Do NOT recite the English "
                "or Marathi versions of the greeting at this point.]"
            )
        else:
            log_dataflow(
                "gemini.prime.skipped",
                "eager greeting is being streamed — Gemini stays silent until caller speaks",
            )

    # Above this PCM16 RMS we consider the caller to be speaking. Twilio
    # sends μ-law frames continuously, so we can't gate on "frame received";
    # we have to look at the actual sample energy.
    _SPEECH_RMS_THRESHOLD = 500

    async def _on_media(self, evt: dict[str, Any]) -> None:
        if not self.session:
            return
        payload = evt.get("media", {}).get("payload")
        if not payload:
            return

        mulaw = decode_twilio_payload(payload)
        pcm16_16k = mulaw8k_to_pcm16_16k(mulaw)

        # Tap caller audio for recording (raw μ-law, decoded later)
        if self.recorder is not None:
            self.recorder.add_caller_audio(mulaw)

        # Update silence watchdog only on actual speech energy.
        try:
            import audioop  # noqa: PLC0415  deprecated in 3.13, fine on 3.12

            if audioop.rms(pcm16_16k, 2) > self._SPEECH_RMS_THRESHOLD:
                self._last_mic_activity_ts = time.monotonic()
        except Exception:
            self._last_mic_activity_ts = time.monotonic()

        # While the greeting WAV is still streaming to the caller, suppress
        # forwarding to Gemini. Otherwise a caller "hello" mid-greeting would
        # trigger a Gemini reply that overlaps with the greeting playback.
        # Audio is still recorded + counted by the silence watchdog above.
        if self._greeting_playing:
            self._mic_buffer.clear()  # don't accumulate stale frames
            return

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
        elif event.type == "input_transcript":
            text = event.text or ""
            log_dataflow("gemini.transcript.input", text[:160])
            async with session_scope() as session:
                await TranscriptService(session).save_message(
                    call_id=self.session.call_db_id,
                    speaker=Speaker.customer,
                    text=text,
                    language=self.session.language,
                )
            # Shadow-RAG: accumulate caller transcript and schedule a
            # debounced KB search; results get injected back into Gemini.
            self._caller_transcript_buf += " " + text
            self._schedule_kb_inject()
        elif event.type == "output_transcript":
            text = event.text or ""
            log_dataflow("gemini.transcript.output", text[:160])
            # Mark that the model is currently speaking — used by shadow-RAG
            # to avoid injecting a prime() mid-turn (which would restart it).
            self._last_output_transcript_at = time.monotonic()
            async with session_scope() as session:
                await TranscriptService(session).save_message(
                    call_id=self.session.call_db_id,
                    speaker=Speaker.agent,
                    text=text,
                    language=self.session.language,
                )
        elif event.type == "audio":
            if event.audio:
                # Tap agent audio for recording (raw 24kHz PCM16)
                if self.recorder is not None:
                    self.recorder.add_agent_audio(event.audio)

                mulaw = self._out_resampler.convert(event.audio)
                for frame in chunk_mulaw_for_twilio(mulaw):
                    await self._send_audio_back_to_twilio(encode_twilio_payload(frame))
        elif event.type == "tool_call":
            await self._handle_tool_call(event)
        elif event.type == "interrupt":
            await self._handle_interrupt()
        elif event.type == "error":
            log_error("GEMINI ERROR", event.error or "unknown")

    async def _play_greeting_via_stream(self, mulaw_audio: bytes) -> None:
        """Stream the cached greeting μ-law buffer to Twilio in 20ms frames.

        Paced at 20ms per frame so Twilio doesn't drop frames from a too-fast
        burst. This runs in parallel with the Gemini connect, so by the time
        the caller hears the end of the greeting, Preeti is fully ready.
        """
        if not self.stream_sid:
            return
        from app.realtime.audio_codec import (
            TWILIO_FRAME_BYTES,
            chunk_mulaw_for_twilio,
            encode_twilio_payload,
        )

        frames = chunk_mulaw_for_twilio(mulaw_audio)
        log_dataflow(
            "greeting.play.start", f"frames={len(frames)} (20ms each)"
        )
        # Pace ~20ms per frame so we don't blast Twilio's buffer.
        # Slightly under 20 to account for send overhead.
        FRAME_INTERVAL = 0.018
        self._greeting_playing = True
        try:
            for i, frame in enumerate(frames):
                if not self.stream_sid:
                    break  # call torn down
                payload = encode_twilio_payload(frame)
                try:
                    await self.websocket.send_text(
                        json.dumps(
                            {
                                "event": "media",
                                "streamSid": self.stream_sid,
                                "media": {"payload": payload},
                            }
                        )
                    )
                except Exception as exc:
                    log_dataflow(
                        "greeting.play.send_error",
                        str(exc),
                        level="warning",
                    )
                    return
                await asyncio.sleep(FRAME_INTERVAL)
            log_dataflow("greeting.play.done", f"streamed {len(frames)} frames")
        except asyncio.CancelledError:
            raise
        finally:
            # Greeting finished (or errored) — caller audio can now flow to Gemini.
            self._greeting_playing = False
            log_dataflow(
                "greeting.play.flag_cleared",
                "caller mic now forwarded to Gemini",
            )

    async def _handle_interrupt(self) -> None:
        """Caller barged in. Flush Twilio's playback buffer so we don't keep
        speaking over them, and reset the audio resampler state."""
        log_dataflow("twilio.media.interrupt", "caller barged in — flushing playback")
        # Send Twilio a `clear` event — discards any media we already pushed
        # but Twilio hasn't played yet. Without this the caller hears Preeti
        # finish her previous sentence before going silent.
        if self.stream_sid:
            try:
                await self.websocket.send_text(
                    json.dumps(
                        {"event": "clear", "streamSid": self.stream_sid}
                    )
                )
            except Exception as exc:
                log_dataflow(
                    "twilio.media.clear_error", str(exc), level="warning"
                )
        # Reset the 24k→8k resampler so the next response doesn't start mid-sample.
        self._out_resampler = Pcm24kToMulaw8k()

    # ------------------------------------------------------------------
    # Shadow RAG — proactively feed KB chunks into Gemini's context
    # ------------------------------------------------------------------

    def _schedule_kb_inject(self) -> None:
        """Cancel any pending KB inject and schedule a fresh one (debounced)."""
        if not settings.kb_shadow_enabled:
            return  # shadow path disabled — model's own tool calls handle RAG
        if self._kb_inject_task and not self._kb_inject_task.done():
            self._kb_inject_task.cancel()
        self._kb_inject_task = asyncio.create_task(self._run_kb_inject())

    async def _run_kb_inject(self) -> None:
        """Wait briefly so multiple transcript fragments coalesce, then search."""
        try:
            await asyncio.sleep(0.6)  # debounce window
        except asyncio.CancelledError:
            return

        if not self.session or not self.session.gemini:
            return
        # Stand down if the model just ran its own search — no need to
        # double-search and re-inject on a partial transcript fragment.
        now = time.monotonic()
        since_tool = now - self._tool_called_at
        if self._tool_called_at and since_tool < self._tool_quiet_window_seconds:
            log_dataflow(
                "kb.shadow.skipped",
                f"model just called a tool {since_tool:.1f}s ago — standing down",
                level="debug",
            )
            self._caller_transcript_buf = ""  # reset so we don't accumulate stale text
            return
        # Stand down if the model is currently speaking. Sending a prime()
        # while it's mid-turn would restart the turn (caller hears: "मैं आपको
        # connect कर..." → kill → "मैं आपको connect कर..." → kill → loop).
        # This is most visible during the long transfer pitch.
        since_speech = now - self._last_output_transcript_at
        if (
            self._last_output_transcript_at
            and since_speech < self._model_speaking_quiet_window_seconds
        ):
            log_dataflow(
                "kb.shadow.skipped",
                f"model is currently speaking ({since_speech:.2f}s since last "
                f"transcript) — standing down so we don't restart the turn",
                level="debug",
            )
            self._caller_transcript_buf = ""
            return
        query = self._caller_transcript_buf.strip()
        # Skip very short / trivial replies — saves embedding cost and avoids
        # injecting context for "हाँ" / "ok" / "no" turns.
        if len(query) < 12:
            return
        # Skip duplicate-ish queries (caller's still saying the same thing).
        if query == self._last_injected_query:
            return
        self._last_injected_query = query
        # Reset the buffer so the next utterance starts fresh.
        self._caller_transcript_buf = ""

        try:
            from app.services.kb_search import KbSearchService

            async with session_scope() as session:
                result = await KbSearchService(session).search(
                    query=query,
                    k=3,
                    call_id=self.session.call_db_id,
                )
        except Exception as exc:
            log_dataflow("kb.shadow.error", str(exc), level="warning")
            return

        results = result.get("results") or []
        if not results:
            return
        # Shadow uses its own (lower) threshold because the caller's raw
        # Hindi/Marathi transcript embeds further from English-indexed chunks
        # than a clean English paraphrase would. The main flow still uses
        # KB_MIN_SCORE for the model-issued tool path.
        top_score = float(result.get("top_score") or 0.0)
        if top_score < settings.kb_shadow_min_score:
            log_dataflow(
                "kb.shadow.low_confidence",
                f"top_score={top_score:.3f} < shadow_threshold={settings.kb_shadow_min_score:.2f}",
                level="debug",
            )
            return

        # Build a compact, model-friendly context block.
        chunks_text = "\n".join(
            f"- [{r['document_title']}{(' › ' + r['heading_path']) if r.get('heading_path') else ''}] "
            f"{r['text'].strip()}"
            for r in results[:3]
        )
        priming = (
            "[KB_CONTEXT — ground your NEXT spoken reply ONLY in these chunks. "
            "Speak in the caller's chosen language. If a chunk doesn't cover "
            "the question, briefly say so and offer to connect to support. "
            "Do NOT read this block aloud.]\n"
            f"{chunks_text}"
        )
        primed = await self.session.gemini.prime(priming)
        log_dataflow(
            "kb.shadow.injected",
            f"primed={primed} chunks={len(results)} "
            f"top_score={result.get('top_score'):.3f}",
        )

    async def _handle_tool_call(self, event: GeminiEvent) -> None:
        assert self.session is not None
        # Loud panel makes it impossible to miss whether the model is actually
        # invoking tools. If you don't see this panel during a Jurinex Q&A,
        # the model is hallucinating instead of grounding — switch to a Live
        # model with stronger tool-calling support (e.g. gemini-2.0-flash-live-001).
        log_event_panel(
            "TOOL CALL FROM MODEL",
            {
                "Tool": event.tool_name or "?",
                "Args": (str(event.tool_args)[:200] if event.tool_args else "{}"),
            },
            style="magenta",
            icon_key="tool",
        )
        # Mark the moment so shadow-RAG stands down briefly — the model
        # already grounded itself, no need for us to double-search.
        if event.tool_name == "search_knowledge_base":
            self._tool_called_at = time.monotonic()
            # Drop the buffer too, so the next utterance starts cleanly.
            self._caller_transcript_buf = ""
        log_dataflow(
            "gemini.tool_call",
            f"{event.tool_name}",
            payload=event.tool_args,
        )
        # The session_id we persist in voice_tool_executions corresponds to
        # the in-memory call session UUID (hex without dashes); cast back to
        # a uuid.UUID so the column accepts it.
        import uuid as _uuid

        try:
            voice_session_uuid = _uuid.UUID(self.session.session_id)
        except (TypeError, ValueError):
            voice_session_uuid = None

        async with session_scope() as session:
            result = await dispatch_tool_call(
                session=session,
                call_id=self.session.call_db_id,
                tool_name=event.tool_name or "",
                arguments=event.tool_args or {},
                bundle=self.bundle,
                voice_session_id=voice_session_uuid,
                function_call_id=event.tool_call_id,
            )
        # Return the tool result via the proper Live API path. This sends
        # the FULL result (not a 200-char truncation) as a function_response
        # so the model actually sees the retrieved chunks and can ground
        # its spoken reply in them.
        await self.session.gemini.send_tool_response(
            tool_name=event.tool_name or "",
            tool_call_id=event.tool_call_id,
            result=result,
        )

        # ── agent_transfer hot-swap ──
        # The dispatcher returns action='swap_agent' when the model handed
        # off to a different voice agent. Honour it AFTER acking the tool
        # call so the OLD model's turn closes cleanly, then close that
        # session and reopen with the new bundle. Twilio Media Stream stays
        # open the whole time — caller hears no hang-up.
        if isinstance(result, dict) and result.get("action") == "swap_agent":
            await self._swap_agent(
                target_agent_id=result.get("target_agent_id") or "",
                target_agent_name=result.get("target_agent_name") or "",
                handoff_message=result.get("handoff_message") or "",
            )

    async def _swap_agent(
        self,
        *,
        target_agent_id: str,
        target_agent_name: str,
        handoff_message: str,
    ) -> None:
        """Hot-swap the live Gemini session to a different voice agent.

        Sequence:
          1. Resolve the target bundle from the DB (id wins, fall back to name).
          2. Build a fresh system instruction via SystemInstructionBuilder.
          3. Close the OLD Gemini Live session.
          4. Open a NEW one with the target's live_model + voice + tools.
          5. Prime the new agent with ``handoff_message`` so it speaks first.

        Twilio Media Stream is NOT touched — caller stays on the same call.
        """
        from app.realtime.gemini_live_client import GeminiLiveClient

        log_event_panel(
            "AGENT HOT-SWAP",
            {
                "From": self.bundle.name if self.bundle else "?",
                "To": target_agent_name or target_agent_id,
                "Handoff": (handoff_message or "")[:120],
            },
            style="magenta",
            icon_key="escalation",
        )

        # 1. Load the target bundle
        async with session_scope() as session:
            repo = VoiceAgentRepository(session)
            target: AgentBundle | None = None
            if target_agent_id:
                target = await repo.load_active_bundle_by_id(target_agent_id)
            if target is None and target_agent_name:
                target = await repo.load_active_bundle(target_agent_name)
            if target is None:
                log_dataflow(
                    "agent.swap.failed",
                    f"target id={target_agent_id} name={target_agent_name} "
                    f"could not be resolved — staying on current agent",
                    level="error",
                )
                return

            # 2. Build new system instruction
            assembled = await SystemInstructionBuilder(
                PromptFragmentsRepository(session)
            ).build(target)

        log_dataflow(
            "agent.swap.target_loaded",
            f"target={target.name} live_model={target.live_model} "
            f"voice={target.voice_name} tools={assembled.enabled_tools}",
        )

        # 3. Close the OLD Gemini session (Twilio WS stays open)
        old_gemini = self.session.gemini
        old_consume_task = self._gemini_task
        try:
            await old_gemini.close()
        except Exception as exc:
            log_dataflow(
                "agent.swap.old_close_error",
                f"old session close raised {exc}",
                level="warning",
            )
        if old_consume_task is not None and not old_consume_task.done():
            old_consume_task.cancel()

        # 4. Open NEW Gemini session
        enabled_tool_names: set[str] = set(target.enabled_function_keys)
        if target.knowledge_base_settings.get("document_ids"):
            enabled_tool_names.add("search_knowledge_base")
        enabled_tool_names.add("end_call")

        new_gemini = GeminiLiveClient()
        new_gemini.on_session_dead = self._on_gemini_session_dead
        try:
            await new_gemini.connect(
                self.session.session_id,
                assembled.text,
                live_model=target.live_model or settings.gemini_model,
                voice_name=target.voice_name or settings.gemini_voice,
                temperature=float(target.temperature),
                enabled_tool_names=enabled_tool_names or None,
            )
        except Exception as exc:
            log_dataflow(
                "agent.swap.new_connect_failed",
                f"new agent {target.name} connect failed: {exc}",
                level="error",
            )
            return

        # 5. Replace the bundle + session.gemini and restart the consumer
        self.bundle = target
        self.assembled = assembled
        self.session.gemini = new_gemini
        self._gemini_task = asyncio.create_task(self._consume_gemini_events())

        # Reset shadow-RAG state so the new agent doesn't get an old buffer
        self._caller_transcript_buf = ""
        self._tool_called_at = 0.0
        self._last_output_transcript_at = 0.0

        # 6. Prime the new agent so it speaks first with the handoff line
        if handoff_message:
            await new_gemini.prime(
                f"[The caller has just been transferred to you. Open the "
                f"conversation in the caller's chosen language by saying "
                f"this naturally: {handoff_message}. Then continue normally.]"
            )

        log_dataflow(
            "agent.swap.complete",
            f"now serving as {target.name} ({target.live_model}/{target.voice_name})",
        )
        self._debug_event(
            event_type="bridge",
            event_stage="agent.swap",
            message=f"hot-swapped to {target.name}",
            payload={
                "target_agent_id": str(target.id),
                "target_agent_name": target.name,
                "live_model": target.live_model,
                "voice_name": target.voice_name,
            },
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
    # Watchdog (silence + max duration) and auto-hangup
    # ------------------------------------------------------------------

    async def _watchdog_loop(self) -> None:
        """Tick once a second checking the silence + max-duration thresholds."""
        try:
            while not self._terminating:
                await asyncio.sleep(1.0)
                now = time.monotonic()

                # B — max-duration cap (admin override wins via bundle.call_settings)
                call_age = now - self._call_start_ts
                if call_age >= self._effective_max_seconds:
                    log_dataflow(
                        "watchdog.max_duration",
                        f"call_age={call_age:.0f}s >= "
                        f"max={self._effective_max_seconds}s",
                        level="warning",
                    )
                    await self._graceful_hangup(
                        reason="max_duration",
                        gemini_prompt=(
                            "[The call has reached its time limit. Politely thank the "
                            "caller, ask them to call back if they need more help, "
                            "and say goodbye in the current language. Keep it short.]"
                        ),
                    )
                    return

                # A — silence timeout (admin override wins via bundle.call_settings)
                silence = now - self._last_mic_activity_ts
                if silence >= self._effective_silence_seconds:
                    log_dataflow(
                        "watchdog.silence_timeout",
                        f"silence={silence:.0f}s >= "
                        f"limit={self._effective_silence_seconds}s",
                        level="warning",
                    )
                    await self._graceful_hangup(
                        reason="silence_timeout",
                        gemini_prompt=(
                            "[The caller has gone silent. Politely check in once with "
                            "'Are you still there?' and if they don't reply, say goodbye "
                            "in the current language. Keep it very short.]"
                        ),
                    )
                    return
        except asyncio.CancelledError:
            log_dataflow("watchdog.cancelled", "watchdog stopped")
            raise
        except Exception as exc:
            log_error("WATCHDOG ERROR", str(exc))

    def _on_gemini_session_dead(self, reason: str) -> Any:
        """Called by GeminiLiveClient when the live session is gone (D)."""
        if not settings.auto_hangup_on_gemini_failure:
            log_dataflow(
                "watchdog.gemini_dead",
                "auto-hangup disabled — leaving the line open",
                level="warning",
            )
            return None
        log_dataflow(
            "watchdog.gemini_dead",
            f"auto-hangup triggered (reason={reason[:80]})",
            level="warning",
        )
        return self._graceful_hangup(
            reason="gemini_failure",
            gemini_prompt=None,
            fallback_say=settings.technical_failure_message,
        )

    async def _graceful_hangup(
        self,
        *,
        reason: str,
        gemini_prompt: str | None,
        fallback_say: str | None = None,
    ) -> None:
        """Wind the call down cleanly: farewell → grace period → drop Twilio leg."""
        if self._terminating:
            return
        self._terminating = True
        self._terminate_reason = reason

        log_event_panel(
            "AUTO HANGUP",
            {
                "Reason": reason,
                "Call SID": self.call_sid or "-",
                "Grace": f"{settings.farewell_grace_seconds}s",
            },
            style="yellow",
            icon_key="warn",
        )
        self._debug_event(
            event_type="watchdog",
            event_stage="auto_hangup",
            message=f"watchdog hangup reason={reason}",
            payload={"reason": reason, "call_sid": self.call_sid},
        )

        # If Gemini is still alive, ask Preeti to say goodbye in the active language.
        gemini_alive = bool(
            self.session
            and self.session.gemini
            and not self.session.gemini._send_disabled  # noqa: SLF001
        )
        if gemini_prompt and gemini_alive:
            try:
                await self.session.gemini.send_text(gemini_prompt)
                # Let the farewell audio play out before we drop the call.
                await asyncio.sleep(max(0, settings.farewell_grace_seconds))
            except Exception as exc:
                log_dataflow(
                    "watchdog.farewell_error", str(exc), level="warning"
                )
        elif fallback_say and self.call_sid:
            # Gemini is dead — replace TwiML with a static <Say> + <Hangup/>.
            safe = html.escape(fallback_say)
            twiml = (
                f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<Response><Say voice="alice">{safe}</Say><Hangup/></Response>'
            )
            CallService.hangup_twilio_call(self.call_sid, twiml=twiml)
            return  # Twilio will close the WS once Hangup runs

        # Drop the leg directly via Twilio REST (C reused).
        if self.call_sid:
            CallService.hangup_twilio_call(self.call_sid)

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
            if self._watchdog_task and not self._watchdog_task.done():
                self._watchdog_task.cancel()

            # Upload call recording to GCS (best-effort, never raises into teardown)
            recording_uris: dict[str, str | None] = {}
            if self.recorder is not None and self.recorder.has_audio():
                metadata = {
                    "call_sid": self.session.twilio_call_sid,
                    "session_id": self.session.session_id,
                    "direction": self.session.direction,
                    "customer_phone": self.session.customer_phone,
                    "language": self.session.language,
                    "started_at": self.recorder.started_at.isoformat(),
                    "ended_at": utcnow().isoformat(),
                    "caller_seconds": round(self.recorder.caller_seconds, 2),
                    "agent_seconds": round(self.recorder.agent_seconds, 2),
                    "model": (
                        self.bundle.live_model if self.bundle else settings.gemini_model
                    ),
                    "voice": (
                        self.bundle.voice_name if self.bundle else settings.gemini_voice
                    ),
                    "agent_id": (
                        str(self.bundle.id) if self.bundle else None
                    ),
                    "terminate_reason": self._terminate_reason or "caller_hangup",
                }
                try:
                    recording_uris = await upload_call_recording(
                        folder=self.recorder.gcs_folder(),
                        mixed_wav=self.recorder.encode_mixed_wav(),
                        metadata=metadata,
                    )
                except Exception as exc:
                    log_error("CALL RECORDING UPLOAD FAILED", str(exc))

            # Persist call end + summary
            from app.services.post_call_service import run_post_call_extraction
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
                    # Stash GCS URIs + per-call cost on calls.raw_metadata
                    # so the admin dashboard can render both without a
                    # separate query. Pricing comes from the admin-owned
                    # voice_model_pricing table keyed on the agent's
                    # live_model.
                    pricing_payload: dict[str, Any] | None = None
                    try:
                        from app.services.pricing_service import compute_call_cost

                        live_model_used = (
                            self.bundle.live_model
                            if self.bundle is not None and self.bundle.live_model
                            else settings.gemini_model
                        )
                        pricing_payload = await compute_call_cost(
                            session,
                            model_id=live_model_used,
                            caller_seconds=(
                                self.recorder.caller_seconds
                                if self.recorder is not None
                                else 0.0
                            ),
                            agent_seconds=(
                                self.recorder.agent_seconds
                                if self.recorder is not None
                                else 0.0
                            ),
                        )
                    except Exception as exc:
                        log_dataflow(
                            "pricing.error",
                            f"could not compute call cost: {exc}",
                            level="warning",
                        )

                    if recording_uris.get("folder") or pricing_payload:
                        from sqlalchemy import update
                        from app.db.models import Call

                        call = await session.get(Call, self.session.call_db_id)
                        if call is not None:
                            existing = call.raw_metadata or {}
                            if recording_uris.get("folder"):
                                existing["recording"] = recording_uris
                            if pricing_payload is not None:
                                existing["pricing"] = pricing_payload
                            await session.execute(
                                update(Call)
                                .where(Call.id == self.session.call_db_id)
                                .values(raw_metadata=existing)
                            )

                    # Scheduler linkage — close the originating
                    # voice_call_schedules row. This is best-effort: a stale
                    # schedule row never blocks teardown, but the operator
                    # still sees a clear FAILED panel if the UPDATE itself
                    # raises.
                    if self.schedule_id is not None:
                        try:
                            from app.db.voice_call_schedules_repository import (
                                VoiceCallSchedulesRepository,
                            )

                            await VoiceCallSchedulesRepository(session).mark_completed(
                                schedule_id=self.schedule_id,
                                call_id=self.session.call_db_id,
                            )
                        except Exception as exc:
                            log_error(
                                "SCHEDULER COMPLETE FAILED",
                                f"schedule_id={self.schedule_id}: {exc}",
                            )

                    # Phase-3 #2 — post-call extraction. Best-effort: this
                    # path catches any exception internally so a failed model
                    # call does not block call teardown or the recording
                    # upload that just succeeded.
                    try:
                        import uuid as _uuid

                        try:
                            voice_session_uuid = _uuid.UUID(self.session.session_id)
                        except (TypeError, ValueError):
                            voice_session_uuid = None
                        await run_post_call_extraction(
                            session,
                            call_id=self.session.call_db_id,
                            bundle=self.bundle,
                            voice_session_id=voice_session_uuid,
                            recording_uris=recording_uris or None,
                            terminate_reason=self._terminate_reason or "caller_hangup",
                            language=self.session.language,
                            transfer_requested=(
                                self._terminate_reason == "transfer"
                            ),
                            pricing_payload=pricing_payload,
                        )
                    except Exception as exc:
                        log_error("POST-CALL HOOK FAILED", str(exc))

            log_event_panel(
                "CALL ENDED",
                {
                    "Call SID": self.session.twilio_call_sid,
                    "Session": self.session.session_id[:8],
                    "Recording": recording_uris.get("folder") or "(not uploaded)",
                },
                style="green",
                icon_key="call_end",
            )
            self._debug_event(
                event_type="bridge",
                event_stage="call.ended",
                message=(
                    f"call ended terminate_reason="
                    f"{self._terminate_reason or 'caller_hangup'} "
                    f"recording={'yes' if recording_uris.get('folder') else 'no'}"
                ),
                payload={
                    "call_sid": self.session.twilio_call_sid,
                    "terminate_reason": self._terminate_reason or "caller_hangup",
                    "recording": recording_uris,
                },
            )
            await session_manager.remove(self.session.session_id)
        try:
            await self.websocket.close()
        except Exception:
            pass
