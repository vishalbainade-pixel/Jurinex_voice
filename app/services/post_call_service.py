"""Post-call extraction job — runs after teardown to populate the admin tables.

  1. Pull the conversation transcript from ``call_messages`` for the call.
  2. Look at the agent bundle's
     ``agent_builder.post_call_extraction`` (which fields to extract) and
     ``agent_builder.post_call_model`` (which Gemini text model to use).
  3. Ask the model for a single JSON object whose keys match the admin's
     ``key`` field, and whose values respect the declared ``type``
     (text/string/boolean/enum/number).
  4. Persist:
       * one ``voice_post_call_extractions`` row (started_at → completed_at,
         status running → completed/failed),
       * one upserted ``voice_call_enrichments`` row (rolled-up summary,
         language, success flag, recording URI, transfer flag).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import CallMessage
from app.db.voice_agent_repository import AgentBundle
from app.db.voice_post_call_repository import (
    VoiceCallEnrichmentsRepository,
    VoicePostCallExtractionsRepository,
)
from app.observability.logger import log_dataflow, log_error, log_event_panel


_DEFAULT_FIELDS: list[dict[str, Any]] = [
    {"key": "call_summary", "type": "text", "label": "Call Summary", "enabled": True},
    {"key": "call_successful", "type": "boolean", "label": "Call Successful", "enabled": True},
    {"key": "user_sentiment", "type": "enum", "label": "User Sentiment", "enabled": True},
    {"key": "preferred_language", "type": "string", "label": "preferred_language", "enabled": True},
]


# ---------------------------------------------------------------------------
# Transcript assembly
# ---------------------------------------------------------------------------


async def _load_transcript(session: AsyncSession, call_id: uuid.UUID) -> str:
    stmt = (
        select(CallMessage.speaker, CallMessage.text, CallMessage.created_at)
        .where(CallMessage.call_id == call_id)
        .order_by(CallMessage.created_at.asc())
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return ""
    lines: list[str] = []
    for speaker, text, _ in rows:
        speaker_str = (
            getattr(speaker, "value", None) or str(speaker)
        ).upper()
        lines.append(f"{speaker_str}: {text}".strip())
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gemini text-model call (non-streaming, JSON output)
# ---------------------------------------------------------------------------


_DEFAULT_POST_CALL_MODEL = "gemini-2.5-flash"


def _enabled_fields(bundle: AgentBundle | None) -> list[dict[str, Any]]:
    if bundle is None:
        return _DEFAULT_FIELDS
    raw = bundle.agent_builder.get("post_call_extraction") or _DEFAULT_FIELDS
    return [f for f in raw if f.get("enabled") and f.get("key")]


def _post_call_model(bundle: AgentBundle | None) -> str:
    if bundle is None:
        return _DEFAULT_POST_CALL_MODEL
    return str(
        bundle.agent_builder.get("post_call_model") or _DEFAULT_POST_CALL_MODEL
    )


def _extraction_prompt(fields: list[dict[str, Any]], transcript: str) -> str:
    """Build a deterministic JSON-only extraction prompt."""
    field_lines: list[str] = []
    for f in fields:
        f_type = f.get("type", "string")
        label = f.get("label") or f["key"]
        description = ""
        if f_type == "boolean":
            description = " — true if the caller's request appears resolved, else false"
        elif f_type == "enum" and f["key"] == "user_sentiment":
            description = " — one of 'positive', 'neutral', 'negative'"
        elif f["key"] == "preferred_language":
            description = " — one of 'English', 'Hindi', 'Marathi'"
        elif f_type == "text":
            description = " — 1-2 sentence summary"
        field_lines.append(f'  "{f["key"]}" ({f_type}): {label}{description}')
    fields_block = "\n".join(field_lines)
    return (
        "You are extracting structured insights from a phone-call transcript.\n"
        "Return ONE valid JSON object. Keys MUST be exactly:\n"
        f"{fields_block}\n"
        "\n"
        "Rules:\n"
        "- Output ONLY the JSON object. No markdown, no commentary, no code fences.\n"
        "- Use null for fields you cannot determine.\n"
        "- For booleans output true/false (lowercase, no quotes).\n"
        "- Keep summaries concise.\n"
        "\n"
        "Transcript:\n"
        f"{transcript or '(empty transcript)'}\n"
    )


def _coerce_json(raw: str) -> dict[str, Any]:
    """Strip code fences and parse a JSON object out of model output."""
    text_str = raw.strip()
    text_str = re.sub(r"^```(?:json)?", "", text_str).strip()
    text_str = re.sub(r"```$", "", text_str).strip()
    # Find the first { ... } block
    match = re.search(r"\{.*\}", text_str, flags=re.DOTALL)
    if not match:
        raise ValueError(f"no JSON object in model output: {text_str[:200]}")
    return json.loads(match.group(0))


def _normalise_extraction(
    fields: list[dict[str, Any]],
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Type-coerce + zero-fill missing keys so the dashboard always sees the schema."""
    out: dict[str, Any] = {}
    for f in fields:
        key = f["key"]
        f_type = f.get("type")
        value = raw.get(key)
        if f_type == "boolean":
            out[key] = bool(value) if value is not None else False
        elif f_type == "text" or f_type == "string":
            out[key] = "" if value is None else str(value)
        elif f_type == "enum":
            out[key] = "" if value is None else str(value)
        elif f_type == "number":
            try:
                out[key] = float(value) if value is not None else None
            except (TypeError, ValueError):
                out[key] = None
        else:
            out[key] = value
    return out


