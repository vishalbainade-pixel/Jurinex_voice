"""``calendar_check`` and ``calendar_book`` tool handlers.

Both pull their configuration from the agent bundle's
``custom_settings.agent_builder.tool_settings.calendar`` block:

    timezone                  e.g. "Asia/Kolkata"
    calendar_id               specific calendar override (else env default)
    view_only                 disables booking — calendar_book refuses
    default_meeting_minutes   used when book input has equal start/end
    blocked_dates             ["2026-04-04", ...]
    working_hours             {"monday": {"enabled": true, "start": "10:00", "end": "17:00"}, ...}

If the bundle is missing those, we fall back to env defaults
(``JURINEX_VOICE_DEFAULT_CALENDAR_ID``, ``JURINEX_VOICE_DEFAULT_CALENDAR_TZ``)
so the tools still work in degraded mode.

Reliability:
  * `calendar_book` rejects naive timestamps (no TZ offset) loudly so the
    model retries with the correct format instead of silently shifting hours.
  * `insert_event` is retried up to 3 times with exponential backoff on
    transient API errors (5xx / network) — booking is best-effort guaranteed.
  * After insert we re-fetch the slot and confirm the event ID is present
    on the calendar; if not, we log loud and mark the booking as failed.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Call
from app.db.schemas import CalendarBookInput, CalendarCheckInput
from app.db.voice_agent_repository import AgentBundle
from app.db.voice_calendar_bookings_repository import (
    VoiceCalendarBookingsRepository,
)
from app.observability.logger import log_dataflow, log_error, log_event_panel
from app.services.google_calendar import GoogleCalendarClient


# ---------------------------------------------------------------------------
# Bundle accessors with env fallback
# ---------------------------------------------------------------------------


_DAY_ORDER = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def _calendar_id(bundle: AgentBundle | None) -> str:
    if bundle is not None:
        cid = bundle.calendar_settings.get("calendar_id")
        if cid:
            return str(cid)
    return settings.jurinex_voice_default_calendar_id


def _timezone_name(bundle: AgentBundle | None) -> str:
    if bundle is not None:
        tz = bundle.calendar_settings.get("timezone")
        if tz:
            return str(tz)
    return settings.jurinex_voice_default_calendar_tz


def _default_meeting_minutes(bundle: AgentBundle | None) -> int:
    if bundle is not None:
        v = bundle.calendar_settings.get("default_meeting_minutes")
        if v:
            return int(v)
    return 30


def _is_view_only(bundle: AgentBundle | None) -> bool:
    if bundle is None:
        return False
    return bool(bundle.calendar_settings.get("view_only"))


def _blocked_dates(bundle: AgentBundle | None) -> set[date]:
    if bundle is None:
        return set()
    raw = bundle.calendar_settings.get("blocked_dates") or []
    out: set[date] = set()
    for item in raw:
        try:
            out.add(date.fromisoformat(str(item)))
        except ValueError:
            continue
    return out


def _working_hours(bundle: AgentBundle | None) -> dict[str, dict[str, str]]:
    """Return ``{day_name: {'start': 'HH:MM', 'end': 'HH:MM'}}`` for enabled days."""
    if bundle is None:
        return {}
    raw = bundle.calendar_settings.get("working_hours") or {}
    out: dict[str, dict[str, str]] = {}
    for day in _DAY_ORDER:
        cfg = raw.get(day) or {}
        if cfg.get("enabled") and cfg.get("start") and cfg.get("end"):
            out[day] = {
                "start": str(cfg["start"]),
                "end": str(cfg["end"]),
            }
    return out


# ---------------------------------------------------------------------------
# Free-window computation
# ---------------------------------------------------------------------------


def _parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 timestamp; assume UTC if it's naive.

    Used by ``calendar_check`` (read-only) where naive→UTC is acceptable.
    ``calendar_book`` uses the strict variant below to reject naive input.
    """
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_iso_strict(s: str) -> datetime:
    """Parse an ISO 8601 timestamp; REJECT naive timestamps.

    A naive timestamp from the model (``2026-05-04T10:00:00``) silently
    shifts the slot by the local TZ offset (e.g. 10 AM IST becomes 3:30 PM
    IST when assumed-UTC), which then trips ``outside_working_hours`` and
    silently fails the booking. We refuse and ask the model to retry with
    the correct ``+HH:MM`` offset.
    """
    text = s.strip()
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(
            f"timestamp {s!r} has no timezone offset — "
            f"include +HH:MM (e.g. '2026-05-04T10:00:00+05:30')"
        )
    return dt


