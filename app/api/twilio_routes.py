"""Twilio webhook + media-stream WebSocket routes."""

from __future__ import annotations

from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Form, Request, WebSocket
from fastapi.responses import Response

from app.config import settings
from app.db.database import session_scope
from app.observability.logger import log_dataflow, log_event_panel
from app.realtime.twilio_media_stream import TwilioMediaStreamHandler
from app.services.call_service import CallService

router = APIRouter(prefix="/twilio", tags=["twilio"])


def _build_twiml_stream(
    *,
    call_sid: str | None,
    direction: str,
    from_number: str | None,
    to_number: str | None,
    schedule_id: str | None = None,
    agent_name: str | None = None,
) -> str:
    """Return TwiML that connects the call to our media-stream WebSocket."""
    base = settings.public_base_url.rstrip("/")
    # FastAPI WebSocket lives at the same host. Use wss:// when public URL is https.
    ws_scheme = "wss" if base.startswith("https") else "ws"
    host = base.split("://", 1)[1]
    ws_url_raw = (
        f"{ws_scheme}://{host}/twilio/media-stream"
        f"?call_sid={quote(call_sid or '')}&direction={direction}"
    )
    # XML-escape every value before embedding — raw `&` makes the TwiML invalid
    # and Twilio responds with "an application error has occurred. Goodbye."
    ws_url = xml_escape(ws_url_raw, {'"': "&quot;"})
    safe_direction = xml_escape(direction)
    safe_from = xml_escape(from_number or "")
    safe_to = xml_escape(to_number or "")
    safe_sid = xml_escape(call_sid or "")
    # Optional scheduler-side parameters. Carried as Twilio Stream
    # <Parameter> entries so the bridge sees them in start.customParameters.
    safe_schedule = xml_escape(schedule_id or "")
    safe_agent = xml_escape(agent_name or "")
    schedule_xml = (
        f'\n      <Parameter name="schedule_id" value="{safe_schedule}"/>'
        if schedule_id
        else ""
    )
    agent_xml = (
        f'\n      <Parameter name="agent_name" value="{safe_agent}"/>'
        if agent_name
        else ""
    )

    # Eager greeting — three flavours, picked automatically:
    #
    #   1. PRE-LOADED LOCAL WAV (fastest):
    #      If the greeting WAV was successfully loaded at app startup, we
    #      do NOT add <Play> here. The Stream handler streams the cached
    #      μ-law bytes directly through the WS as soon as Stream opens,
    #      so Gemini Live cold-start can happen in parallel with the
    #      greeting playback. Total post-greeting latency: near zero.
    #
    #   2. <Play> a remote URL or local file we couldn't pre-load:
    #      Sequential — Twilio plays the file, then opens Connect/Stream.
    #      The Gemini cold-start happens AFTER the playback ends.
    #
    #   3. <Say> with TTS — fallback when no audio is configured.
    eager_say = ""
    if settings.eager_greeting_enabled:
        from app.realtime.greeting_loader import get_greeting_mulaw

        if get_greeting_mulaw() is None:
            audio_url = settings.eager_greeting_audio_url.strip()
            if audio_url:
                if not audio_url.lower().startswith(("http://", "https://")):
                    audio_url = f"{base}/{audio_url.lstrip('/')}"
                eager_say = f"  <Play>{xml_escape(audio_url)}</Play>\n"
            elif settings.eager_greeting_text:
                eg_text = xml_escape(settings.eager_greeting_text)
                eg_voice = xml_escape(settings.eager_greeting_voice)
                eg_lang = xml_escape(settings.eager_greeting_language)
                eager_say = (
                    f'  <Say voice="{eg_voice}" language="{eg_lang}">{eg_text}</Say>\n'
                )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
{eager_say}  <Connect>
    <Stream url="{ws_url}">
      <Parameter name="direction" value="{safe_direction}"/>
      <Parameter name="from" value="{safe_from}"/>
      <Parameter name="to" value="{safe_to}"/>
      <Parameter name="call_sid" value="{safe_sid}"/>{schedule_xml}{agent_xml}
    </Stream>
  </Connect>
  <Say voice="alice">Sorry, the assistant is currently unavailable. Please call back later.</Say>
</Response>""".strip()


@router.post("/incoming-call")
async def incoming_call(
    request: Request,
    CallSid: str = Form(default=""),
    From: str = Form(default=""),
    To: str = Form(default=""),
) -> Response:
    log_event_panel(
        "INBOUND CALL",
        {"From": From, "To": To, "Call SID": CallSid},
        style="cyan",
        icon_key="call_start",
    )
    async with session_scope() as session:
        await CallService(session).record_inbound_webhook(
            call_sid=CallSid,
            from_number=From,
            to_number=To,
            raw=dict(await request.form()),
        )

    twiml = _build_twiml_stream(
        call_sid=CallSid, direction="inbound", from_number=From, to_number=To
    )
    log_dataflow("twilio.twiml.generated", "inbound twiml", payload={"twiml": twiml})
    return Response(content=twiml, media_type="application/xml")


@router.post("/outbound-answer")
async def outbound_answer(
    request: Request,
    CallSid: str = Form(default=""),
    From: str = Form(default=""),
    To: str = Form(default=""),
) -> Response:
    # Scheduler-originated dials carry ``schedule_id`` + ``agent_name`` as
    # query-string parameters (the answer URL is built that way in
    # CallService.place_outbound_for_schedule). Forward both into the
    # Stream so the bridge can read them from start.customParameters.
    schedule_id = (request.query_params.get("schedule_id") or "").strip()
    agent_name = (request.query_params.get("agent_name") or "").strip()

    log_event_panel(
        "OUTBOUND ANSWERED",
        {
            "From": From,
            "To": To,
            "Call SID": CallSid,
            "Schedule id": schedule_id or "-",
            "Agent": agent_name or "-",
        },
        style="cyan",
        icon_key="call_start",
    )
    twiml = _build_twiml_stream(
        call_sid=CallSid,
        direction="outbound",
        from_number=From,
        to_number=To,
        schedule_id=schedule_id or None,
        agent_name=agent_name or None,
    )
    log_dataflow("twilio.twiml.generated", "outbound twiml", payload={"twiml": twiml})
    return Response(content=twiml, media_type="application/xml")


@router.post("/call-status")
async def call_status(
    CallSid: str = Form(default=""),
    CallStatus: str = Form(default=""),
) -> dict[str, str]:
    log_dataflow(
        "twilio.call.status",
        f"{CallSid} → {CallStatus}",
        payload={"call_sid": CallSid, "status": CallStatus},
    )
    return {"received": "ok"}


@router.websocket("/media-stream")
async def media_stream(websocket: WebSocket) -> None:
    handler = TwilioMediaStreamHandler(websocket)
    await handler.handle()
