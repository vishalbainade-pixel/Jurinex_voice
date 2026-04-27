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

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{ws_url}">
      <Parameter name="direction" value="{safe_direction}"/>
      <Parameter name="from" value="{safe_from}"/>
      <Parameter name="to" value="{safe_to}"/>
      <Parameter name="call_sid" value="{safe_sid}"/>
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
    log_event_panel(
        "OUTBOUND ANSWERED",
        {"From": From, "To": To, "Call SID": CallSid},
        style="cyan",
        icon_key="call_start",
    )
    twiml = _build_twiml_stream(
        call_sid=CallSid, direction="outbound", from_number=From, to_number=To
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
