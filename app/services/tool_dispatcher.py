"""Route tool calls coming from the LLM to the right handler."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import AgentToolEventRepository
from app.db.schemas import (
    CheckCaseStatusInput,
    CreateSupportTicketInput,
    EndCallInput,
    EscalateToHumanInput,
    LookupCustomerInput,
)
from app.observability.logger import log_dataflow, log_error
from app.tools.call_tools import end_call
from app.tools.case_tools import check_case_status
from app.tools.customer_tools import lookup_customer
from app.tools.escalation_tools import escalate_to_human
from app.tools.ticket_tools import create_support_ticket


async def dispatch_tool_call(
    *,
    session: AsyncSession,
    call_id: uuid.UUID | None,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Look up and run a tool by name, persisting an audit row."""
    log_dataflow("tool.dispatch", tool_name, payload=arguments)

    try:
        if tool_name == "create_support_ticket":
            payload = CreateSupportTicketInput(**arguments)
            result = await create_support_ticket(session, payload, call_id=call_id)
            return result.model_dump()

        if tool_name == "lookup_customer":
            payload = LookupCustomerInput(**arguments)
            result = await lookup_customer(session, payload)
            return result.model_dump()

        if tool_name == "check_case_status":
            payload = CheckCaseStatusInput(**arguments)
            return await check_case_status(payload)

        if tool_name == "escalate_to_human":
            args = dict(arguments)
            if call_id and "call_id" not in args:
                args["call_id"] = str(call_id)
            payload = EscalateToHumanInput(**args)
            return await escalate_to_human(session, payload)

        if tool_name == "end_call":
            args = dict(arguments)
            if call_id and "call_id" not in args:
                args["call_id"] = str(call_id)
            payload = EndCallInput(**args)
            return await end_call(session, payload)

        return {"success": False, "message": f"unknown tool: {tool_name}"}

    except Exception as exc:
        log_error("TOOL DISPATCH ERROR", f"{tool_name}: {exc}", {"args": str(arguments)[:200]})
        if call_id:
            await AgentToolEventRepository(session).add(
                call_id=call_id,
                tool_name=tool_name,
                input_json=arguments,
                output_json=None,
                success=False,
                error_message=str(exc),
            )
        return {"success": False, "message": str(exc)}
