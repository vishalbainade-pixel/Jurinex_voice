"""Live call → human-agent transfer using Twilio's `<Dial>` bridge.

When Preeti decides she can't help, she calls `transfer_to_human_agent(...)`.
We replace the in-progress Twilio call's TwiML with a `<Say>` farewell + a
`<Dial>` to the configured admin number; Twilio bridges the two legs into a
3-way conversation. Our media-stream WebSocket disconnects cleanly when the
new TwiML supersedes ours.
"""

from __future__ import annotations

import html
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import ResolutionStatus
from app.db.repositories import (
    AgentToolEventRepository,
    CallRepository,
    EscalationRepository,
)
from app.db.schemas import TransferToHumanInput
from app.observability.logger import log_dataflow, log_event_panel
from app.services.call_service import CallService


# Twilio <Say language="..."> codes per language. The voice itself is
# resolved from settings (env-overridable per language) so you can swap
# voices without code changes.
_LANGUAGE_CODE_MAP: dict[str, str] = {
    "English": "en-IN",
    "Hindi": "hi-IN",
    "Marathi": "mr-IN",
}


def _hold_message_for(language: str) -> str:
    if language == "Hindi":
        return settings.transfer_hold_message_hi
    if language == "Marathi":
        return settings.transfer_hold_message_mr
    return settings.transfer_hold_message_en


def _voice_for(language: str) -> str:
    if language == "Hindi":
        return settings.transfer_hold_voice_hi
    if language == "Marathi":
        return settings.transfer_hold_voice_mr
    return settings.transfer_hold_voice_en


def _build_transfer_twiml(*, farewell: str | None, language: str) -> str:
    target = html.escape(settings.support_admin_phone)
    caller_id = html.escape(settings.twilio_phone_number)
    timeout = max(5, int(settings.transfer_dial_timeout_seconds))

    voice = _voice_for(language)
    lang_code = _LANGUAGE_CODE_MAP.get(language, "en-IN")
    # `farewell` semantics:
    #   None      → use the configured language-specific static pitch
    #               (Twilio Polly/Google reads it before <Dial>)
    #   ""        → SUPPRESS the static <Say> entirely. Use this when
    #               Preeti has already spoken a dynamic pitch in her own
    #               voice before calling the tool, so Twilio doesn't read
    #               a duplicate over the top.
    #   "custom"  → speak this exact text instead of the default.
    if farewell is None:
        hold_text = _hold_message_for(language)
    else:
        hold_text = farewell

    say_xml = (
        f'<Say voice="{html.escape(voice)}" language="{lang_code}">'
        f"{html.escape(hold_text)}</Say>"
        if hold_text
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response>{say_xml}"
        f'<Dial callerId="{caller_id}" timeout="{timeout}">{target}</Dial>'
        "</Response>"
    )


async def transfer_to_human_agent(
    session: AsyncSession,
    payload: TransferToHumanInput,
    *,
    call_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    if not call_id:
        return {"success": False, "message": "no active call to transfer"}

    if not settings.support_admin_phone:
        return {"success": False, "message": "SUPPORT_ADMIN_PHONE not configured"}

    call = await CallRepository(session).get(call_id)
    if not call:
        return {"success": False, "message": "call not found"}
    if not call.twilio_call_sid:
        return {"success": False, "message": "no twilio call_sid on this call"}

    log_event_panel(
        "TRANSFER TO HUMAN",
        {
            "Call SID": call.twilio_call_sid,
            "Admin": settings.support_admin_phone,
            "Reason": payload.reason,
            "Language": payload.language,
        },
        style="yellow",
        icon_key="escalation",
    )

    twiml = _build_transfer_twiml(farewell=payload.farewell, language=payload.language)
    twilio_ok = CallService.hangup_twilio_call(call.twilio_call_sid, twiml=twiml)

    if not twilio_ok:
        await AgentToolEventRepository(session).add(
            call_id=call_id,
            tool_name="transfer_to_human_agent",
            input_json=payload.model_dump(),
            output_json={"twilio_ok": False},
            success=False,
            error_message="twilio update returned false (see TWILIO HANGUP FAILED panel)",
        )
        return {"success": False, "message": "twilio refused the TwiML update"}

    # Audit trail.
    esc = await EscalationRepository(session).create(
        call_id=call_id,
        reason=payload.reason,
        assigned_team="human-support",
    )
    await CallRepository(session).update_status(
        call_id, resolution_status=ResolutionStatus.escalated
    )
    await AgentToolEventRepository(session).add(
        call_id=call_id,
        tool_name="transfer_to_human_agent",
        input_json=payload.model_dump(),
        output_json={
            "escalation_id": str(esc.id),
            "to": settings.support_admin_phone,
        },
        success=True,
    )

    log_dataflow(
        "tool.transfer_to_human",
        f"bridging caller → {settings.support_admin_phone}",
        payload={"reason": payload.reason, "escalation_id": str(esc.id)},
    )

    return {
        "success": True,
        "message": "Caller is being connected to a human support agent.",
        "to": settings.support_admin_phone,
        "escalation_id": str(esc.id),
    }
