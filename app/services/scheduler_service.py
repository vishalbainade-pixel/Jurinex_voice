"""Outbound call scheduler — polls ``voice_call_schedules`` and dials.

Lifecycle:

    on app startup
        ↓
    SchedulerService.start()      ── creates the asyncio task that runs
        ↓                              ``_poll_loop()`` forever
    every N seconds
        ↓
    _poll_loop()                  ── claims one row at a time (FOR UPDATE
        ↓                              SKIP LOCKED) up to ``max_inflight``
    _dispatch_row()               ── dials Twilio with status='in_progress';
        ↓                              the bridge takes over from there.
    on app shutdown
        ↓
    SchedulerService.stop()       ── cancels the loop task

Each stage emits dataflow logs and a Rich console table so the entire
flow is visible without opening psql:

    scheduler.tick                — poll heartbeat (debug)
    scheduler.claim               — row claimed, status='queued'
    scheduler.dial.dispatched     — Twilio accepted the call, sid known
    scheduler.dial.cancelled      — admin cancelled between claim + dial
    scheduler.dial.error          — Twilio REST rejected the dial
    scheduler.failed              — terminal failure persisted
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from app.config import settings
from app.db.database import session_scope
from app.db.voice_agent_repository import VoiceAgentRepository
from app.db.voice_call_schedules_repository import (
    ScheduledCall,
    VoiceCallSchedulesRepository,
)
from app.observability.logger import (
    log_dataflow,
    log_error,
    log_event_panel,
)


_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


def _normalize_e164(phone: str | None) -> str | None:
    """Best-effort phone normalisation.

    The admin app validates on insert, so 99% of rows already pass —
    this is purely a safety net for direct DB inserts.
    """
    if not phone:
        return None
    cleaned = re.sub(r"[^\d+]", "", phone.strip())
    if not cleaned:
        return None
    if not cleaned.startswith("+"):
        # Plain 10-digit number → prepend the configured default country.
        cc = settings.scheduler_default_country_code or "+91"
        cleaned = (
            cc + cleaned if cleaned[0] != "0" else cc + cleaned[1:]
        )
    return cleaned if _E164_RE.match(cleaned) else None


class SchedulerService:
    """Background poller that turns due rows into Twilio outbound calls."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event = asyncio.Event()
        self._inflight_dials: int = 0
        # Per-tick dial counter for the heartbeat panel.
        self._tick: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not settings.scheduler_enabled:
            log_dataflow(
                "scheduler.disabled",
                "SCHEDULER_ENABLED=false — outbound poller will not run",
                level="warning",
            )
            return
        if self.is_running():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._poll_loop(), name="scheduler-poll")
        log_event_panel(
            "SCHEDULER STARTED",
            {
                "Poll interval": f"{settings.scheduler_poll_seconds}s",
                "Max in-flight": str(settings.scheduler_max_inflight),
                "Default country": settings.scheduler_default_country_code,
            },
            style="cyan",
            icon_key="tool",
        )

    async def stop(self) -> None:
        if not self.is_running():
            return
        self._stop_event.set()
        assert self._task is not None
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None
        log_event_panel(
            "SCHEDULER STOPPED",
            {"Reason": "app shutdown / manual stop"},
            style="yellow",
            icon_key="warn",
        )

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                self._tick += 1
                claimed = 0
                try:
                    claimed = await self._tick_once()
                except Exception as exc:
                    log_error("SCHEDULER TICK ERROR", str(exc))
                # Heartbeat at debug so it doesn't spam INFO; bump to info on
                # any tick that actually dispatched a dial.
                heartbeat_level = "info" if claimed else "debug"
                log_dataflow(
                    "scheduler.tick",
                    f"tick={self._tick} claimed={claimed} "
                    f"inflight={self._inflight_dials}",
                    level=heartbeat_level,
                )
                # Wait for the next tick OR an explicit stop signal.
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=settings.scheduler_poll_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            log_dataflow("scheduler.cancelled", "poll loop cancelled", level="warning")
            raise

    async def _tick_once(self) -> int:
        """Claim up to ``max_inflight`` due rows and dispatch each.

        Returns the number of rows that were CLAIMED (not necessarily
        dialed — the dispatch can still fail, but the row is now ours).
        """
        slots = max(
            0, settings.scheduler_max_inflight - self._inflight_dials
        )
        claimed = 0
        for _ in range(slots):
            scheduled: ScheduledCall | None
            async with session_scope() as session:
                repo = VoiceCallSchedulesRepository(session)
                scheduled = await repo.claim_due_row()
            if scheduled is None:
                break
            claimed += 1
            # Dispatch each dial in its own task so a slow Twilio call
            # doesn't block the next claim.
            asyncio.create_task(
                self._dispatch_row(scheduled),
                name=f"scheduler-dial-{scheduled.id}",
            )
        return claimed

    # ------------------------------------------------------------------
    # Dispatch (one row → one Twilio call)
    # ------------------------------------------------------------------

    async def _dispatch_row(self, scheduled: ScheduledCall) -> None:
        self._inflight_dials += 1
        try:
            await self._dispatch_row_inner(scheduled)
        finally:
            self._inflight_dials -= 1

    async def _dispatch_row_inner(self, scheduled: ScheduledCall) -> None:
        # ── 1. Phone normalisation (defence in depth)
        phone = _normalize_e164(scheduled.recipient_phone)
        if not phone:
            async with session_scope() as session:
                await VoiceCallSchedulesRepository(session).mark_failed(
                    schedule_id=scheduled.id,
                    last_error=f"invalid recipient_phone={scheduled.recipient_phone!r}",
                )
            return

        # ── 2. Verify the agent is still active
        async with session_scope() as session:
            agent_repo = VoiceAgentRepository(session)
            bundle = await agent_repo.load_active_bundle_by_id(
                str(scheduled.agent_id)
            )
        if bundle is None:
            async with session_scope() as session:
                await VoiceCallSchedulesRepository(session).mark_failed(
                    schedule_id=scheduled.id,
                    last_error=(
                        f"voice agent {scheduled.agent_id} is not active or "
                        "missing — refusing to dial"
                    ),
                )
            return

        log_event_panel(
            "SCHEDULER DIAL",
            {
                "Schedule id": str(scheduled.id),
                "Agent": f"{bundle.name} ({bundle.id})",
                "To": phone,
                "Name": scheduled.recipient_name or "-",
                "Email": scheduled.recipient_email or "-",
                "Source": scheduled.source,
                "Attempt": f"{scheduled.attempts + 1} of {scheduled.max_attempts}",
                "Notes": (scheduled.notes or "")[:80] or "-",
            },
            style="magenta",
            icon_key="call_start",
        )

        # ── 3. Place the Twilio call (carries schedule_id + agent_name as
        # custom-params so the bridge can mark the row complete + pick the
        # right agent).
        from app.services.call_service import CallService

        twilio_sid: str | None = None
        try:
            async with session_scope() as session:
                twilio_sid = await CallService(session).place_outbound_for_schedule(
                    schedule_id=scheduled.id,
                    agent_name=bundle.name,
                    to_phone=phone,
                    customer_name=scheduled.recipient_name,
                )
        except Exception as exc:
            log_dataflow(
                "scheduler.dial.error",
                f"id={scheduled.id} twilio dial raised: {exc}",
                level="error",
            )
            async with session_scope() as session:
                await VoiceCallSchedulesRepository(session).mark_failed(
                    schedule_id=scheduled.id,
                    last_error=f"twilio dial failed: {exc}",
                )
            return

        if not twilio_sid:
            async with session_scope() as session:
                await VoiceCallSchedulesRepository(session).mark_failed(
                    schedule_id=scheduled.id,
                    last_error="twilio returned no call SID",
                )
            return

        # ── 4. Flip queued → in_progress, gated on status='queued' so admin
        # cancellations between claim and dial win the race.
        async with session_scope() as session:
            ok = await VoiceCallSchedulesRepository(session).mark_dialing(
                schedule_id=scheduled.id,
                twilio_call_sid=twilio_sid,
            )
        if not ok:
            log_dataflow(
                "scheduler.dial.cancelled_post_dial",
                f"id={scheduled.id} sid={twilio_sid} — admin cancelled after "
                f"dial fired; Twilio call will continue but row stays cancelled",
                level="warning",
            )


# ---------------------------------------------------------------------------
# Singleton (one poller per process)
# ---------------------------------------------------------------------------


scheduler: SchedulerService = SchedulerService()
