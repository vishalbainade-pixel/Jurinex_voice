"""Catalogue lookup for active Gemini Live voices.

Validates the agent bundle's ``voice_name`` against the admin-curated
``platform_voices`` table at session start. If the requested voice is not
in the active set, the bridge logs a loud warning and falls back to a
known-good voice — without this check, an unknown voice silently drops
the live session with WS 1008 a few seconds in.

Result is process-cached for 5 minutes (matches the prompt-fragment
cache TTL pattern). Admin updates land within that window.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import log_dataflow


_CACHE_TTL_MS = 5 * 60 * 1000


@dataclass(slots=True)
class PlatformVoice:
    voice_name: str
    provider: str
    default_live_model: str
    gender: str
    accent: str


_CACHE: dict[str, PlatformVoice] | None = None
_CACHE_AT_MS: float = 0.0


def _now_ms() -> float:
    return time.monotonic() * 1000.0


class PlatformVoicesRepository:
    _SQL = text(
        """
        SELECT voice_name, provider, default_live_model, gender, accent
        FROM platform_voices
        WHERE is_active = TRUE AND provider = 'gemini'
        """
    )

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def active_voices(self) -> dict[str, PlatformVoice]:
        global _CACHE, _CACHE_AT_MS
        if _CACHE is not None and _now_ms() - _CACHE_AT_MS < _CACHE_TTL_MS:
            log_dataflow(
                "platform_voices.cache.hit",
                f"age_ms={_now_ms() - _CACHE_AT_MS:.0f}",
                level="debug",
            )
            return _CACHE

        log_dataflow("platform_voices.cache.miss", "refreshing from DB")
        result = await self.session.execute(self._SQL)
        rows = list(result.mappings())
        cache: dict[str, PlatformVoice] = {
            r["voice_name"]: PlatformVoice(
                voice_name=r["voice_name"],
                provider=r["provider"],
                default_live_model=r["default_live_model"],
                gender=r["gender"],
                accent=r["accent"],
            )
            for r in rows
        }
        _CACHE = cache
        _CACHE_AT_MS = _now_ms()
        log_dataflow(
            "platform_voices.loaded",
            f"count={len(cache)} sample={list(cache)[:5]}",
        )
        return cache

    async def is_active(self, voice_name: str) -> bool:
        return (await self.active_voices()).get(voice_name) is not None
