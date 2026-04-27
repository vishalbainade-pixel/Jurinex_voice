"""ORM models for the call-agent domain."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.utils.time_utils import utcnow


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CallDirection(str, enum.Enum):
    inbound = "inbound"
    outbound = "outbound"


class CallStatus(str, enum.Enum):
    started = "started"
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"


class ResolutionStatus(str, enum.Enum):
    resolved = "resolved"
    unresolved = "unresolved"
    escalated = "escalated"
    unknown = "unknown"


class Speaker(str, enum.Enum):
    customer = "customer"
    agent = "agent"
    system = "system"
    tool = "tool"


class TicketPriority(str, enum.Enum):
    low = "low"
    normal = "normal"
    high = "high"
    urgent = "urgent"


class TicketStatus(str, enum.Enum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"
    escalated = "escalated"


class EscalationStatus(str, enum.Enum):
    pending = "pending"
    assigned = "assigned"
    resolved = "resolved"


# ---------------------------------------------------------------------------
# Mixins
# ---------------------------------------------------------------------------


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    preferred_language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    calls: Mapped[list["Call"]] = relationship(back_populates="customer")


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[uuid.UUID] = _uuid_pk()
    twilio_call_sid: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True, nullable=True
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id"), nullable=True
    )
    customer_phone: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    twilio_from: Mapped[str | None] = mapped_column(String(32), nullable=True)
    twilio_to: Mapped[str | None] = mapped_column(String(32), nullable=True)
    direction: Mapped[CallDirection] = mapped_column(
        SAEnum(CallDirection, name="call_direction"), default=CallDirection.inbound
    )
    status: Mapped[CallStatus] = mapped_column(
        SAEnum(CallStatus, name="call_status"), default=CallStatus.started
    )
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    issue_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolution_status: Mapped[ResolutionStatus] = mapped_column(
        SAEnum(ResolutionStatus, name="resolution_status"),
        default=ResolutionStatus.unknown,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    sentiment: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    raw_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    customer: Mapped[Customer | None] = relationship(back_populates="calls")
    messages: Mapped[list["CallMessage"]] = relationship(
        back_populates="call", cascade="all, delete-orphan"
    )
    tool_events: Mapped[list["AgentToolEvent"]] = relationship(
        back_populates="call", cascade="all, delete-orphan"
    )


class CallMessage(Base):
    __tablename__ = "call_messages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id"), index=True
    )
    speaker: Mapped[Speaker] = mapped_column(SAEnum(Speaker, name="speaker"))
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    audio_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    call: Mapped[Call] = relationship(back_populates="messages")


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    ticket_number: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id"), nullable=True
    )
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id"), nullable=True
    )
    issue_type: Mapped[str] = mapped_column(String(128), index=True)
    issue_summary: Mapped[str] = mapped_column(Text)
    priority: Mapped[TicketPriority] = mapped_column(
        SAEnum(TicketPriority, name="ticket_priority"), default=TicketPriority.normal
    )
    status: Mapped[TicketStatus] = mapped_column(
        SAEnum(TicketStatus, name="ticket_status"), default=TicketStatus.open
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    call_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("calls.id"))
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("support_tickets.id"), nullable=True
    )
    reason: Mapped[str] = mapped_column(Text)
    assigned_team: Mapped[str] = mapped_column(String(64))
    status: Mapped[EscalationStatus] = mapped_column(
        SAEnum(EscalationStatus, name="escalation_status"),
        default=EscalationStatus.pending,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AgentToolEvent(Base):
    __tablename__ = "agent_tool_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    call_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("calls.id"))
    tool_name: Mapped[str] = mapped_column(String(128))
    input_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    call: Mapped[Call] = relationship(back_populates="tool_events")


class CallDebugEvent(Base):
    __tablename__ = "call_debug_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id"), nullable=True
    )
    twilio_call_sid: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    event_stage: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