async def _run_gemini_extraction(
    *,
    model_id: str,
    fields: list[dict[str, Any]],
    transcript: str,
) -> dict[str, Any]:
    """Call the Gemini text model and return the parsed JSON dict."""
    if not settings.gemini_key:
        raise RuntimeError("GOOGLE_API_KEY/GEMINI_API_KEY missing")
    from google import genai

    client = genai.Client(api_key=settings.gemini_key)
    prompt = _extraction_prompt(fields, transcript)

    # google-genai is not async-native for generate_content; run in a thread.
    def _call_sync() -> str:
        response = client.models.generate_content(
            model=model_id,
            contents=prompt,
        )
        text_str = getattr(response, "text", None)
        if not text_str:
            raise RuntimeError("model returned empty text")
        return text_str

    raw_text = await asyncio.to_thread(_call_sync)
    return _coerce_json(raw_text)


# ---------------------------------------------------------------------------
# Public entry point — invoked from the bridge teardown
# ---------------------------------------------------------------------------


async def run_post_call_extraction(
    session: AsyncSession,
    *,
    call_id: uuid.UUID,
    bundle: AgentBundle | None,
    voice_session_id: uuid.UUID | None = None,
    recording_uris: dict[str, str | None] | None = None,
    terminate_reason: str | None = None,
    language: str | None = None,
    transfer_requested: bool = False,
    pricing_payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Run extraction + write enrichment row. Best-effort — never raises."""
    fields = _enabled_fields(bundle)
    if not fields:
        log_dataflow(
            "post_call.skipped",
            "no enabled extraction fields on this agent",
            level="debug",
        )
        return None

    model_id = _post_call_model(bundle)
    transcript = await _load_transcript(session, call_id)

    log_event_panel(
        "POST-CALL EXTRACTION",
        {
            "Call": str(call_id),
            "Model": model_id,
            "Fields": ", ".join(f["key"] for f in fields),
            "Transcript chars": str(len(transcript)),
        },
        style="cyan",
        icon_key="tool",
    )

    extraction_id = uuid.uuid4()
    extraction_repo = VoicePostCallExtractionsRepository(session)
    enrichment_repo = VoiceCallEnrichmentsRepository(session)

    await extraction_repo.insert_running(
        extraction_id=extraction_id,
        call_id=call_id,
        agent_id=(bundle.id if bundle else None),
        session_id=voice_session_id,
        extraction_fields=fields,
        transcript=transcript,
        extraction_model=model_id,
    )

    started = time.monotonic()
    extracted: dict[str, Any] = {}
    error_msg: str | None = None
    try:
        if not transcript.strip():
            log_dataflow(
                "post_call.empty_transcript",
                "no call_messages rows — skipping model call, writing zero-fill",
                level="warning",
            )
            extracted = _normalise_extraction(fields, {})
        else:
            raw = await _run_gemini_extraction(
                model_id=model_id, fields=fields, transcript=transcript
            )
            extracted = _normalise_extraction(fields, raw)
    except Exception as exc:
        error_msg = str(exc)
        log_error("POST-CALL EXTRACTION FAILED", error_msg)
        extracted = _normalise_extraction(fields, {})

    latency_ms = int((time.monotonic() - started) * 1000)

    if error_msg:
        await extraction_repo.mark_failed(
            extraction_id=extraction_id,
            latency_ms=latency_ms,
            error_message=error_msg,
        )
    else:
        await extraction_repo.mark_completed(
            extraction_id=extraction_id,
            extracted_data=extracted,
            latency_ms=latency_ms,
        )

    # Upsert enrichment row
    successful = (
        bool(extracted.get("call_successful"))
        if "call_successful" in extracted
        else None
    )
    pref_lang = extracted.get("preferred_language") or language
    summary = extracted.get("call_summary") or ""

    rec_url = (recording_uris or {}).get("mixed")
    rec_folder = (recording_uris or {}).get("folder")

    cost_usd = (
        pricing_payload.get("cost_usd") if pricing_payload else None
    )
    analysis_block: dict[str, Any] = dict(extracted)
    if pricing_payload:
        analysis_block["pricing"] = pricing_payload

    try:
        await enrichment_repo.upsert(
            call_id=call_id,
            agent_id=(bundle.id if bundle else None),
            agent_name=(bundle.name if bundle else None),
            channel_type="phone",
            session_outcome=("successful" if successful else "unresolved"),
            end_reason=terminate_reason,
            preferred_language=pref_lang,
            successful=successful,
            picked_up=True,
            transfer_requested=transfer_requested,
            recording_url=rec_url,
            recording_gcs_uri=rec_folder,
            cost_usd=cost_usd,
            custom_attributes={"summary": summary},
            analysis=analysis_block,
        )
    except Exception as exc:
        log_error("POST-CALL ENRICHMENT UPSERT FAILED", str(exc))

    log_dataflow(
        "post_call.done",
        f"call={call_id} latency_ms={latency_ms} "
        f"keys={sorted(extracted)} successful={successful}",
    )
    return extracted
