"""initial schema

Revision ID: 20260427_0001
Revises:
Create Date: 2026-04-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260427_0001"
down_revision = None
branch_labels = None
depends_on = None


def _enum(*values: str, name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()

    call_direction = postgresql.ENUM("inbound", "outbound", name="call_direction")
    call_status = postgresql.ENUM(
        "started", "in_progress", "completed", "failed", name="call_status"
    )
    resolution_status = postgresql.ENUM(
        "resolved", "unresolved", "escalated", "unknown", name="resolution_status"
    )
    speaker = postgresql.ENUM("customer", "agent", "system", "tool", name="speaker")
    ticket_priority = postgresql.ENUM(
        "low", "normal", "high", "urgent", name="ticket_priority"
    )
    ticket_status = postgresql.ENUM(
        "open", "in_progress", "resolved", "escalated", name="ticket_status"
    )
    escalation_status = postgresql.ENUM(
        "pending", "assigned", "resolved", name="escalation_status"
    )

    for e in (
        call_direction,
        call_status,
        resolution_status,
        speaker,
        ticket_priority,
        ticket_status,
        escalation_status,
    ):
        e.create(bind, checkfirst=True)

    op.create_table(
        "customers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("phone_number", sa.String(32), nullable=False, unique=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("preferred_language", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_customers_phone_number", "customers", ["phone_number"], unique=True)
    op.create_index("ix_customers_email", "customers", ["email"])

    op.create_table(
        "calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("twilio_call_sid", sa.String(64), nullable=True, unique=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id"),
            nullable=True,
        ),
        sa.Column("customer_phone", sa.String(32), nullable=True),
        sa.Column("twilio_from", sa.String(32), nullable=True),
        sa.Column("twilio_to", sa.String(32), nullable=True),
        sa.Column("direction", _enum("inbound", "outbound", name="call_direction"), nullable=False),
        sa.Column(
            "status",
            _enum("started", "in_progress", "completed", "failed", name="call_status"),
            nullable=False,
        ),
        sa.Column("language", sa.String(32), nullable=True),
        sa.Column("issue_type", sa.String(128), nullable=True),
        sa.Column(
            "resolution_status",
            _enum("resolved", "unresolved", "escalated", "unknown", name="resolution_status"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer, nullable=True),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("sentiment", sa.String(32), nullable=True),
        sa.Column("created_ticket_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("raw_metadata", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_calls_twilio_call_sid", "calls", ["twilio_call_sid"], unique=True)
    op.create_index("ix_calls_customer_phone", "calls", ["customer_phone"])

    op.create_table(
        "call_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "call_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("calls.id"),
            nullable=False,
        ),
        sa.Column(
            "speaker",
            _enum("customer", "agent", "system", "tool", name="speaker"),
            nullable=False,
        ),
        sa.Column("language", sa.String(32), nullable=True),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("audio_event_id", sa.String(128), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_call_messages_call_id", "call_messages", ["call_id"])

    op.create_table(
        "support_tickets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ticket_number", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id"),
            nullable=True,
        ),
        sa.Column(
            "call_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("calls.id"),
            nullable=True,
        ),
        sa.Column("issue_type", sa.String(128), nullable=False),
        sa.Column("issue_summary", sa.Text, nullable=False),
        sa.Column(
            "priority",
            _enum("low", "normal", "high", "urgent", name="ticket_priority"),
            nullable=False,
        ),
        sa.Column(
            "status",
            _enum("open", "in_progress", "resolved", "escalated", name="ticket_status"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_support_tickets_ticket_number", "support_tickets", ["ticket_number"], unique=True)
    op.create_index("ix_support_tickets_issue_type", "support_tickets", ["issue_type"])

    op.create_table(
        "escalations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "call_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("calls.id"),
            nullable=False,
        ),
        sa.Column(
            "ticket_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("support_tickets.id"),
            nullable=True,
        ),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("assigned_team", sa.String(64), nullable=False),
        sa.Column(
            "status",
            _enum("pending", "assigned", "resolved", name="escalation_status"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "agent_tool_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "call_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("calls.id"),
            nullable=False,
        ),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("input_json", postgresql.JSONB, nullable=True),
        sa.Column("output_json", postgresql.JSONB, nullable=True),
        sa.Column("success", sa.Boolean, nullable=False),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "call_debug_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "call_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("calls.id"),
            nullable=True,
        ),
        sa.Column("twilio_call_sid", sa.String(64), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("event_stage", sa.String(64), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_call_debug_events_twilio_call_sid", "call_debug_events", ["twilio_call_sid"]
    )
    op.create_index("ix_call_debug_events_event_type", "call_debug_events", ["event_type"])
    op.create_index("ix_call_debug_events_event_stage", "call_debug_events", ["event_stage"])


def downgrade() -> None:
    op.drop_table("call_debug_events")
    op.drop_table("agent_tool_events")
    op.drop_table("escalations")
    op.drop_table("support_tickets")
    op.drop_table("call_messages")
    op.drop_table("calls")
    op.drop_table("customers")

    bind = op.get_bind()
    for name in (
        "call_direction",
        "call_status",
        "resolution_status",
        "speaker",
        "ticket_priority",
        "ticket_status",
        "escalation_status",
    ):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
