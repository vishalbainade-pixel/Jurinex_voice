"""Route tool calls coming from the LLM to the right handler.

In addition to executing the tool, this dispatcher writes one
``voice_tool_executions`` row per call (pending → completed/failed) so the
admin dashboard can render an audit trail. The legacy
``agent_tool_events`` rows continue to be written by the individual tool
handlers (search_knowledge_base, transfer_to_human_agent, etc.).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import AgentToolEventRepository
from app.db.schemas import (
    AgentTransferInput,
    CalendarBookInput,
    CalendarCheckInput,
    CheckCaseStatusInput,
    CreateSupportTicketInput,
    EndCallInput,
    EscalateToHumanInput,
    LookupCustomerInput,
    SearchKnowledgeBaseInput,
    TransferToHumanInput,
)
from app.db.voice_agent_repository import AgentBundle
from app.db.voice_tool_executions_repository import VoiceToolExecutionsRepository
from app.observability.logger import log_dataflow, log_error
from app.tools.agent_transfer_tools import agent_transfer
from app.tools.calendar_tools import calendar_book, calendar_check
from app.tools.call_tools import end_call
from app.tools.case_tools import check_case_status
from app.tools.customer_tools import lookup_customer
from app.tools.escalation_tools import escalate_to_human
from app.tools.kb_tools import search_knowledge_base
from app.tools.ticket_tools import create_support_ticket
from app.tools.transfer_tools import transfer_to_human_agent


# Admin-table tool name → bridge handler name. Both names land on the same
# handler so the model can use whichever name appears in its system prompt.
_TOOL_ALIASES: dict[str, str] = {
    "transfer_call": "transfer_to_human_agent",
}


def _canonicalize(tool_name: str) -> str:
    return _TOOL_ALIASES.get(tool_name, tool_name)


async def dispatch_tool_call(
    *,
    session: AsyncSession,
    call_id: uuid.UUID | None,
    tool_name: str,
    arguments: dict[str, Any],
    bundle: AgentBundle | None = None,
    voice_session_id: uuid.UUID | None = None,
    trace_id: uuid.UUID | None = None,
    function_call_id: str | None = None,
) -> dict[str, Any]:
    """Look up and run a tool by name, persisting both audit rows.

    Parameters mostly come from the live bridge; ``bundle`` lets the
    transfer handler pull the destination from the admin's
    ``voice_agent_transfer_configs`` table instead of falling back to env.
    """
    canonical = _canonicalize(tool_name)
    log_dataflow(
        "tool.dispatch",
        f"{tool_name} → {canonical}" if canonical != tool_name else canonical,
        payload=arguments,
    )

    # ── voice_tool_executions: pending row ──
    exec_repo = VoiceToolExecutionsRepository(session)
    execution_id = uuid.uuid4()
    started_at = time.monotonic()
    try:
        await exec_repo.insert_pending(
            execution_id=execution_id,
            tool_name=tool_name,  # store the name the model used
            input_payload=arguments,
            agent_id=(bundle.id if bundle is not None else None),
            session_id=voice_session_id,
            trace_id=trace_id,
            function_call_id=function_call_id,
        )
    except Exception as exc:
        # Audit failure must never block tool execution.
        log_dataflow(
            "tool_exec.insert_error",
            f"could not insert pending row: {exc}",
            level="warning",
        )

    result: dict[str, Any]
    success = True
    error_message: str | None = None
    try:
        if canonical == "create_support_ticket":
            payload = CreateSupportTicketInput(**arguments)
            result_obj = await create_support_ticket(session, payload, call_id=call_id)
            result = result_obj.model_dump()

        elif canonical == "lookup_customer":
            payload_lc = LookupCustomerInput(**arguments)
            result_obj = await lookup_customer(session, payload_lc)
            result = result_obj.model_dump()

        elif canonical == "check_case_status":
            payload_cc = CheckCaseStatusInput(**arguments)
            result = await check_case_status(payload_cc)

        elif canonical == "escalate_to_human":
            args = dict(arguments)
            if call_id and "call_id" not in args:
                args["call_id"] = str(call_id)
            payload_es = EscalateToHumanInput(**args)
            result = await escalate_to_human(session, payload_es)

        elif canonical == "end_call":
            args = dict(arguments)
            if call_id and "call_id" not in args:
                args["call_id"] = str(call_id)
            payload_ec = EndCallInput(**args)
            result = await end_call(session, payload_ec)

        elif canonical == "search_knowledge_base":
            payload_kb = SearchKnowledgeBaseInput(**arguments)
            result = await search_knowledge_base(session, payload_kb, call_id=call_id)

        elif canonical == "transfer_to_human_agent":
            payload_tr = TransferToHumanInput(**arguments)
            result = await transfer_to_human_agent(
                session, payload_tr, call_id=call_id, bundle=bundle
            )

        elif canonical == "calendar_check":
            payload_cal = CalendarCheckInput(**arguments)
            result = await calendar_check(
                session, payload_cal, call_id=call_id, bundle=bundle
            )

        elif canonical == "calendar_book":
            payload_cb = CalendarBookInput(**arguments)
            result = await calendar_book(
                session,
                payload_cb,
                call_id=call_id,
                bundle=bundle,
                voice_session_id=voice_session_id,
                tool_execution_id=execution_id,
            )

        elif canonical == "agent_transfer":
            payload_at = AgentTransferInput(**arguments)
            result = await agent_transfer(
                session, payload_at, call_id=call_id, bundle=bundle
            )
        else:
            success = False
            result = {"success": False, "message": f"unknown tool: {tool_name}"}

        if isinstance(result, dict) and result.get("success") is False:
            success = False
            error_message = result.get("message")

    except Exception as exc:
        success = False
        error_message = str(exc)
        log_error(
            "TOOL DISPATCH ERROR",
            f"{tool_name}: {exc}",
            {"args": str(arguments)[:200]},
        )
        if call_id:
            await AgentToolEventRepository(session).add(
                call_id=call_id,
                tool_name=tool_name,
                input_json=arguments,
                output_json=None,
                success=False,
                error_message=error_message,
            )
        result = {"success": False, "message": error_message}

    # ── voice_tool_executions: completed/failed row ──
    latency_ms = int((time.monotonic() - started_at) * 1000)
    try:
        await exec_repo.mark_completed(
            execution_id=execution_id,
            output_payload=result,
            latency_ms=latency_ms,
            success=success,
            error_message=error_message,
        )
    except Exception as exc:
        log_dataflow(
            "tool_exec.update_error",
            f"could not finalize execution row: {exc}",
            level="warning",
        )

    return result