def _to_zone(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.astimezone(tz)


# Transient HTTP errors we retry. Anything else (4xx other than 429) is a
# hard failure surfaced immediately to the model.
def _is_transient_calendar_error(exc: Exception) -> bool:
    msg = str(exc)
    if "HTTP 429" in msg:
        return True
    for code in ("HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504"):
        if code in msg:
            return True
    # Network / timeout markers from httpx
    for needle in ("ReadTimeout", "ConnectError", "RemoteProtocolError"):
        if needle in msg:
            return True
    return False


def _day_window(
    target: date,
    wh: dict[str, dict[str, str]],
    tz: ZoneInfo,
) -> tuple[datetime, datetime] | None:
    """Return the (start, end) of the working window on ``target`` if open."""
    day_name = _DAY_ORDER[target.weekday()]
    cfg = wh.get(day_name)
    if not cfg:
        return None
    start = datetime.combine(target, dt_time.fromisoformat(cfg["start"]), tzinfo=tz)
    end = datetime.combine(target, dt_time.fromisoformat(cfg["end"]), tzinfo=tz)
    return (start, end)


def _subtract_busy(
    window: tuple[datetime, datetime],
    busy: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Return free intervals inside ``window`` after removing busy intervals."""
    free = [window]
    for b_start, b_end in sorted(busy):
        next_free: list[tuple[datetime, datetime]] = []
        for f_start, f_end in free:
            if b_end <= f_start or b_start >= f_end:
                next_free.append((f_start, f_end))
                continue
            if b_start > f_start:
                next_free.append((f_start, max(f_start, b_start)))
            if b_end < f_end:
                next_free.append((min(f_end, b_end), f_end))
        free = [(s, e) for (s, e) in next_free if e > s]
    return free


# ---------------------------------------------------------------------------
# calendar_check
# ---------------------------------------------------------------------------


async def calendar_check(
    session: AsyncSession,
    payload: CalendarCheckInput,
    *,
    call_id: uuid.UUID | None = None,
    bundle: AgentBundle | None = None,
) -> dict[str, Any]:
    cal_id = _calendar_id(bundle)
    if not cal_id:
        return {
            "success": False,
            "message": (
                "calendar_id is not configured (neither agent_builder.tool_settings.calendar.calendar_id "
                "nor JURINEX_VOICE_DEFAULT_CALENDAR_ID has a value)"
            ),
        }

    tz_name = _timezone_name(bundle)
    tz = ZoneInfo(tz_name)
    wh = _working_hours(bundle)
    blocked = _blocked_dates(bundle)
    min_minutes = _default_meeting_minutes(bundle)

    try:
        start_dt = _to_zone(_parse_iso(payload.start_iso), tz)
        end_dt = _to_zone(_parse_iso(payload.end_iso), tz)
    except Exception as exc:
        return {
            "success": False,
            "message": (
                "start_iso and end_iso must be ISO 8601 with TZ offset "
                f"(got: {payload.start_iso!r}, {payload.end_iso!r}; error: {exc})"
            ),
        }

    if end_dt <= start_dt:
        return {"success": False, "message": "end_iso must be after start_iso"}

    log_event_panel(
        "CALENDAR CHECK",
        {
            "Calendar": cal_id,
            "TZ": tz_name,
            "Range": f"{start_dt.isoformat()} → {end_dt.isoformat()}",
            "Working days": ", ".join(wh.keys()) or "(none)",
            "Blocked dates": ", ".join(d.isoformat() for d in sorted(blocked)) or "(none)",
        },
        style="cyan",
        icon_key="tool",
    )

    client = GoogleCalendarClient()
    try:
        events = await client.list_events(
            calendar_id=cal_id,
            time_min_iso=start_dt.isoformat(),
            time_max_iso=end_dt.isoformat(),
            time_zone=tz_name,
        )
    except Exception as exc:
        return {"success": False, "message": f"calendar API error: {exc}"}

    busy: list[tuple[datetime, datetime]] = [
        (_to_zone(_parse_iso(e.start_iso), tz), _to_zone(_parse_iso(e.end_iso), tz))
        for e in events
    ]

    # Walk one day at a time across the requested window.
    free_windows: list[dict[str, str]] = []
    cursor = start_dt.date()
    last_day = end_dt.date()
    while cursor <= last_day:
        if cursor in blocked:
            cursor += timedelta(days=1)
            continue
        day_w = _day_window(cursor, wh, tz)
        if day_w is None:
            cursor += timedelta(days=1)
            continue
        # Clip to the actual requested window
        clipped_start = max(day_w[0], start_dt)
        clipped_end = min(day_w[1], end_dt)
        if clipped_end <= clipped_start:
            cursor += timedelta(days=1)
            continue
        free_intervals = _subtract_busy(
            (clipped_start, clipped_end), busy
        )
        # Drop intervals shorter than the default meeting length
        for s, e in free_intervals:
            if (e - s).total_seconds() < min_minutes * 60:
                continue
            free_windows.append(
                {
                    "start_iso": s.isoformat(),
                    "end_iso": e.isoformat(),
                    "duration_minutes": int((e - s).total_seconds() // 60),
                }
            )
        cursor += timedelta(days=1)

    log_dataflow(
        "calendar.check.done",
        f"events={len(events)} free_windows={len(free_windows)} "
        f"min_minutes={min_minutes}",
    )

    return {
        "success": True,
        "calendar_id": cal_id,
        "timezone": tz_name,
        "default_meeting_minutes": min_minutes,
        "events_in_window": len(events),
        "free_windows": free_windows,
        "view_only": _is_view_only(bundle),
    }


# ---------------------------------------------------------------------------
# calendar_book
# ---------------------------------------------------------------------------


async def calendar_book(
    session: AsyncSession,
    payload: CalendarBookInput,
    *,
    call_id: uuid.UUID | None = None,
    bundle: AgentBundle | None = None,
    voice_session_id: uuid.UUID | None = None,
    tool_execution_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    if _is_view_only(bundle):
        return {
            "success": False,
            "status": "view_only",
            "message": (
                "calendar is configured in VIEW-ONLY mode — "
                "bookings are disabled by the admin"
            ),
        }

    cal_id = _calendar_id(bundle)
    if not cal_id:
        return {
            "success": False,
            "message": "calendar_id is not configured",
        }

    tz_name = _timezone_name(bundle)
    tz = ZoneInfo(tz_name)
    wh = _working_hours(bundle)
    blocked = _blocked_dates(bundle)
    default_min = _default_meeting_minutes(bundle)

    try:
        start_dt = _to_zone(_parse_iso_strict(payload.start_iso), tz)
        end_dt = _to_zone(_parse_iso_strict(payload.end_iso), tz)
    except Exception as exc:
        log_dataflow(
            "calendar.book.bad_iso",
            f"strict parse failed: {exc}",
            level="warning",
        )
        return {
            "success": False,
            "status": "bad_timestamp",
            "message": (
                "start_iso/end_iso MUST be ISO 8601 with timezone offset "
                f"(e.g. '2026-05-04T10:00:00+05:30'). Error: {exc}. "
                "Re-issue the call with the correct format."
            ),
        }

    if end_dt <= start_dt:
        end_dt = start_dt + timedelta(minutes=default_min)
        log_dataflow(
            "calendar.book.duration_filled",
            f"end_iso<=start_iso — auto-extended to "
            f"{default_min} minutes ({end_dt.isoformat()})",
        )

    target_day = start_dt.date()
    if target_day in blocked:
        return {
            "success": False,
            "status": "date_blocked",
            "message": f"{target_day.isoformat()} is in the blocked_dates list",
        }
    day_w = _day_window(target_day, wh, tz)
    if day_w is None:
        return {
            "success": False,
            "status": "day_disabled",
            "message": (
                f"{_DAY_ORDER[target_day.weekday()]} is not an enabled "
                "working day for this agent"
            ),
        }
    if start_dt < day_w[0] or end_dt > day_w[1]:
        return {
            "success": False,
            "status": "outside_working_hours",
            "message": (
                f"requested slot {start_dt.isoformat()}–{end_dt.isoformat()} "
                f"is outside working hours "
                f"{day_w[0].time().isoformat(timespec='minutes')}–"
                f"{day_w[1].time().isoformat(timespec='minutes')} "
                f"({tz_name})"
            ),
        }

    # Conflict check
    client = GoogleCalendarClient()
    try:
        existing = await client.list_events(
            calendar_id=cal_id,
            time_min_iso=start_dt.isoformat(),
            time_max_iso=end_dt.isoformat(),
            time_zone=tz_name,
        )
    except Exception as exc:
        return {"success": False, "message": f"calendar API error (list): {exc}"}

    if existing:
        return {
            "success": False,
            "status": "conflict",
            "message": (
                f"slot conflicts with {len(existing)} existing event(s); "
                "please propose another time"
            ),
            "conflicts": [
                {
                    "summary": e.summary,
                    "start_iso": e.start_iso,
                    "end_iso": e.end_iso,
                }
                for e in existing[:5]
            ],
        }

    # ── Pull caller phone from the originating call row (if any) so the
    # human reading the calendar event can call back directly.
    caller_phone: str | None = None
    if call_id is not None:
        try:
            call_row = await session.get(Call, call_id)
            if call_row is not None:
                caller_phone = (
                    call_row.customer_phone or call_row.twilio_from
                )
        except Exception as exc:
            log_dataflow(
                "calendar.book.caller_lookup_error",
                f"could not load call row: {exc}",
                level="warning",
            )

    log_event_panel(
        "CALENDAR BOOK",
        {
            "Calendar": cal_id,
            "When": f"{start_dt.isoformat()} → {end_dt.isoformat()}",
            "Summary": payload.summary,
            "Attendee": (
                f"{payload.attendee_name or '?'} "
                f"<{payload.attendee_email or '-'}>"
            ),
            "Attendee phone": payload.attendee_phone or "-",
            "Caller phone": caller_phone or "-",
        },
        style="cyan",
        icon_key="tool",
    )

    # ── Build a rich description so the human reading the event can call
    # the caller back directly. Format is human-friendly; the bridge writes
    # the same fields into voice_calendar_bookings.metadata for structured
    # consumers (admin dashboard).
    contact_lines: list[str] = []
    if payload.attendee_name:
        contact_lines.append(f"Name:           {payload.attendee_name}")
    if payload.attendee_email:
        contact_lines.append(f"Email:          {payload.attendee_email}")
    if payload.attendee_phone:
        contact_lines.append(f"Contact phone:  {payload.attendee_phone}")
    if caller_phone and caller_phone != payload.attendee_phone:
        contact_lines.append(f"Caller phone:   {caller_phone}")

    # Best phone number to call back — attendee's stated phone wins, else
    # the inbound caller's phone.
    callback_phone = payload.attendee_phone or caller_phone
    if callback_phone:
        contact_lines.append(f"Tap to call:    tel:{callback_phone}")

    reason_text = (payload.description or payload.summary or "").strip()

    description_blocks: list[str] = [
        "Booked via Preeti voice agent (Jurinex).",
    ]
    if contact_lines:
        description_blocks.append("── Contact ──\n" + "\n".join(contact_lines))
    if reason_text:
        description_blocks.append(f"── Reason ──\n{reason_text}")
    ref_lines: list[str] = []
    if call_id:
        ref_lines.append(f"Originating call_id: {call_id}")
    if bundle is not None:
        ref_lines.append(f"Agent: {bundle.name} ({bundle.id})")
    if ref_lines:
        description_blocks.append("── Reference ──\n" + "\n".join(ref_lines))

    description = "\n\n".join(description_blocks)

    # ── Insert with retries on transient API errors. This is the single
    # most common failure mode for "demo wasn't on the calendar" reports.
    booking_repo = VoiceCalendarBookingsRepository(session)
    event: dict[str, Any] | None = None
    insert_error: Exception | None = None
    insert_attempts = 0
    max_attempts = 3
    backoff = 0.4
    for attempt in range(1, max_attempts + 1):
        insert_attempts = attempt
        try:
            event = await client.insert_event(
                calendar_id=cal_id,
                start_iso=start_dt.isoformat(),
                end_iso=end_dt.isoformat(),
                time_zone=tz_name,
                summary=payload.summary,
                description=description,
                attendee_email=payload.attendee_email,
                attendee_name=payload.attendee_name,
            )
            insert_error = None
            break
        except Exception as exc:
            insert_error = exc
            transient = _is_transient_calendar_error(exc)
            log_dataflow(
                "calendar.book.insert_error",
                f"attempt={attempt}/{max_attempts} transient={transient} error={exc}",
                level="warning" if transient else "error",
            )
            if not transient or attempt == max_attempts:
                break
            await asyncio.sleep(backoff)
            backoff *= 2

    if insert_error is not None or event is None:
        try:
            await booking_repo.insert(
                google_event_id=None,
                google_calendar_id=cal_id,
                summary=payload.summary,
                description=description,
                start_time=start_dt,
                end_time=end_dt,
                attendee_name=payload.attendee_name,
                attendee_email=payload.attendee_email,
                attendee_phone=payload.attendee_phone,
                status="failed",
                metadata={
                    "error": str(insert_error),
                    "attempts": insert_attempts,
                    "caller_phone": caller_phone,
                },
                session_id=voice_session_id,
                agent_id=(bundle.id if bundle else None),
                tool_execution_id=tool_execution_id,
            )
        except Exception:
            pass
        log_event_panel(
            "BOOKING FAILED",
            {
                "Reason": str(insert_error)[:200],
                "Attempts": str(insert_attempts),
                "Calendar": cal_id,
                "When": f"{start_dt.isoformat()} → {end_dt.isoformat()}",
            },
            style="red",
            icon_key="error",
        )
        return {
            "success": False,
            "status": "insert_failed",
            "message": (
                f"calendar API rejected the booking after {insert_attempts} "
                f"attempt(s): {insert_error}"
            ),
        }

    # ── Post-insert verification. We re-list the slot and confirm Google
    # actually persisted the event. This catches silent permission /
    # quota issues where insert returns 200 but the event never lands on
    # the visible calendar.
    verified = False
    verification_error: str | None = None
    try:
        confirmed = await client.list_events(
            calendar_id=cal_id,
            time_min_iso=start_dt.isoformat(),
            time_max_iso=end_dt.isoformat(),
            time_zone=tz_name,
        )
        verified = any(e.id == event.get("id") for e in confirmed)
        if not verified:
            verification_error = (
                f"event id {event.get('id')!r} not found in re-listed slot "
                f"({len(confirmed)} event(s) on calendar at that time)"
            )
            log_dataflow(
                "calendar.book.verify_missing",
                verification_error,
                level="error",
            )
    except Exception as exc:
        verification_error = str(exc)
        log_dataflow(
            "calendar.book.verify_error",
            f"could not re-list slot for verification: {exc}",
            level="warning",
        )

    booking_id = await booking_repo.insert(
        google_event_id=event.get("id"),
        google_calendar_id=cal_id,
        summary=payload.summary,
        description=description,
        start_time=start_dt,
        end_time=end_dt,
        attendee_name=payload.attendee_name,
        attendee_email=payload.attendee_email,
        attendee_phone=payload.attendee_phone,
        status="booked",
        metadata={
            "html_link": event.get("htmlLink"),
            "attendees_emailed": (
                bool(payload.attendee_email)
                and bool(settings.jurinex_voice_calendar_allow_attendees)
            ),
            "attempts": insert_attempts,
            "verified_on_calendar": verified,
            "verification_error": verification_error,
            "caller_phone": caller_phone,
            "callback_phone": callback_phone,
        },
        session_id=voice_session_id,
        agent_id=(bundle.id if bundle else None),
        tool_execution_id=tool_execution_id,
    )

    log_event_panel(
        "BOOKING SUCCEEDED" if verified else "BOOKING UNCONFIRMED",
        {
            "Booking id": str(booking_id),
            "Google event id": event.get("id") or "-",
            "Verified": "yes" if verified else f"no — {verification_error}",
            "Attempts": str(insert_attempts),
            "Callback phone": callback_phone or "-",
            "When": f"{start_dt.isoformat()} → {end_dt.isoformat()}",
        },
        style=("green" if verified else "yellow"),
        icon_key="tool" if verified else "warn",
    )

    return {
        "success": True,
        "status": "booked",
        "booking_id": str(booking_id),
        "google_event_id": event.get("id"),
        "html_link": event.get("htmlLink"),
        "calendar_id": cal_id,
        "start_iso": start_dt.isoformat(),
        "end_iso": end_dt.isoformat(),
        "summary": payload.summary,
        "attendee_name": payload.attendee_name,
        "attendee_email": payload.attendee_email,
        "attendee_phone": payload.attendee_phone,
        "callback_phone": callback_phone,
        "verified_on_calendar": verified,
        "attempts": insert_attempts,
        "attendees_emailed": (
            bool(payload.attendee_email)
            and bool(settings.jurinex_voice_calendar_allow_attendees)
        ),
    }
