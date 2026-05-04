"""Load the active voice agent + its configuration + transfer config.

The admin panel writes three tables that together describe everything we
need to spin up a Live session:

  * ``voice_agents``                 — identity, status (active/inactive/draft).
  * ``voice_agent_configurations``   — Gemini Live model, voice, temperature,
                                       audio system prompt, tool_settings JSONB,
                                       custom_settings JSONB (the agent_builder
                                       blob with welcome/speech/call/security
                                       sections).
  * ``voice_agent_transfer_configs`` — static_destination, destination_prompt,
                                       transfer_type, ring_duration_seconds,
                                       handoff_message, etc.

This module pulls all three in a single round-trip and packages them as a
typed ``AgentBundle`` dataclass that the rest of the bridge can consume
without re-querying.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import log_dataflow


# ---------------------------------------------------------------------------
# Typed result objects
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AgentTransferConfig:
    """Mirrors a row of ``voice_agent_transfer_configs``."""

    name: str
    routing_mode: str               # 'static' | 'dynamic'
    static_destination: str | None  # E.164 phone number when routing_mode='static'
    destination_prompt: str | None  # natural-language routing rule when 'dynamic'
    e164_format: bool
    transfer_type: str              # 'warm' | 'cold'
    on_hold_music: str
    ring_duration_seconds: int
    navigate_ivr: bool
    internal_queue: bool
    agent_wait_seconds: int
    whisper_debrief: bool
    whisper_message: str | None
    three_way_ring_tone: bool
    three_way_debrief: bool
    handoff_mode: str
    handoff_message: str | None
    displayed_caller_id: str
    custom_settings: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentBundle:
    """Everything needed to start a live call for one voice agent."""

    # voice_agents columns
    id: UUID
    name: str
    display_name: str | None
    description: str | None
    status: str
    language_config: dict[str, Any] = field(default_factory=dict)

    # voice_agent_configurations columns
    text_model: str = ""
    live_model: str = ""
    voice_name: str = ""
    voice_tag: str = ""
    temperature: float = 0.0
    top_p: float = 0.0
    max_tokens: int = 0
    top_k_results: int = 0
    text_chat_system_prompt: str | None = None
    audio_live_system_prompt: str | None = None
    custom_settings: dict[str, Any] = field(default_factory=dict)
    tool_settings: dict[str, Any] = field(default_factory=dict)

    # voice_agent_transfer_configs (optional — agents may not have one)
    transfer: AgentTransferConfig | None = None

    # ── Convenience accessors over custom_settings.agent_builder ──

    @property
    def agent_builder(self) -> dict[str, Any]:
        return self.custom_settings.get("agent_builder") or {}

    @property
    def welcome_settings(self) -> dict[str, Any]:
        return self.agent_builder.get("welcome") or {}

    @property
    def call_settings(self) -> dict[str, Any]:
        return self.agent_builder.get("call") or {}

    @property
    def speech_settings(self) -> dict[str, Any]:
        return self.agent_builder.get("speech") or {}

    @property
    def security_settings(self) -> dict[str, Any]:
        return self.agent_builder.get("security") or {}

    @property
    def calendar_settings(self) -> dict[str, Any]:
        return (self.agent_builder.get("tool_settings") or {}).get("calendar") or {}

    @property
    def knowledge_base_settings(self) -> dict[str, Any]:
        return self.agent_builder.get("knowledge_base") or {}

    @property
    def enabled_function_keys(self) -> list[str]:
        return [
            f.get("key")
            for f in (self.agent_builder.get("functions") or [])
            if f.get("enabled") and f.get("key")
        ]

    @property
    def languages(self) -> list[str]:
        return self.agent_builder.get("languages") or self.language_config.get(
            "languages", []
        )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class VoiceAgentRepository:
    """Reads the admin's voice-agent tables.

    All queries are read-only. The admin panel owns writes; we never UPDATE
    or DELETE anything in these tables.
    """

    _SELECT_COLUMNS = """
            a.id                            AS agent_id,
            a.name                          AS name,
            a.display_name                  AS display_name,
            a.description                   AS description,
            a.status                        AS status,
            a.language_config               AS language_config,
            c.text_model                    AS text_model,
            c.live_model                    AS live_model,
            c.voice_name                    AS voice_name,
            c.voice_tag                     AS voice_tag,
            c.temperature                   AS temperature,
            c.top_p                         AS top_p,
            c.max_tokens                    AS max_tokens,
            c.top_k_results                 AS top_k_results,
            c.text_chat_system_prompt       AS text_chat_system_prompt,
            c.audio_live_system_prompt      AS audio_live_system_prompt,
            c.custom_settings               AS custom_settings,
            c.tool_settings                 AS tool_settings,
            t.name                          AS t_name,
            t.routing_mode                  AS t_routing_mode,
            t.static_destination            AS t_static_destination,
            t.destination_prompt            AS t_destination_prompt,
            t.e164_format                   AS t_e164_format,
            t.transfer_type                 AS t_transfer_type,
            t.on_hold_music                 AS t_on_hold_music,
            t.ring_duration_seconds         AS t_ring_duration_seconds,
            t.navigate_ivr                  AS t_navigate_ivr,
            t.internal_queue                AS t_internal_queue,
            t.agent_wait_seconds            AS t_agent_wait_seconds,
            t.whisper_debrief               AS t_whisper_debrief,
            t.whisper_message               AS t_whisper_message,
            t.three_way_ring_tone           AS t_three_way_ring_tone,
            t.three_way_debrief             AS t_three_way_debrief,
            t.handoff_mode                  AS t_handoff_mode,
            t.handoff_message               AS t_handoff_message,
            t.displayed_caller_id           AS t_displayed_caller_id,
            t.custom_settings               AS t_custom_settings
    """

    # Single SELECT that pulls agent + config + transfer in one trip. LEFT
    # JOIN on transfer because not every agent has a transfer config row.
    _LOAD_SQL = text(
        f"""
        SELECT {_SELECT_COLUMNS}
        FROM voice_agents               a
        JOIN voice_agent_configurations c ON c.agent_id = a.id
        LEFT JOIN voice_agent_transfer_configs t ON t.agent_id = a.id
        WHERE a.name = :name
        LIMIT 1
        """
    )

    _LOAD_BY_ID_SQL = text(
        f"""
        SELECT {_SELECT_COLUMNS}
        FROM voice_agents               a
        JOIN voice_agent_configurations c ON c.agent_id = a.id
        LEFT JOIN voice_agent_transfer_configs t ON t.agent_id = a.id
        WHERE a.id = :id
        LIMIT 1
        """
    )

    _LIST_ACTIVE_SQL = text(
        """
        SELECT id, name, display_name, description
        FROM voice_agents
        WHERE status = 'active'
        ORDER BY name
        """
    )

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def load_active_bundle(self, name: str) -> AgentBundle | None:
        """Return the bundle for ``name`` if it exists and is ``active``.

        Returns ``None`` for missing rows OR inactive agents — both produce
        the same caller-side outcome (refuse to take the call), so the
        bridge can treat them uniformly.
        """
        log_dataflow("agent.bundle.lookup", f"name={name}", level="debug")

        result = await self.session.execute(self._LOAD_SQL, {"name": name})
        row = result.mappings().first()
        if row is None:
            log_dataflow(
                "agent.bundle.not_found",
                f"no row in voice_agents for name={name!r}",
                level="warning",
            )
            return None

        if row["status"] != "active":
            log_dataflow(
                "agent.bundle.inactive",
                f"agent {name} status={row['status']!r} — refusing to load",
                level="warning",
            )
            return None

        bundle = self._row_to_bundle(row)
        log_dataflow(
            "agent.bundle.loaded",
            f"name={bundle.name} live_model={bundle.live_model} "
            f"voice={bundle.voice_name} prompt_len="
            f"{len(bundle.audio_live_system_prompt or '')} "
            f"transfer={'yes' if bundle.transfer else 'no'} "
            f"functions={','.join(bundle.enabled_function_keys) or '-'}",
        )
        return bundle

    async def load_active_bundle_by_id(self, agent_id: str) -> AgentBundle | None:
        """Same as ``load_active_bundle`` but keyed on ``voice_agents.id``.

        Used by the agent_transfer tool when the model passes a UUID instead
        of a name.
        """
        log_dataflow("agent.bundle.lookup", f"id={agent_id}", level="debug")
        try:
            result = await self.session.execute(
                self._LOAD_BY_ID_SQL, {"id": agent_id}
            )
        except Exception as exc:
            log_dataflow(
                "agent.bundle.lookup_error",
                f"invalid agent id {agent_id!r}: {exc}",
                level="warning",
            )
            return None
        row = result.mappings().first()
        if row is None:
            log_dataflow(
                "agent.bundle.not_found",
                f"no row in voice_agents for id={agent_id!r}",
                level="warning",
            )
            return None
        if row["status"] != "active":
            log_dataflow(
                "agent.bundle.inactive",
                f"agent id={agent_id} status={row['status']!r}",
                level="warning",
            )
            return None
        bundle = self._row_to_bundle(row)
        log_dataflow(
            "agent.bundle.loaded",
            f"id={bundle.id} name={bundle.name} live_model={bundle.live_model}",
        )
        return bundle

    async def list_active_agents(self) -> list[dict[str, Any]]:
        """Return ``[{id, name, display_name, description}]`` for ALL active agents.

        Used by agent_transfer to validate the requested target name and
        produce a helpful error listing the choices.
        """
        result = await self.session.execute(self._LIST_ACTIVE_SQL)
        return [dict(row) for row in result.mappings()]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_bundle(row: Any) -> AgentBundle:
        transfer: AgentTransferConfig | None = None
        if row["t_name"] is not None:
            transfer = AgentTransferConfig(
                name=row["t_name"],
                routing_mode=row["t_routing_mode"],
                static_destination=row["t_static_destination"],
                destination_prompt=row["t_destination_prompt"],
                e164_format=bool(row["t_e164_format"]),
                transfer_type=row["t_transfer_type"],
                on_hold_music=row["t_on_hold_music"],
                ring_duration_seconds=int(row["t_ring_duration_seconds"]),
                navigate_ivr=bool(row["t_navigate_ivr"]),
                internal_queue=bool(row["t_internal_queue"]),
                agent_wait_seconds=int(row["t_agent_wait_seconds"]),
                whisper_debrief=bool(row["t_whisper_debrief"]),
                whisper_message=row["t_whisper_message"],
                three_way_ring_tone=bool(row["t_three_way_ring_tone"]),
                three_way_debrief=bool(row["t_three_way_debrief"]),
                handoff_mode=row["t_handoff_mode"],
                handoff_message=row["t_handoff_message"],
                displayed_caller_id=row["t_displayed_caller_id"],
                custom_settings=dict(row["t_custom_settings"] or {}),
            )

        return AgentBundle(
            id=row["agent_id"],
            name=row["name"],
            display_name=row["display_name"],
            description=row["description"],
            status=row["status"],
            language_config=dict(row["language_config"] or {}),
            text_model=row["text_model"] or "",
            live_model=row["live_model"] or "",
            voice_name=row["voice_name"] or "",
            voice_tag=row["voice_tag"] or "",
            temperature=float(row["temperature"] or 0.0),
            top_p=float(row["top_p"] or 0.0),
            max_tokens=int(row["max_tokens"] or 0),
            top_k_results=int(row["top_k_results"] or 0),
            text_chat_system_prompt=row["text_chat_system_prompt"],
            audio_live_system_prompt=row["audio_live_system_prompt"],
            custom_settings=dict(row["custom_settings"] or {}),
            tool_settings=dict(row["tool_settings"] or {}),
            transfer=transfer,
        )
