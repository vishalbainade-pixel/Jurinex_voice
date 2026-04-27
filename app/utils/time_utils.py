"""Datetime helpers — single source of truth for "now"."""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def date_compact(dt: datetime | None = None) -> str:
    """``YYYYMMDD`` — used in ticket numbers like JX-20260427-0001."""
    return (dt or utcnow()).strftime("%Y%m%d")
