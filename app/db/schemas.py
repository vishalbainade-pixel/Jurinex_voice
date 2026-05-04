"""Pydantic v2 request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Outbound call API
# ---------------------------------------------------------------------------


class OutboundCallRequest(BaseModel):
    to_phone_number: str
    customer_name: str | None = None
    language_hint: Literal["English", "Hindi", "Marathi"] | None = None
    reason: str | None = None


class OutboundCallResponse(BaseModel):
    call_sid: str
    status: str
    to: str
    from_: str = Field(alias="from")

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------------
# Tool inputs
# ---------------------------------------------------------------------------


class CreateSupportTicketInput(BaseModel):
    customer_name: str | None = None
    phone_number: str | None = None
    email: str | None = None
    issue_type: str
    issue_summary: str
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    language: Literal["English", "Hindi", "Marathi"] = "English"


class CreateSupportTicketOutput(BaseModel):
    success: bool
    ticket_number: str | None = None
    message: str


class LookupCustomerInput(BaseModel):
    phone_number: str


class LookupCustomerOutput(BaseModel):
    success: bool
    customer_id: str | None = None
    name: str | None = None
    preferred_language: str | None = None
    is_new_customer: bool = False
    message: str = ""


class CheckCaseStatusInput(BaseModel):
    case_id: str


class EscalateToHumanInput(BaseModel):
    call_id: str
    reason: str
    assigned_team: str = "tier-2-support"


class EndCallInput(BaseModel):
    call_id: str
    reason: str | None = None


class SearchKnowledgeBaseInput(BaseModel):
    query: str
    k: int = 5


class TransferToHumanInput(BaseModel):
    reason: str = "general_support"
    farewell: str | None = None  # optional message Preeti wants spoken before transfer
    language: Literal["English", "Hindi", "Marathi"] = "English"
    # Optional explicit destination. Used in DYNAMIC routing mode where
    # the model is told (via the admin's destination_prompt) which number
    # to dial based on the caller's intent. In STATIC mode this is ignored
    # and the bundle's static_destination wins.
    destination_phone: str | None = None


class CalendarCheckInput(BaseModel):
    """Inputs for ``calendar_check`` — read-only availability lookup.

    ``start_iso`` / ``end_iso`` MUST be ISO 8601 with TZ offset
    (e.g. ``2026-05-04T09:00:00+05:30``). The bridge tightens the window to
    the agent's working hours before responding.
    """

    start_iso: str
    end_iso: str


class CalendarBookInput(BaseModel):
    """Inputs for ``calendar_book`` — create a Google Calendar event."""

    start_iso: str
    end_iso: str
    summary: str
    attendee_name: str | None = None
    attendee_email: str | None = None
    attendee_phone: str | None = None
    description: str | None = None


class AgentTransferInput(BaseModel):
    """Inputs for ``agent_transfer`` — switch to a different voice agent.

    Either ``target_agent_name`` (preferred — matches voice_agents.name) or
    ``target_agent_id`` (the UUID) MUST be provided. Both are accepted so
    the model can use whichever the admin's tool prompt mentions.
    """

    target_agent_name: str | None = None
    target_agent_id: str | None = None
    reason: str = "intent_changed"
    handoff_message: str | None = None  # one-line note the new agent should read aloud
    language: Literal["English", "Hindi", "Marathi"] = "English"


# ---------------------------------------------------------------------------
# Admin output schemas
# ---------------------------------------------------------------------------


class CallSummary(BaseModel):
    id: uuid.UUID
    twilio_call_sid: str | None
    direction: str
    status: str
    customer_phone: str | None
    language: str | None
    issue_type: str | None
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: int | None
    summary: str | None

    model_config = ConfigDict(from_attributes=True)


class TicketSummary(BaseModel):
    id: uuid.UUID
    ticket_number: str
    issue_type: str
    issue_summary: str
    priority: str
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DebugEvent(BaseModel):
    id: uuid.UUID
    call_id: uuid.UUID | None
    twilio_call_sid: str | None
    event_type: str
    event_stage: str
    message: str
    payload: dict[str, Any] | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Demo simulation
# ---------------------------------------------------------------------------


class SimulateConversationRequest(BaseModel):
    phone_number: str
    messages: list[str]
    customer_name: str | None = None


class SimulateConversationResponse(BaseModel):
    call_id: str
    transcript: list[dict[str, Any]]
    ticket_created: bool
    ticket_number: str | None = None
