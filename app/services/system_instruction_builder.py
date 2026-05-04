"""Assemble the live-session system instruction from admin-owned tables.

Recipe (matches admin spec §4):

  1. Persona prompt — ``voice_agent_configurations.audio_live_system_prompt``
  2. Blank line
  3. ``live_session_base`` fragment
  4. Blank line
  5. ``live_session_realtime_rules`` fragment
  6. Optional ``knowledge_base_header`` block — only when the KB content is
     intended to be inlined into the prompt itself (i.e. when
     ``search_knowledge_base`` is NOT enabled as a callable tool). When the
     tool *is* enabled, the model is expected to call it on demand and the
     header block is omitted.
  7. One block per enabled tool (sort_order ascending) from
     ``voice_tool_system_prompts``.

Mustache variables exposed to every fragment + tool template:

    language_label              "English (multilingual: en, hi, mr)"
    timezone                    "Asia/Kolkata"
    default_meeting_minutes     30
    working_hours_block         multi-line "  monday: 10:00-17:00\n  ..."
    disabled_days               "Sunday"
    blocked_dates               "2026-04-04, 2026-04-11, ..."
    view_only_warning           "" or "(read-only mode — no real bookings)"
    transfer_destination        either static_destination, the destination_prompt
                                summary, or "" if no transfer config
    transfer_type               "warm" / "cold"
    welcome_message             rendered welcome line for AI-first turn 1
    kb_sections_block / kb_truncated_note — only relevant when KB header is on
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.db.prompt_fragments_repository import (
    PromptFragmentsRepository,
    render_mustache,
)
from app.db.voice_agent_repository import AgentBundle
from app.observability.logger import log_dataflow


_DAY_LABEL = {
    "monday": "Monday",
    "tuesday": "Tuesday",
    "wednesday": "Wednesday",
    "thursday": "Thursday",
    "friday": "Friday",
    "saturday": "Saturday",
    "sunday": "Sunday",
}
_DAY_ORDER = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


@dataclass(slots=True)
class AssembledInstruction:
    """The compiled system instruction + the variables that produced it."""

    text: str
    variables: dict[str, Any] = field(default_factory=dict)
    enabled_tools: list[str] = field(default_factory=list)
    welcome_text: str = ""


# ---------------------------------------------------------------------------
# Variable helpers
# ---------------------------------------------------------------------------


def _format_language_label(bundle: AgentBundle) -> str:
    languages = bundle.languages or ["en"]
    if len(languages) == 1:
        return {"en": "English", "hi": "Hindi", "mr": "Marathi"}.get(
            languages[0], languages[0]
        )
    primary = languages[0]
    label = {"en": "English", "hi": "Hindi", "mr": "Marathi"}.get(primary, primary)
    return f"{label} (multilingual: {', '.join(languages)})"


def _format_working_hours(calendar: dict[str, Any]) -> tuple[str, str]:
    """Return ``(working_hours_block, disabled_days)``."""
    wh = calendar.get("working_hours") or {}
    enabled_lines: list[str] = []
    disabled: list[str] = []
    for day in _DAY_ORDER:
        cfg = wh.get(day) or {}
        label = _DAY_LABEL[day]
        if cfg.get("enabled"):
            start = cfg.get("start") or "00:00"
            end = cfg.get("end") or "00:00"
            enabled_lines.append(f"  {label}: {start}-{end}")
        else:
            disabled.append(label)
    block = "\n".join(enabled_lines) if enabled_lines else "  (none configured)"
    return block, ", ".join(disabled) if disabled else "(none)"


def _format_blocked_dates(calendar: dict[str, Any]) -> str:
    dates = calendar.get("blocked_dates") or []
    return ", ".join(str(d) for d in dates) if dates else "(none)"


_E164_RE = __import__("re").compile(r"\+\d{6,15}")


def _format_transfer(bundle: AgentBundle) -> tuple[str, str]:
    """Return ``(transfer_destination_block, transfer_type)``.

    Static mode → just the E.164 number.
    Dynamic mode → a plain-language block listing the intent → number rules
    from ``destination_prompt`` so the model can quote the right number when
    it calls the tool with ``destination_phone``. We deliberately keep the
    admin's wording verbatim and append a normalised numbers list so the
    model never has to guess formatting.
    """
    if bundle.transfer is None:
        return "", ""
    t = bundle.transfer
    if t.routing_mode == "static" and t.static_destination:
        return t.static_destination, t.transfer_type
    if t.routing_mode == "dynamic" and t.destination_prompt:
        prompt = t.destination_prompt.strip()
        numbers = sorted(set(_E164_RE.findall(prompt)))
        if numbers:
            block = (
                "DYNAMIC ROUTING (pick ONE based on caller intent):\n"
                f"  Rules: {prompt}\n"
                f"  Allowed numbers (must match exactly): "
                f"{', '.join(numbers)}\n"
                "When you call the transfer tool, pass `destination_phone` "
                "set to one of the allowed numbers above."
            )
        else:
            block = prompt
        return block, t.transfer_type
    return "", t.transfer_type or ""


def _format_welcome(bundle: AgentBundle) -> str:
    """Pick the welcome line to use when ``welcome.speaker == 'ai_first'``.

    Priority:
      1. Explicit ``welcome.message`` set by admin.
      2. Empty string when ``mode == 'dynamic'`` — the model writes its own
         opening line based on the persona prompt's Turn-1 instructions.
      3. Empty string otherwise.
    """
    welcome = bundle.welcome_settings
    msg = welcome.get("message")
    if msg:
        return str(msg)
    return ""


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


class SystemInstructionBuilder:
    """Stateless helper that combines a bundle + fragments into one prompt."""

    def __init__(self, fragments_repo: PromptFragmentsRepository) -> None:
        self.fragments_repo = fragments_repo

    async def build(
        self,
        bundle: AgentBundle,
        *,
        kb_sections_block: str = "",
        kb_truncated: bool = False,
    ) -> AssembledInstruction:
        fragments = await self.fragments_repo.get_fragments()
        tools = await self.fragments_repo.get_tool_prompts()

        enabled_keys = set(bundle.enabled_function_keys)
        # Auto-enable search_knowledge_base whenever the agent has KB docs
        # selected — admin docs treat this as implicit, the dashboard doesn't
        # surface it as a toggle.
        kb_docs = bundle.knowledge_base_settings.get("document_ids") or []
        if kb_docs:
            enabled_keys.add("search_knowledge_base")

        # ── Variable bag ──
        transfer_destination, transfer_type = _format_transfer(bundle)
        working_hours_block, disabled_days = _format_working_hours(
            bundle.calendar_settings
        )
        view_only = bool(bundle.calendar_settings.get("view_only"))
        view_only_warning = (
            "Calendar is in VIEW-ONLY mode — DO NOT call calendar_book; "
            "only describe availability if asked."
            if view_only
            else ""
        )

        welcome_text = _format_welcome(bundle)

        variables: dict[str, Any] = {
            "language_label": _format_language_label(bundle),
            "timezone": bundle.calendar_settings.get("timezone")
            or "Asia/Kolkata",
            "default_meeting_minutes": bundle.calendar_settings.get(
                "default_meeting_minutes"
            )
            or 30,
            "working_hours_block": working_hours_block,
            "disabled_days": disabled_days,
            "blocked_dates": _format_blocked_dates(bundle.calendar_settings),
            "view_only_warning": view_only_warning,
            "transfer_destination": transfer_destination,
            "transfer_type": transfer_type,
            "welcome_message": welcome_text,
            "kb_sections_block": kb_sections_block,
            "kb_truncated_note": (
                fragments["knowledge_base_truncated_note"].template
                if kb_truncated and "knowledge_base_truncated_note" in fragments
                else ""
            ),
        }

        # ── Assemble in spec order ──
        sections: list[str] = []

        # 1. persona
        persona = (bundle.audio_live_system_prompt or "").strip()
        if persona:
            sections.append(persona)

        # 3 + 5. base + realtime rules (always, when active)
        for key in ("live_session_base", "live_session_realtime_rules"):
            frag = fragments.get(key)
            if frag is not None:
                sections.append(render_mustache(frag.template, variables))

        # 6. KB header — only when the tool is NOT one of the enabled tools
        # (i.e. KB content gets inlined into the prompt instead of being
        # fetched on demand). For Preeti, search_knowledge_base IS enabled,
        # so this block is skipped.
        kb_header = fragments.get("knowledge_base_header")
        if (
            kb_header is not None
            and "search_knowledge_base" not in enabled_keys
            and kb_sections_block
        ):
            sections.append(render_mustache(kb_header.template, variables))

        # 7. tool blocks, sorted by their sort_order
        enabled_tool_objs = sorted(
            (tools[k] for k in tools if k in enabled_keys),
            key=lambda t: t.sort_order,
        )
        for tp in enabled_tool_objs:
            sections.append(render_mustache(tp.prompt_template, variables))

        # 8. fallback phrase always tail-appended (no-source-of-truth fallback)
        fp = fragments.get("fallback_phrase")
        if fp is not None:
            sections.append(render_mustache(fp.template, variables))

        compiled = "\n\n".join(s for s in sections if s.strip())
        log_dataflow(
            "prompt.assembled",
            f"agent={bundle.name} sections={len(sections)} "
            f"tools={[t.tool_name for t in enabled_tool_objs]} "
            f"chars={len(compiled)} kb_header={'yes' if (kb_header is not None and 'search_knowledge_base' not in enabled_keys) else 'no'}",
        )

        # Resolve welcome turn template (rendered) so the bridge can prime
        # Gemini with it on AI-first calls.
        rendered_welcome = ""
        wt = fragments.get("welcome_turn_template")
        if wt is not None and welcome_text:
            rendered_welcome = render_mustache(wt.template, variables)

        return AssembledInstruction(
            text=compiled,
            variables=variables,
            enabled_tools=[t.tool_name for t in enabled_tool_objs],
            welcome_text=rendered_welcome,
        )
