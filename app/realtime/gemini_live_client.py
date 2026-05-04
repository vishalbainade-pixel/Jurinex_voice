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
        # Optional callback fired exactly once when the live session dies.
        # The Twilio media-stream handler hooks this to auto-hang up the call.
        self.on_session_dead: Any = None  # Callable[[str], Awaitable | None] | None

        # Effective model/voice for THIS session. Resolved at connect() time
        # from the agent bundle (DB) or settings.* fallback.
        self._live_model: str = settings.gemini_model
        self._voice_name: str = settings.gemini_voice
        self._temperature: float | None = None
        self._enabled_tool_names: set[str] | None = None

        # Auto-resume budget — bumped on every reconnect attempt; capped by
        # ``settings.jurinex_voice_live_max_resumes``.
        self._resume_count: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(
        self,
        session_id: str,
        system_prompt: str,
        *,
        live_model: str | None = None,
        voice_name: str | None = None,
        temperature: float | None = None,
        enabled_tool_names: set[str] | list[str] | None = None,
    ) -> None:
        """Open a live session.

        Optional overrides (sourced from the admin DB at call start):

        * ``live_model`` — Gemini Live model id (``gemini-3.1-flash-live-preview``,
          ``gemini-2.5-flash-native-audio-preview-09-2025``, etc.). Falls back
          to ``settings.gemini_model`` when ``None``.
        * ``voice_name`` — prebuilt voice id (``Achernar``, ``Leda``, …).
          Falls back to ``settings.gemini_voice``.
        * ``temperature`` — passed through to the Live config when supported.
        * ``enabled_tool_names`` — restrict which tool declarations get sent
          to the model. ``None`` declares ALL of the legacy four tools. The
          admin uses ``transfer_call`` whereas the legacy bridge declared
          ``transfer_to_human_agent`` — both are accepted aliases here.
        """
        self._session_id = session_id
        self._system_prompt = system_prompt
        if live_model:
            self._live_model = live_model
        if voice_name:
            self._voice_name = voice_name
        if temperature is not None:
            self._temperature = temperature
        if enabled_tool_names is not None:
            self._enabled_tool_names = set(enabled_tool_names)

        if not settings.gemini_key or settings.demo_mode:
            log_event_panel(
                "GEMINI SESSION (SIMULATED)",
                {
                    "session_id": session_id[:8],
                    "model": self._live_model,
                    "voice": self._voice_name,
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

        # Enable transcription on BOTH directions so call_messages gets
        # populated on real calls. Some SDK versions or model variants may not
        # support these fields — wrap in try/except so the session still opens
        # if the kwargs are rejected.
        config_kwargs: dict[str, Any] = dict(
            response_modalities=["AUDIO"],
            system_instruction=types.Content(parts=[types.Part(text=system_prompt)]),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self._voice_name
                    )
                )
            ),
        )
        # Optional generation knobs from the agent bundle. Older SDK versions
        # may not honour `temperature` here — drop it silently if rejected.
        if self._temperature is not None:
            config_kwargs["temperature"] = float(self._temperature)
        try:
            config_kwargs["input_audio_transcription"] = types.AudioTranscriptionConfig()
            config_kwargs["output_audio_transcription"] = types.AudioTranscriptionConfig()
        except AttributeError:
            log_dataflow(
                "gemini.transcription",
                "AudioTranscriptionConfig not in SDK — transcripts will be empty on real calls",
                level="warning",
            )

        # VAD tuning for telephony.
        #
        # We start_sensitivity=LOW (not HIGH) because:
        #   - μ-law/8k phone audio has line noise + breathing that HIGH
        #     sensitivity treated as speech → it kept interrupting Preeti
        #     mid-sentence → Gemini retried the same response → same noise
        #     interrupted again → "speaks 2 words then restarts" loop.
        #   - LOW + 200 ms prefix_padding requires real, sustained speech
        #     before VAD fires the interrupt.
        # silence_duration=500ms gives the caller a comfortable beat to
        # finish their thought before Gemini starts responding.
        try:
            config_kwargs["realtime_input_config"] = types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False,
                    start_of_speech_sensitivity="START_SENSITIVITY_LOW",
                    end_of_speech_sensitivity="END_SENSITIVITY_HIGH",
                    prefix_padding_ms=200,
                    silence_duration_ms=500,
                ),
                activity_handling="START_OF_ACTIVITY_INTERRUPTS",
            )
        except (AttributeError, ValueError) as exc:
            log_dataflow(
                "gemini.vad",
                f"RealtimeInputConfig not available on this SDK: {exc}",
                level="warning",
            )

        # Tool declarations the model can call. Wrapped in try/except so an
        # SDK without `Tool` / `FunctionDeclaration` still opens cleanly.
        # If the admin bundle restricted enabled tools, we filter the
        # declarations to that subset (so the model can't call something
        # the admin disabled).
        try:
            config_kwargs["tools"] = _build_tool_declarations(
                types, self._enabled_tool_names
            )
        except Exception as exc:
            log_dataflow(
                "gemini.tools.declare_failed",
                f"could not declare tools: {exc}",
                level="warning",
            )

        # Explicit tool_config — pins function-calling to AUTO so the model
        # *can* call any of the declared tools when its judgment dictates.
        # This guards against SDK silently defaulting to mode="NONE".
        try:
            config_kwargs["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode="AUTO",
                )
            )
        except Exception as exc:
            log_dataflow(
                "gemini.tool_config.unavailable",
                f"ToolConfig not in SDK: {exc}",
                level="warning",
            )

        # Try a sequence of fallbacks: drop one optional field at a time and
        # retry. Pydantic raises ValidationError (a ValueError subclass)
        # when an unknown kwarg is passed, while older SDKs raise TypeError.
        # We catch both.
        _OPTIONAL_KEYS = (
            "temperature",
            "tool_config",
            "input_audio_transcription",
            "output_audio_transcription",
        )
        config = None
        last_err: Exception | None = None
        for _ in range(len(_OPTIONAL_KEYS) + 1):
            try:
                config = types.LiveConnectConfig(**config_kwargs)
                break
            except (TypeError, ValueError) as exc:
                last_err = exc
                # Drop the first optional field that's still present and retry.
                dropped = None
                for k in _OPTIONAL_KEYS:
                    if k in config_kwargs:
                        config_kwargs.pop(k)
                        dropped = k
                        break
                log_dataflow(
                    "gemini.config.fallback",
                    f"LiveConnectConfig rejected kwargs ({exc.__class__.__name__}); "
                    f"dropped={dropped!r}",
                    level="warning",
                )
                if dropped is None:
                    break  # nothing left to drop
        if config is None:
            raise last_err if last_err else RuntimeError("LiveConnectConfig unbuildable")

        # Surface what actually made it into the live config so we can spot
        # silent SDK drops (e.g. tools= being filtered) without guesswork.
        tool_count = 0
        for t in (config_kwargs.get("tools") or []):
            tool_count += len(getattr(t, "function_declarations", []) or [])
        log_dataflow(
            "gemini.config.summary",
            f"model={self._live_model} "
            f"modalities={config_kwargs.get('response_modalities')} "
            f"voice={self._voice_name} "
            f"temperature={self._temperature} "
            f"input_transcription={'input_audio_transcription' in config_kwargs} "
            f"output_transcription={'output_audio_transcription' in config_kwargs} "
            f"tools_declared={tool_count} "
            f"enabled={sorted(self._enabled_tool_names) if self._enabled_tool_names else 'all'}",
        )

        self._connect_cm = self._client.aio.live.connect(
            model=self._live_model, config=config
        )
        self._real_session = await self._connect_cm.__aenter__()
        self._real_mode = True
        self._receive_task = asyncio.create_task(self._receive_loop())

        log_event_panel(
            "GEMINI SESSION STARTED",
            {
                "session_id": session_id[:8],
                "model": self._live_model,
                "voice": self._voice_name,
                "tools_declared": tool_count,
            },
            style="cyan",
            icon_key="gemini",
        )
        await self._inbox.put(GeminiEvent(type="session_open"))

    async def _attempt_resume(self, reason: str) -> bool:
        """Try to reconnect the Live session up to ``max_resumes`` times.

        Returns True when a fresh session is open and the receive loop has
        been restarted; False once the budget is exhausted (or no
        system_prompt was ever recorded — meaning we never made it past the
        initial handshake, so resuming is meaningless).
        """
        if self._closed or self._system_prompt is None:
            return False
        if self._resume_count >= settings.jurinex_voice_live_max_resumes:
            log_dataflow(
                "gemini.resume.exhausted",
                f"resumes={self._resume_count} "
                f"limit={settings.jurinex_voice_live_max_resumes} — giving up",
                level="warning",
            )
            return False

        self._resume_count += 1
        log_event_panel(
            "GEMINI RESUME",
            {
                "attempt": f"{self._resume_count} of "
                f"{settings.jurinex_voice_live_max_resumes}",
                "trigger": reason[:200],
                "model": self._live_model,
                "voice": self._voice_name,
            },
            style="yellow",
            icon_key="warn",
        )

        # Cancel the dead receive loop + close the dead context manager.
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
        self._receive_task = None
        if self._connect_cm is not None:
            try:
                await self._connect_cm.__aexit__(None, None, None)
            except Exception:
                pass
            self._connect_cm = None
        self._real_session = None
        self._real_mode = False

        try:
            await self._open_real_session(
                self._session_id or "resumed", self._system_prompt
            )
        except Exception as exc:
            log_dataflow(
                "gemini.resume.failed",
                f"attempt={self._resume_count}: {exc}",
                level="error",
            )
            return False

        log_dataflow(
            "gemini.resume.ok",
            f"attempt={self._resume_count} session re-opened",
        )
        return True

    def _disable_session(self, reason: str) -> None:
        """Mark the live session as dead — log once, drop further sends.

        Before giving up we try ``JURINEX_VOICE_LIVE_MAX_RESUMES`` reconnects.
        Only when those are exhausted do we set ``_send_disabled=True`` and
        fire the ``on_session_dead`` callback to auto-hang up the call leg.
        """
        if self._send_disabled:
            return

        async def _try_resume_then_die() -> None:
            ok = await self._attempt_resume(reason)
            if ok:
                return
            # Resume budget exhausted — set the dead flag and notify owner.
            self._send_disabled = True
            self._send_disabled_reason = reason
            log_event_panel(
                "GEMINI SESSION DEAD",
                {
                    "reason": reason[:200],
                    "model": self._live_model,
                    "voice": self._voice_name,
                    "resumes_tried": str(self._resume_count),
                    "hint": (
                        "Common causes: (1) API key not authorized for this model; "
                        "(2) voice name not supported by this model; "
                        "(3) model name unavailable to the project."
                    ),
                },
                style="red",
                icon_key="error",
            )
            cb = self.on_session_dead
            if cb is not None:
                try:
                    result = cb(reason)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    log_dataflow(
                        "gemini.on_session_dead.error",
                        str(exc),
                        level="warning",
                    )

        try:
            asyncio.create_task(_try_resume_then_die())
        except RuntimeError:
            # Event loop is closed (e.g. mid-shutdown). Fall back to the
            # legacy synchronous teardown — just notify the owner.
            self._send_disabled = True
            self._send_disabled_reason = reason
            cb = self.on_session_dead
            if cb is not None:
                try:
                    cb(reason)
                except Exception:
                    pass

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

    async def prime(self, text: str) -> bool:
        """Push a one-shot text trigger via ``send_realtime_input(text=...)``.

        Used right after session_open to make Preeti speak first. This path
        is distinct from ``send_text`` which uses ``send_client_content``
        (and gets rejected with WS close 1008 when sent before any audio
        on an audio-modality session).

        Returns True if the trigger was accepted by the live session.
        """
        if not text or not self._real_mode or self._real_session is None:
            return False
        if self._send_disabled:
            return False

        sess = self._real_session
        method = getattr(sess, "send_realtime_input", None)
        if method is None:
            log_dataflow(
                "gemini.prime.unavailable",
                "send_realtime_input not on this SDK; skipping prime",
                level="warning",
            )
            return False
        try:
            await method(text=text)
            log_dataflow("gemini.prime.sent", text[:120])
            return True
        except Exception as exc:
            log_dataflow(
                "gemini.prime.error",
                f"send_realtime_input(text=...) failed: {exc}",
                level="warning",
            )
            return False

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

    async def send_tool_response(
        self,
        *,
        tool_name: str,
        tool_call_id: str | None,
        result: dict[str, Any],
    ) -> bool:
        """Return a tool/function-call result to the live model.

        This is the *correct* way to feed RAG chunks back so the model
        can ground its next spoken reply in them. Falls back to a plain
        send_text() with the full JSON if send_tool_response isn't on the
        SDK or if it rejects the payload.
        """
        if not self._real_mode or self._real_session is None or self._send_disabled:
            return False

        from google.genai import types

        sess = self._real_session
        method = getattr(sess, "send_tool_response", None)
        if method is not None:
            try:
                fr_kwargs: dict[str, Any] = {
                    "name": tool_name,
                    "response": result,
                }
                if tool_call_id:
                    fr_kwargs["id"] = tool_call_id
                await method(function_responses=[types.FunctionResponse(**fr_kwargs)])
                log_dataflow(
                    "gemini.tool_response.sent",
                    f"name={tool_name} id={tool_call_id} payload={len(str(result))}b",
                )
                return True
            except Exception as exc:
                log_dataflow(
                    "gemini.tool_response.error",
                    f"send_tool_response failed: {exc}; falling back to send_text",
                    level="warning",
                )

        # Fallback: serialize the full result and pipe via send_text(...).
        import json as _json

        await self.send_text(
            f"[tool_result name={tool_name}]\n{_json.dumps(result, default=str)}"
        )
        return False

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
        # Walking the nested path on top would duplicate every chunk.

        # Interruption (caller barged in) — handle first so any in-flight
        # audio chunks below are dropped, not played.
        sc = getattr(response, "server_content", None)
        if sc is not None and getattr(sc, "interrupted", False):
            self._inbox.put_nowait(GeminiEvent(type="interrupt"))
            return  # don't process audio/text from this response chunk

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

        # Audio transcriptions live under server_content (when enabled in config)
        sc = getattr(response, "server_content", None)
        if sc is not None:
            input_tr = getattr(sc, "input_transcription", None)
            input_text = getattr(input_tr, "text", None) if input_tr else None
            if input_text:
                self._inbox.put_nowait(
                    GeminiEvent(type="input_transcript", text=input_text)
                )

            output_tr = getattr(sc, "output_transcription", None)
            output_text = getattr(output_tr, "text", None) if output_tr else None
            if output_text:
                self._inbox.put_nowait(
                    GeminiEvent(type="output_transcript", text=output_text)
                )

        tool_call = getattr(response, "tool_call", None)
        if tool_call and getattr(tool_call, "function_calls", None):
            for fc in tool_call.function_calls:
                self._inbox.put_nowait(
                    GeminiEvent(
                        type="tool_call",
                        tool_name=getattr(fc, "name", "") or "",
                        tool_args=dict(getattr(fc, "args", {}) or {}),
                        tool_call_id=getattr(fc, "id", None),
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


# ---------------------------------------------------------------------------
# Tool declarations exposed to the model
# ---------------------------------------------------------------------------


def _build_tool_declarations(
    types: Any,
    enabled_tool_names: set[str] | None = None,
) -> list[Any]:
    """Build the `tools=[Tool(function_declarations=[...])]` config block.

    The model needs to see the schema of every tool it's allowed to call.
    These mirror the tools wired in `app/services/tool_dispatcher.py`.

    ``enabled_tool_names`` (when not ``None``) restricts the declarations to
    that subset. The admin's table uses ``transfer_call`` while the legacy
    bridge wired ``transfer_to_human_agent`` — both names map to the same
    underlying declaration so the admin can use either.
    """
    # When the admin enabled "transfer_call" (their canonical name), advertise
    # the FunctionDeclaration under THAT name so the model can find it — the
    # admin's tool prompt template tells the model to call transfer_call(...).
    # The dispatcher canonicalizes back to transfer_to_human_agent on the way in.
    transfer_decl_name = (
        "transfer_call"
        if enabled_tool_names is not None and "transfer_call" in enabled_tool_names
        else "transfer_to_human_agent"
    )

    # Aliases admin tables → bridge declarations.
    _aliases = {"transfer_call": "transfer_to_human_agent"}
    if enabled_tool_names is not None:
        normalized: set[str] = set()
        for name in enabled_tool_names:
            normalized.add(_aliases.get(name, name))
            normalized.add(name)
        # Also accept the chosen advertised name in the filter set.
        normalized.add(transfer_decl_name)
        enabled_tool_names = normalized

    all_fns = [
        types.FunctionDeclaration(
            name="search_knowledge_base",
            description=(
                "Search Jurinex product documentation. Use this BEFORE answering "
                "any product/feature/pricing question. Returns top-k chunks with "
                "a similarity score. If `confident` is false or the chunks don't "
                "cover the question, do NOT guess — call transfer_to_human_agent."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The user's question, paraphrased into a self-contained search query.",
                    },
                    "k": {
                        "type": "integer",
                        "description": "Number of chunks to retrieve (default 5).",
                    },
                },
                "required": ["query"],
            },
        ),
        types.FunctionDeclaration(
            name=transfer_decl_name,
            description=(
                "Bridge the live caller to a human agent. "
                "REQUIRES EXPLICIT CALLER CONSENT unless the caller proactively "
                "asked for a human. Always speak a short transfer line in the "
                "caller's language before calling. Pass `language` so Twilio's "
                "on-hold message matches.\n\n"
                "DYNAMIC ROUTING: when your system prompt's "
                "TRANSFER configuration lists multiple intent → number rules "
                "(e.g. 'support → +91…', 'sales → +91…', 'admin → +91…'), "
                "you MUST pass `destination_phone` set to the E.164 number "
                "that matches the caller's intent. Pick exactly one of the "
                "numbers listed in the rules — never invent a number. "
                "If the rules list only one number, pass that one. "
                "If you cannot tell which intent applies, ask the caller a "
                "single clarifying question first; do NOT call this tool yet."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Short reason code (e.g. 'pricing', 'account_issue', 'kb_miss', 'caller_request', 'support', 'sales', 'admin').",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["English", "Hindi", "Marathi"],
                        "description": "The language the caller chose at the start of the call. Picks which on-hold message Twilio reads while the admin's phone rings.",
                    },
                    "destination_phone": {
                        "type": "string",
                        "description": (
                            "E.164 phone number to dial. REQUIRED when the "
                            "TRANSFER rules in your system prompt list "
                            "multiple numbers — pick the one whose intent "
                            "matches the caller (support / sales / admin). "
                            "Format: '+<country><number>' with no spaces or "
                            "dashes (e.g. '+917499303475'). Omit ONLY when "
                            "the system prompt configures a single static "
                            "destination."
                        ),
                    },
                    "farewell": {
                        "type": "string",
                        "description": (
                            "Controls the static on-hold message Twilio "
                            "reads while dialing the admin. PASS \"\" "
                            "(empty string) when you have already spoken "
                            "the dynamic pitch in your own voice — this "
                            "tells Twilio to skip its TTS message entirely "
                            "and go straight to dialing. Pass a custom "
                            "string to override with that text. Omit "
                            "the field to use the configured default "
                            "language-specific message."
                        ),
                    },
                },
                "required": ["reason", "language"],
            },
        ),
        types.FunctionDeclaration(
            name="create_support_ticket",
            description="Open a Jurinex support ticket capturing the customer's issue.",
            parameters={
                "type": "object",
                "properties": {
                    "customer_name": {"type": "string"},
                    "phone_number": {"type": "string"},
                    "email": {"type": "string"},
                    "issue_type": {"type": "string"},
                    "issue_summary": {"type": "string"},
                    "priority": {
                        "type": "string",
                        "enum": ["low", "normal", "high", "urgent"],
                    },
                    "language": {
                        "type": "string",
                        "enum": ["English", "Hindi", "Marathi"],
                    },
                },
                "required": ["issue_type", "issue_summary"],
            },
        ),
        types.FunctionDeclaration(
            name="end_call",
            description="Mark the call complete and disconnect the line. Call this only after saying goodbye.",
            parameters={
                "type": "object",
                "properties": {"reason": {"type": "string"}},
            },
        ),
        types.FunctionDeclaration(
            name="calendar_check",
            description=(
                "Look up real availability on the agent's Google Calendar. "
                "USE THIS WHEN the caller asks about open slots, availability, "
                "'when can we meet', or before proposing any specific time. "
                "Returns free_windows (already filtered to working hours, "
                "excluding blocked dates and existing events). Speak the times "
                "to the caller naturally — never read the raw ISO strings."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "start_iso": {
                        "type": "string",
                        "description": (
                            "Window start in ISO 8601 with timezone offset, "
                            "e.g. '2026-05-04T09:00:00+05:30'."
                        ),
                    },
                    "end_iso": {
                        "type": "string",
                        "description": (
                            "Window end in ISO 8601 with timezone offset, "
                            "e.g. '2026-05-04T18:00:00+05:30'."
                        ),
                    },
                },
                "required": ["start_iso", "end_iso"],
            },
        ),
        types.FunctionDeclaration(
            name="agent_transfer",
            description=(
                "Hand the live call off to a DIFFERENT voice agent (not a "
                "human). Use ONLY when the caller's intent clearly belongs to "
                "another agent (e.g. switching from a sales agent to a "
                "support agent). Speak one short hand-off line in the "
                "caller's language BEFORE calling. Pass either "
                "target_agent_name (the canonical voice_agents.name) or "
                "target_agent_id (the UUID), plus a short reason."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "target_agent_name": {
                        "type": "string",
                        "description": "Canonical voice agent name (e.g. 'preeti', 'rohit_sales').",
                    },
                    "target_agent_id": {
                        "type": "string",
                        "description": "Voice agent UUID — used when name is ambiguous.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Short reason code (e.g. 'intent_changed', 'sales_query', 'billing_issue').",
                    },
                    "handoff_message": {
                        "type": "string",
                        "description": (
                            "One sentence the new agent should read aloud "
                            "as its first turn (provides context to the "
                            "caller about why they're being moved)."
                        ),
                    },
                    "language": {
                        "type": "string",
                        "enum": ["English", "Hindi", "Marathi"],
                    },
                },
                "required": ["reason"],
            },
        ),
        types.FunctionDeclaration(
            name="calendar_book",
            description=(
                "Create a Google Calendar event after the caller has agreed "
                "to a specific slot AND spelled out their email. Returns "
                "status='booked' with google_event_id on success, or one of "
                "{outside_working_hours, date_blocked, day_disabled, "
                "view_only, conflict} on failure. Always read back the date, "
                "time, reason, and email aloud BEFORE calling this tool."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "start_iso": {
                        "type": "string",
                        "description": "Start time in ISO 8601 with TZ offset.",
                    },
                    "end_iso": {
                        "type": "string",
                        "description": (
                            "End time in ISO 8601 with TZ offset. If equal to "
                            "start_iso the bridge fills in default_meeting_minutes."
                        ),
                    },
                    "summary": {
                        "type": "string",
                        "description": "Short event title (the meeting reason).",
                    },
                    "attendee_name": {"type": "string"},
                    "attendee_email": {"type": "string"},
                    "attendee_phone": {"type": "string"},
                    "description": {
                        "type": "string",
                        "description": "Optional longer notes for the event body.",
                    },
                },
                "required": ["start_iso", "end_iso", "summary"],
            },
        ),
    ]

    if enabled_tool_names is None:
        fns = all_fns
    else:
        fns = [fn for fn in all_fns if fn.name in enabled_tool_names]

    if not fns:
        return []
    return [types.Tool(function_declarations=fns)]
