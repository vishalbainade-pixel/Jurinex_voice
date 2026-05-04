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
from app.db.voice_agent_repository import AgentBundle
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


import re as _re

_E164_RE = _re.compile(r"\+\d{6,15}")


def _normalize_e164(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = _re.sub(r"[^\d+]", "", value)
    if cleaned and not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    return cleaned or None


def _resolve_destination(
    bundle: AgentBundle | None,
    explicit: str | None,
) -> tuple[str, str | None]:
    """Pick the phone number to dial.

    Returns ``(destination, error_message)``. ``destination`` is empty when
    we couldn't resolve one — the caller-side handler turns ``error_message``
    into the tool result so the model retries with a corrected argument
    instead of dialing a hallucinated number.

    Priority:
      * Static routing → ``bundle.transfer.static_destination`` (ignores
        ``explicit``; the admin pinned a single number).
      * Dynamic routing → ``explicit`` MUST be one of the E.164 numbers
        embedded in ``bundle.transfer.destination_prompt``. Otherwise we
        return an error so the model picks again.
      * Legacy / no transfer config → fall back to settings.support_admin_phone.
    """
    transfer = bundle.transfer if bundle else None

    if transfer and transfer.routing_mode == "static":
        if transfer.static_destination:
            return transfer.static_destination.strip(), None
        return "", "transfer is configured as static but static_destination is empty"

    if transfer and transfer.routing_mode == "dynamic":
        allowed = set(_E164_RE.findall(transfer.destination_prompt or ""))
        if not allowed:
            return "", (
                "dynamic routing has no E.164 numbers in destination_prompt"
            )
        candidate = _normalize_e164(explicit)
        if not candidate:
            return "", (
                "dynamic routing requires destination_phone — pick one of: "
                + ", ".join(sorted(allowed))
            )
        if candidate not in allowed:
            return "", (
                f"destination_phone {candidate!r} is not in the allowed "
                f"routing list ({', '.join(sorted(allowed))}). Re-issue the "
                f"tool call with one of the allowed numbers."
            )
        return candidate, None

    # No transfer config at all → legacy env fallback.
    legacy = (_normalize_e164(explicit) or settings.support_admin_phone or "").strip()
    return legacy, None if legacy else "no transfer destination configured"


def _build_transfer_twiml(
    *,
    farewell: str | None,
    language: str,
    destination: str,
    ring_seconds: int | None = None,
) -> str:
    target = html.escape(destination)
    caller_id = html.escape(settings.twilio_phone_number)
    timeout = max(
        5,
        int(ring_seconds or settings.transfer_dial_timeout_seconds),
    )

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
    bundle: AgentBundle | None = None,
) -> dict[str, Any]:
    if not call_id:
        return {"success": False, "message": "no active call to transfer"}

    destination, dest_error = _resolve_destination(bundle, payload.destination_phone)
    if not destination:
        log_dataflow(
            "tool.transfer.rejected",
            f"reason={dest_error} requested={payload.destination_phone!r}",
            level="warning",
        )
        return {
            "success": False,
            "message": dest_error
            or (
                "transfer destination not configured "
                "(neither voice_agent_transfer_configs nor SUPPORT_ADMIN_PHONE "
                "is set, and no destination_phone was provided)"
            ),
        }

    call = await CallRepository(session).get(call_id)
    if not call:
        return {"success": False, "message": "call not found"}
    if not call.twilio_call_sid:
        return {"success": False, "message": "no twilio call_sid on this call"}

    routing_mode = bundle.transfer.routing_mode if bundle and bundle.transfer else "legacy"
    transfer_type = bundle.transfer.transfer_type if bundle and bundle.transfer else "warm"
    ring_seconds = (
        bundle.transfer.ring_duration_seconds if bundle and bundle.transfer else None
    )

    log_event_panel(
        "TRANSFER TO HUMAN",
        {
            "Call SID": call.twilio_call_sid,
            "Destination": destination,
            "Routing": routing_mode,
            "Type": transfer_type,
            "Reason": payload.reason,
            "Language": payload.language,
        },
        style="yellow",
        icon_key="escalation",
    )

    twiml = _build_transfer_twiml(
        farewell=payload.farewell,
        language=payload.language,
        destination=destination,
        ring_seconds=ring_seconds,
    )
    twilio_ok = CallService.hangup_twilio_call(call.twilio_call_sid, twiml=twiml)

    if not twilio_ok:
        await AgentToolEventRepository(session).add(
            call_id=call_id,
            tool_name="transfer_to_human_agent",
            input_json=payload.model_dump(),
            output_json={"twilio_ok": False, "destination": destination},
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
            "to": destination,
            "routing_mode": routing_mode,
            "transfer_type": transfer_type,
        },
        success=True,
    )

    log_dataflow(
        "tool.transfer_to_human",
        f"bridging caller → {destination} ({routing_mode}/{transfer_type})",
        payload={"reason": payload.reason, "escalation_id": str(esc.id)},
    )

    return {
        "success": True,
        "message": "Caller is being connected to a human support agent.",
        "to": destination,
        "routing_mode": routing_mode,
        "transfer_type": transfer_type,
        "escalation_id": str(esc.id),
    }
