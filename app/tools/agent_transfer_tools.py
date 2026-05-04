"""``agent_transfer`` tool — swap to a different voice agent mid-call.

Operationally:

  1. The model calls ``agent_transfer(target_agent_name=...)`` (or by id).
  2. This handler resolves the target bundle, validates that it is active,
     and returns a result dict that includes a sentinel ``action: "swap_agent"``.
  3. The Twilio bridge inspects the dispatcher's return value (in
     ``_handle_tool_call``); when it sees ``swap_agent``, it:
       - sends the tool response back to the OLD model so the turn closes,
       - closes the OLD Gemini Live session,
       - opens a NEW Live session using the target bundle's
         live_model / voice / system instruction,
       - primes the new agent with the handoff message so it speaks first.

  Twilio Media Stream stays open the whole time — the caller never hears
  a hang-up.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.schemas import AgentTransferInput
from app.db.voice_agent_repository import AgentBundle, VoiceAgentRepository
from app.observability.logger import log_dataflow, log_event_panel


async def agent_transfer(
    session: AsyncSession,
    payload: AgentTransferInput,
    *,
    call_id: uuid.UUID | None = None,
    bundle: AgentBundle | None = None,
) -> dict[str, Any]:
    """Resolve the target agent bundle.

    Returns either an error result OR a success result that carries a
    ``next_bundle`` pointer (the bridge serializes the bundle id + name
    so the bridge knows which agent to hot-swap to). We deliberately do
    NOT mutate the live session here — that is the bridge's job.
    """
    repo = VoiceAgentRepository(session)
    target: AgentBundle | None = None

    if payload.target_agent_id:
        target = await repo.load_active_bundle_by_id(payload.target_agent_id)
    if target is None and payload.target_agent_name:
        target = await repo.load_active_bundle(payload.target_agent_name)

    if target is None:
        active = await repo.list_active_agents()
        names = [
            row["name"]
            for row in active
            if row["name"] != (bundle.name if bundle else None)
        ]
        log_dataflow(
            "tool.agent_transfer.not_found",
            f"requested name={payload.target_agent_name!r} "
            f"id={payload.target_agent_id!r} — choices={names}",
            level="warning",
        )
        return {
            "success": False,
            "message": (
                f"no active voice agent matches name={payload.target_agent_name!r} "
                f"or id={payload.target_agent_id!r}. "
                f"Active agents: {', '.join(names) or '(none)'}"
            ),
        }

    # Refuse no-op transfers — handing off to the SAME agent would just
    # waste a Gemini reconnect and probably loop.
    if bundle is not None and target.id == bundle.id:
        log_dataflow(
            "tool.agent_transfer.noop",
            f"target {target.name} is the current agent — refusing",
            level="warning",
        )
        return {
            "success": False,
            "message": (
                f"already on agent {target.name!r} — pick a different one"
            ),
        }

    log_event_panel(
        "AGENT TRANSFER",
        {
            "From": (bundle.name if bundle else "?"),
            "To": target.name,
            "Reason": payload.reason,
            "Language": payload.language,
        },
        style="magenta",
        icon_key="escalation",
    )
    log_dataflow(
        "tool.agent_transfer.resolved",
        f"target={target.name} (id={target.id}) "
        f"live_model={target.live_model} voice={target.voice_name}",
    )

    return {
        "success": True,
        "action": "swap_agent",
        "target_agent_id": str(target.id),
        "target_agent_name": target.name,
        "target_display_name": target.display_name,
        "handoff_message": payload.handoff_message
        or (
            target.transfer.handoff_message if target.transfer else None
        )
        or f"Connecting you to {target.display_name or target.name}.",
        "language": payload.language,
        "reason": payload.reason,
    }
