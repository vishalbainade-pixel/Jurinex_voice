"""Call lifecycle orchestration (creation, status, outbound dialing)."""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client as TwilioClient

from app.config import settings
from app.db.models import CallDirection, CallStatus
from app.db.repositories import CallRepository, CustomerRepository
from app.db.schemas import OutboundCallRequest, OutboundCallResponse
from app.observability.logger import log_dataflow, log_error, log_event_panel
from app.utils.phone import normalize_e164
from app.utils.time_utils import utcnow


class CallService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Inbound (record only — actual streaming handled in TwilioMediaStreamHandler)
    # ------------------------------------------------------------------

    async def record_inbound_webhook(
        self,
        *,
        call_sid: str | None,
        from_number: str | None,
        to_number: str | None,
        raw: dict[str, Any] | None,
    ) -> None:
        log_dataflow(
            "twilio.webhook.received",
            f"inbound webhook from={from_number}",
            payload={"call_sid": call_sid, "to": to_number},
        )

    async def mark_completed(self, call_id: uuid.UUID) -> None:
        repo = CallRepository(self.session)
        call = await repo.get(call_id)
        if not call:
            return
        duration = (
            int((utcnow() - call.started_at).total_seconds()) if call.started_at else None
        )
        await repo.update_status(
            call_id,
            status=CallStatus.completed,
            ended_at=utcnow(),
            duration_seconds=duration,
        )

    # ------------------------------------------------------------------
    # Outbound dialing via Twilio
    # ------------------------------------------------------------------

    async def place_outbound(self, payload: OutboundCallRequest) -> OutboundCallResponse:
        try:
            to_e164 = normalize_e164(payload.to_phone_number)
        except ValueError as e:
            raise ValueError(f"invalid to_phone_number: {e}") from e

        if settings.demo_mode and not (
            settings.twilio_account_sid and settings.twilio_auth_token
        ):
            # Demo: pretend Twilio accepted the call.
            fake_sid = f"DEMO_{uuid.uuid4().hex[:24]}"
            await self._persist_pending_call(payload, to_e164, fake_sid)
            log_event_panel(
                "OUTBOUND CALL (DEMO)",
                {
                    "To": to_e164,
                    "From": settings.twilio_phone_number,
                    "SID": fake_sid,
                    "Reason": payload.reason or "-",
                },
                style="magenta",
                icon_key="call_start",
            )
            return OutboundCallResponse(
                call_sid=fake_sid,
                status="queued-demo",
                to=to_e164,
                **{"from": settings.twilio_phone_number},
            )

        client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
        qs = urlencode(
            {
                "customer_name": payload.customer_name or "",
                "language_hint": payload.language_hint or "",
            }
        )
        answer_url = (
            f"{settings.public_base_url.rstrip('/')}/twilio/outbound-answer?{qs}"
        )
        status_callback = f"{settings.public_base_url.rstrip('/')}/twilio/call-status"

        try:
            twilio_call = client.calls.create(
                to=to_e164,
                from_=settings.twilio_phone_number,
                url=answer_url,
                status_callback=status_callback,
                status_callback_event=["initiated", "ringing", "answered", "completed"],
                status_callback_method="POST",
            )
        except TwilioRestException as exc:
            log_error("OUTBOUND CALL FAILED", str(exc), {"to": to_e164})
            raise

        await self._persist_pending_call(payload, to_e164, twilio_call.sid)
        log_event_panel(
            "OUTBOUND CALL PLACED",
            {
                "To": to_e164,
                "From": settings.twilio_phone_number,
                "SID": twilio_call.sid,
                "Status": twilio_call.status,
            },
            style="cyan",
            icon_key="call_start",
        )
        return OutboundCallResponse(
            call_sid=twilio_call.sid,
            status=twilio_call.status,
            to=to_e164,
            **{"from": settings.twilio_phone_number},
        )

    # ------------------------------------------------------------------
    # Twilio leg termination — used by end_call tool, watchdogs, etc.
    # ------------------------------------------------------------------

    @staticmethod
    def hangup_twilio_call(call_sid: str, twiml: str | None = None) -> bool:
        """Disconnect the Twilio call leg (synchronous Twilio REST call).

        Returns True on success. Skips DEMO/empty SIDs.
        If ``twiml`` is provided, that TwiML plays before the hangup.
        """
        if not call_sid or call_sid.startswith("DEMO_"):
            log_dataflow(
                "twilio.hangup.skipped",
                f"sid={call_sid!r} not a real Twilio call",
                level="debug",
            )
            return False
        if not (settings.twilio_account_sid and settings.twilio_auth_token):
            log_dataflow(
                "twilio.hangup.skipped",
                "twilio creds not configured",
                level="warning",
            )
            return False

        try:
            client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
            if twiml:
                client.calls(call_sid).update(twiml=twiml)
                log_dataflow(
                    "twilio.hangup.twiml",
                    f"replaced TwiML on {call_sid} (will play farewell + hang up)",
                )
            else:
                client.calls(call_sid).update(status="completed")
                log_dataflow(
                    "twilio.hangup.completed",
                    f"sent status=completed to {call_sid}",
                )
            return True
        except TwilioRestException as exc:
            log_error(
                "TWILIO HANGUP FAILED",
                str(exc),
                {"call_sid": call_sid},
            )
            return False

    async def _persist_pending_call(
        self,
        payload: OutboundCallRequest,
        to_e164: str,
        sid: str,
    ) -> None:
        customer, _ = await CustomerRepository(self.session).get_or_create(
            phone=to_e164,
            name=payload.customer_name,
            preferred_language=payload.language_hint,
        )
        await CallRepository(self.session).create(
            twilio_call_sid=sid,
            direction=CallDirection.outbound,
            customer_phone=to_e164,
            twilio_from=settings.twilio_phone_number,
            twilio_to=to_e164,
            customer_id=customer.id,
            raw_metadata={"reason": payload.reason, "language_hint": payload.language_hint},
        )
