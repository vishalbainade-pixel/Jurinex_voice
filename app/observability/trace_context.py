"""Per-call trace context using contextvars so every log line is attributable."""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TraceContext:
    """Lightweight per-call context propagated via contextvars."""

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    call_sid: str | None = None
    direction: str | None = None
    customer_phone: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "call_sid": self.call_sid,
            "direction": self.direction,
            "customer_phone": self.customer_phone,
            **self.extra,
        }


_current_trace: ContextVar[TraceContext | None] = ContextVar(
    "jurinex_trace_context", default=None
)


def new_trace(
    *,
    call_sid: str | None = None,
    direction: str | None = None,
    customer_phone: str | None = None,
) -> TraceContext:
    """Create and bind a new trace context for the current async task."""
    ctx = TraceContext(
        call_sid=call_sid,
        direction=direction,
        customer_phone=customer_phone,
    )
    _current_trace.set(ctx)
    return ctx


def get_trace() -> TraceContext:
    """Return the active trace context, creating an empty one on demand."""
    ctx = _current_trace.get()
    if ctx is None:
        ctx = TraceContext()
        _current_trace.set(ctx)
    return ctx


def set_trace(ctx: TraceContext) -> None:
    _current_trace.set(ctx)


def update_trace(**fields: Any) -> TraceContext:
    """Mutate fields on the active trace context."""
    ctx = get_trace()
    for k, v in fields.items():
        if hasattr(ctx, k):
            setattr(ctx, k, v)
        else:
            ctx.extra[k] = v
    return ctx
