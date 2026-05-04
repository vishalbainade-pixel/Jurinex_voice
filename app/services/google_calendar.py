"""Google Calendar v3 client (httpx + google-auth — no googleapiclient).

We avoid pulling in `googleapiclient` because the project already ships
`httpx` and `google-auth` for other tools (GCS, Storage). The flow is:

  1. Decode the SA JSON from ``JURINEX_VOICE_CALENDAR_SA_JSON_BASE64``.
  2. Mint an OAuth access token via ``google.oauth2.service_account``.
  3. Call the Calendar v3 REST API directly with ``httpx.AsyncClient``.

Two methods are exposed:

  * ``list_events(calendar_id, time_min, time_max, time_zone)`` — read-only
    lookup of busy ranges in the requested window.
  * ``insert_event(...)`` — create an event. Honours
    ``JURINEX_VOICE_CALENDAR_ALLOW_ATTENDEES``: when False, attendee
    notification emails are suppressed (Domain-Wide Delegation needed
    otherwise — Google rejects the create with HTTP 403).
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Any

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from app.config import settings
from app.observability.logger import log_dataflow


# Google Calendar v3 endpoints — we hit these directly so no SDK dependency.
_API_BASE = "https://www.googleapis.com/calendar/v3"
_SCOPES = ["https://www.googleapis.com/auth/calendar"]


# ---------------------------------------------------------------------------
# Credentials cache
# ---------------------------------------------------------------------------


_CREDENTIALS: service_account.Credentials | None = None


def _load_credentials() -> service_account.Credentials:
    """Decode the SA JSON from the base64-encoded env var (cached)."""
    global _CREDENTIALS
    if _CREDENTIALS is not None:
        return _CREDENTIALS
    raw = settings.jurinex_voice_calendar_sa_json_base64
    if not raw:
        raise RuntimeError(
            "JURINEX_VOICE_CALENDAR_SA_JSON_BASE64 is empty — "
            "calendar tools are disabled."
        )
    try:
        info = json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"could not decode calendar SA JSON: {exc}") from exc
    _CREDENTIALS = service_account.Credentials.from_service_account_info(
        info, scopes=_SCOPES
    )
    log_dataflow(
        "calendar.creds.loaded",
        f"client_email={info.get('client_email')}",
    )
    return _CREDENTIALS


def _refresh_token() -> str:
    """Mint or refresh an access token (sync — google-auth has no async API)."""
    creds = _load_credentials()
    if not creds.valid:
        creds.refresh(Request())
    return creds.token  # type: ignore[no-any-return]


async def _get_token() -> str:
    """Run the (sync) token mint in a thread so we don't block the event loop."""
    return await asyncio.to_thread(_refresh_token)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CalendarEvent:
    id: str
    summary: str | None
    start_iso: str
    end_iso: str
    html_link: str | None = None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GoogleCalendarClient:
    """Thin async wrapper around Calendar v3."""

    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        self._timeout = timeout_seconds

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = await _get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.request(
                method,
                _API_BASE + path,
                headers=headers,
                params=params,
                json=json_body,
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"calendar API {method} {path} → HTTP {resp.status_code}: "
                f"{resp.text[:300]}"
            )
        if not resp.content:
            return {}
        return resp.json()

    async def list_events(
        self,
        *,
        calendar_id: str,
        time_min_iso: str,
        time_max_iso: str,
        time_zone: str = "Asia/Kolkata",
    ) -> list[CalendarEvent]:
        params = {
            "singleEvents": "true",
            "orderBy": "startTime",
            "timeMin": time_min_iso,
            "timeMax": time_max_iso,
            "timeZone": time_zone,
            "maxResults": 250,
        }
        # URL-encode the calendar_id (group calendars contain '@' which must
        # be quoted in the path component).
        from urllib.parse import quote

        cal_q = quote(calendar_id, safe="")
        body = await self._request("GET", f"/calendars/{cal_q}/events", params=params)
        items = body.get("items") or []
        out: list[CalendarEvent] = []
        for it in items:
            # Skip cancelled or transparent (free-time-marked) events.
            if it.get("status") == "cancelled":
                continue
            if it.get("transparency") == "transparent":
                continue
            start = (it.get("start") or {}).get("dateTime") or (
                it.get("start") or {}
            ).get("date")
            end = (it.get("end") or {}).get("dateTime") or (
                it.get("end") or {}
            ).get("date")
            if not start or not end:
                continue
            out.append(
                CalendarEvent(
                    id=it.get("id") or "",
                    summary=it.get("summary"),
                    start_iso=start,
                    end_iso=end,
                    html_link=it.get("htmlLink"),
                )
            )
        return out

    async def insert_event(
        self,
        *,
        calendar_id: str,
        start_iso: str,
        end_iso: str,
        time_zone: str,
        summary: str,
        description: str | None = None,
        attendee_email: str | None = None,
        attendee_name: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "summary": summary,
            "start": {"dateTime": start_iso, "timeZone": time_zone},
            "end": {"dateTime": end_iso, "timeZone": time_zone},
        }
        if description:
            body["description"] = description
        params: dict[str, Any] = {}

        # Attendees — Google requires Domain-Wide Delegation (DWD) on the SA
        # to send invite emails. Without DWD, supplying attendees and asking
        # for sendUpdates=all returns HTTP 403. Gate with the env flag.
        if attendee_email and settings.jurinex_voice_calendar_allow_attendees:
            attendee: dict[str, Any] = {"email": attendee_email}
            if attendee_name:
                attendee["displayName"] = attendee_name
            body["attendees"] = [attendee]
            params["sendUpdates"] = "all"
        else:
            params["sendUpdates"] = "none"

        from urllib.parse import quote

        cal_q = quote(calendar_id, safe="")
        result = await self._request(
            "POST", f"/calendars/{cal_q}/events", params=params, json_body=body
        )
        log_dataflow(
            "calendar.event.created",
            f"id={result.get('id')} summary={summary!r} "
            f"start={start_iso} end={end_iso} attendees="
            f"{'yes' if 'attendees' in body else 'no'}",
        )
        return result
