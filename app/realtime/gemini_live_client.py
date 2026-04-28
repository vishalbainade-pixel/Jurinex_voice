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
                        voice_name=settings.gemini_voice
                    )
                )
            ),
        )
        try:
            config_kwargs["input_audio_transcription"] = types.AudioTranscriptionConfig()
            config_kwargs["output_audio_transcription"] = types.AudioTranscriptionConfig()
        except AttributeError:
            log_dataflow(
                "gemini.transcription",
                "AudioTranscriptionConfig not in SDK — transcripts will be empty on real calls",
                level="warning",
            )

        # Tool declarations the model can call. Wrapped in try/except so an
        # SDK without `Tool` / `FunctionDeclaration` still opens cleanly.
        try:
            config_kwargs["tools"] = _build_tool_declarations(types)
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
            f"modalities={config_kwargs.get('response_modalities')} "
            f"voice={settings.gemini_voice} "
            f"input_transcription={'input_audio_transcription' in config_kwargs} "
            f"output_transcription={'output_audio_transcription' in config_kwargs} "
            f"tools_declared={tool_count}",
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
                "tools_declared": tool_count,
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

        # Notify owner (Twilio handler) so it can auto-hang up the call leg.
        cb = self.on_session_dead
        if cb is not None:
            try:
                result = cb(reason)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception as exc:
                log_dataflow(
                    "gemini.on_session_dead.error", str(exc), level="warning"
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


def _build_tool_declarations(types: Any) -> list[Any]:
    """Build the `tools=[Tool(function_declarations=[...])]` config block.

    The model needs to see the schema of every tool it's allowed to call.
    These mirror the tools wired in `app/services/tool_dispatcher.py`.
    """
    fns = [
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
            name="transfer_to_human_agent",
            description=(
                "Bridge the live caller to a human Jurinex support agent. "
                "REQUIRES EXPLICIT CALLER CONSENT — call this ONLY after "
                "(a) the caller proactively asked for a human, OR "
                "(b) you told them the KB doesn't have an answer and they "
                "said yes/haan/होय when asked if they want to be connected, "
                "OR (c) the issue is account-specific (their billing, their "
                "case, their account). Do NOT auto-call this on a KB miss "
                "without first asking the caller. Always speak a short "
                "transfer line in the caller's language before calling. "
                "Always pass `language` so Twilio's on-hold message matches."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Short reason code (e.g. 'pricing', 'account_issue', 'kb_miss', 'caller_request').",
                    },
                    "language": {
                        "type": "string",
                        "enum": ["English", "Hindi", "Marathi"],
                        "description": "The language the caller chose at the start of the call. Picks which on-hold message Twilio reads while the admin's phone rings.",
                    },
                    "farewell": {
                        "type": "string",
                        "description": "Optional override of the on-hold message. Leave empty unless you need to say something specific to this caller's situation; Twilio will use the configured language-specific message instead.",
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
    ]
    return [types.Tool(function_declarations=fns)]
