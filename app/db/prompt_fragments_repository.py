"""Read-only access to admin-owned prompt template tables.

Two tables back the system instruction we send to Gemini Live:

  * ``voice_system_prompt_fragments`` — keyed by ``fragment_key`` (e.g.
    ``live_session_base``, ``knowledge_base_header``, ``welcome_turn_template``).
    Column name: **``template``** (not ``prompt_template`` despite the spec).

  * ``voice_tool_system_prompts``     — keyed by ``tool_name`` (e.g.
    ``search_knowledge_base``, ``transfer_call``, ``calendar_check``).
    Column name: ``prompt_template``.

Both are pulled once per agent boot, cached in-process for 60 s (configurable
via ``JURINEX_VOICE_PROMPT_FRAGMENT_CACHE_MS`` and
``JURINEX_VOICE_TOOL_PROMPT_CACHE_MS``), then re-fetched. This lets the admin
edit a fragment in the dashboard and have new calls pick it up within a
minute, without a redeploy.

Mustache rendering is deliberately tiny — ``{{var}}`` and ``{{ var }}`` only,
no conditionals, no loops. Unknown placeholders are left in place so a
half-rendered template surfaces clearly in logs instead of being silently
dropped.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.observability.logger import log_dataflow


# ---------------------------------------------------------------------------
# Typed result objects
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PromptFragment:
    fragment_key: str
    display_name: str
    template: str
    is_active: bool
    sort_order: int


@dataclass(slots=True)
class ToolPrompt:
    tool_name: str
    display_name: str
    prompt_template: str
    is_active: bool
    sort_order: int


@dataclass(slots=True)
class _CachedFragments:
    fetched_at_ms: float
    by_key: dict[str, PromptFragment] = field(default_factory=dict)


@dataclass(slots=True)
class _CachedTools:
    fetched_at_ms: float
    by_name: dict[str, ToolPrompt] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Process-local caches (TTL)
# ---------------------------------------------------------------------------


_FRAGMENTS_CACHE: _CachedFragments | None = None
_TOOLS_CACHE: _CachedTools | None = None


def _now_ms() -> float:
    return time.monotonic() * 1000.0


# ---------------------------------------------------------------------------
# Mustache rendering
# ---------------------------------------------------------------------------


_MUSTACHE_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def render_mustache(template: str, variables: dict[str, Any]) -> str:
    """Replace ``{{var}}`` / ``{{ var }}`` with ``variables[var]``.

    Unknown keys are left untouched so a misconfigured template surfaces in
    the rendered system prompt (and in logs) rather than disappearing
    silently.
    """
    if not template:
        return template

    def _sub(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key in variables:
            value = variables[key]
            return "" if value is None else str(value)
        return match.group(0)

    return _MUSTACHE_RE.sub(_sub, template)


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class PromptFragmentsRepository:
    """Read-only loader for fragments + tool prompts with a TTL cache."""

    _LOAD_FRAGMENTS = text(
        """
        SELECT fragment_key, display_name, template, is_active, sort_order
        FROM voice_system_prompt_fragments
        WHERE is_active = TRUE
        ORDER BY sort_order ASC
        """
    )

    _LOAD_TOOLS = text(
        """
        SELECT tool_name, display_name, prompt_template, is_active, sort_order
        FROM voice_tool_system_prompts
        WHERE is_active = TRUE
        ORDER BY sort_order ASC
        """
    )

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_fragments(self) -> dict[str, PromptFragment]:
        """Return active fragments keyed by ``fragment_key`` (cached)."""
        global _FRAGMENTS_CACHE

        ttl_ms = settings.jurinex_voice_prompt_fragment_cache_ms
        if (
            _FRAGMENTS_CACHE is not None
            and ttl_ms > 0
            and _now_ms() - _FRAGMENTS_CACHE.fetched_at_ms < ttl_ms
        ):
            log_dataflow(
                "prompts.cache.hit",
                f"fragments age_ms={_now_ms() - _FRAGMENTS_CACHE.fetched_at_ms:.0f}",
                level="debug",
            )
            return _FRAGMENTS_CACHE.by_key

        log_dataflow("prompts.cache.miss", "fragments — refreshing from DB")
        result = await self.session.execute(self._LOAD_FRAGMENTS)
        rows = list(result.mappings())
        by_key: dict[str, PromptFragment] = {
            row["fragment_key"]: PromptFragment(
                fragment_key=row["fragment_key"],
                display_name=row["display_name"],
                template=row["template"],
                is_active=bool(row["is_active"]),
                sort_order=int(row["sort_order"]),
            )
            for row in rows
        }
        _FRAGMENTS_CACHE = _CachedFragments(fetched_at_ms=_now_ms(), by_key=by_key)
        log_dataflow(
            "prompts.fragment.loaded",
            f"count={len(by_key)} keys={sorted(by_key)}",
        )
        return by_key

    async def get_tool_prompts(self) -> dict[str, ToolPrompt]:
        """Return active tool prompts keyed by ``tool_name`` (cached)."""
        global _TOOLS_CACHE

        ttl_ms = settings.jurinex_voice_tool_prompt_cache_ms
        if (
            _TOOLS_CACHE is not None
            and ttl_ms > 0
            and _now_ms() - _TOOLS_CACHE.fetched_at_ms < ttl_ms
        ):
            log_dataflow(
                "prompts.cache.hit",
                f"tool_prompts age_ms={_now_ms() - _TOOLS_CACHE.fetched_at_ms:.0f}",
                level="debug",
            )
            return _TOOLS_CACHE.by_name

        log_dataflow("prompts.cache.miss", "tool_prompts — refreshing from DB")
        result = await self.session.execute(self._LOAD_TOOLS)
        rows = list(result.mappings())
        by_name: dict[str, ToolPrompt] = {
            row["tool_name"]: ToolPrompt(
                tool_name=row["tool_name"],
                display_name=row["display_name"],
                prompt_template=row["prompt_template"],
                is_active=bool(row["is_active"]),
                sort_order=int(row["sort_order"]),
            )
            for row in rows
        }
        _TOOLS_CACHE = _CachedTools(fetched_at_ms=_now_ms(), by_name=by_name)
        log_dataflow(
            "prompts.tool.loaded",
            f"count={len(by_name)} names={sorted(by_name)}",
        )
        return by_name

    # ------------------------------------------------------------------
    # Cache controls (used by tests / admin reload endpoint)
    # ------------------------------------------------------------------

    @staticmethod
    def invalidate_cache() -> None:
        """Drop both caches so the next read hits the DB."""
        global _FRAGMENTS_CACHE, _TOOLS_CACHE
        _FRAGMENTS_CACHE = None
        _TOOLS_CACHE = None
        log_dataflow("prompts.cache.invalidated", "fragments + tool_prompts")
