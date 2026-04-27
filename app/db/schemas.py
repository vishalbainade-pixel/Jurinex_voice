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
