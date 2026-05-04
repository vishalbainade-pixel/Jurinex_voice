"""Writers for the two admin-owned post-call tables.

  * ``voice_post_call_extractions`` — the structured-output record of one
    extraction run (transcript + extraction_fields → extracted_data).
    Lifecycle: insert with status='running' → update to
    'completed'/'failed' when the model returns.
  * ``voice_call_enrichments``      — one row per call_id with rolled-up
    metrics the admin dashboard renders (summary, sentiment, language,
    recording uri, transfer flag, cost). Upsert keyed on call_id.

Both schemas were verified live:

    voice_post_call_extractions(id, session_id, call_id, agent_id, status,
        extraction_fields(jsonb), extracted_data(jsonb), transcript,
        extraction_model, error_message, latency_ms,
        started_at, completed_at, created_at)

    voice_call_enrichments(call_id PK, agent_id, agent_name, agent_version,
        channel_type, session_outcome, end_reason, end_to_end_latency_ms,
        average_latency_ms, llm_token_count, cost_usd, preferred_language,
        successful, picked_up, transfer_requested, voicemail,
        recording_url, recording_gcs_uri, custom_attributes(jsonb),
        analysis(jsonb), created_at, updated_at)
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import log_dataflow


# ---------------------------------------------------------------------------
# voice_post_call_extractions
# ---------------------------------------------------------------------------


class VoicePostCallExtractionsRepository:
    _INSERT_SQL = text(
        """
        INSERT INTO voice_post_call_extractions (
            id, session_id, call_id, agent_id,
            status, extraction_fields, extracted_data,
            transcript, extraction_model,
            started_at, created_at
        )
        VALUES (
            :id, :session_id, :call_id, :agent_id,
            :status, CAST(:extraction_fields AS jsonb),
            CAST(:extracted_data AS jsonb),
            :transcript, :extraction_model,
            NOW(), NOW()
        )
        """
    )

    _UPDATE_SQL = text(
        """
        UPDATE voice_post_call_extractions
        SET status         = :status,
            extracted_data = CAST(:extracted_data AS jsonb),
            error_message  = :error_message,
            latency_ms     = :latency_ms,
            completed_at   = NOW()
        WHERE id = :id
        """
    )

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def insert_running(
        self,
        *,
        extraction_id: uuid.UUID,
        call_id: uuid.UUID | None,
        agent_id: uuid.UUID | None,
        session_id: uuid.UUID | None,
        extraction_fields: list[dict[str, Any]],
        transcript: str,
        extraction_model: str,
    ) -> None:
        await self.session.execute(
            self._INSERT_SQL,
            {
                "id": extraction_id,
                "session_id": session_id,
                "call_id": call_id,
                "agent_id": agent_id,
                "status": "running",
                "extraction_fields": json.dumps(extraction_fields, default=str),
                "extracted_data": json.dumps({}),
                "transcript": transcript,
                "extraction_model": extraction_model,
            },
        )
        log_dataflow(
            "post_call.extraction.started",
            f"id={extraction_id} model={extraction_model} "
            f"fields={[f.get('key') for f in extraction_fields]} "
            f"transcript_chars={len(transcript)}",
        )

    async def mark_completed(
        self,
        *,
        extraction_id: uuid.UUID,
        extracted_data: dict[str, Any],
        latency_ms: int,
    ) -> None:
        await self.session.execute(
            self._UPDATE_SQL,
            {
                "id": extraction_id,
                "status": "completed",
                "extracted_data": json.dumps(extracted_data, default=str),
                "error_message": None,
                "latency_ms": latency_ms,
            },
        )
        log_dataflow(
            "post_call.extraction.completed",
            f"id={extraction_id} latency_ms={latency_ms} keys={sorted(extracted_data)}",
        )

    async def mark_failed(
        self,
        *,
        extraction_id: uuid.UUID,
        latency_ms: int,
        error_message: str,
    ) -> None:
        await self.session.execute(
            self._UPDATE_SQL,
            {
                "id": extraction_id,
                "status": "failed",
                "extracted_data": json.dumps({}),
                "error_message": error_message,
                "latency_ms": latency_ms,
            },
        )
        log_dataflow(
            "post_call.extraction.failed",
            f"id={extraction_id} latency_ms={latency_ms} error={error_message[:160]}",
            level="warning",
        )


# ---------------------------------------------------------------------------
# voice_call_enrichments (one row per call_id)
# ---------------------------------------------------------------------------


class VoiceCallEnrichmentsRepository:
    _UPSERT_SQL = text(
        """
        INSERT INTO voice_call_enrichments (
            call_id, agent_id, agent_name, channel_type,
            session_outcome, end_reason,
            preferred_language, successful, picked_up, transfer_requested,
            recording_url, recording_gcs_uri,
            cost_usd,
            custom_attributes, analysis,
            created_at, updated_at
        )
        VALUES (
            :call_id, :agent_id, :agent_name, :channel_type,
            :session_outcome, :end_reason,
            :preferred_language, :successful, :picked_up, :transfer_requested,
            :recording_url, :recording_gcs_uri,
            :cost_usd,
            CAST(:custom_attributes AS jsonb),
            CAST(:analysis AS jsonb),
            NOW(), NOW()
        )
        ON CONFLICT (call_id) DO UPDATE SET
            agent_id            = EXCLUDED.agent_id,
            agent_name          = EXCLUDED.agent_name,
            channel_type        = EXCLUDED.channel_type,
            session_outcome     = EXCLUDED.session_outcome,
            end_reason          = EXCLUDED.end_reason,
            preferred_language  = EXCLUDED.preferred_language,
            successful          = EXCLUDED.successful,
            picked_up           = EXCLUDED.picked_up,
            transfer_requested  = EXCLUDED.transfer_requested,
            recording_url       = EXCLUDED.recording_url,
            recording_gcs_uri   = EXCLUDED.recording_gcs_uri,
            cost_usd            = COALESCE(EXCLUDED.cost_usd, voice_call_enrichments.cost_usd),
            custom_attributes   = EXCLUDED.custom_attributes,
            analysis            = EXCLUDED.analysis,
            updated_at          = NOW()
        """
    )

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(
        self,
        *,
        call_id: uuid.UUID,
        agent_id: uuid.UUID | None,
        agent_name: str | None,
        channel_type: str | None,
        session_outcome: str | None,
        end_reason: str | None,
        preferred_language: str | None,
        successful: bool | None,
        picked_up: bool | None,
        transfer_requested: bool | None,
        recording_url: str | None,
        recording_gcs_uri: str | None,
        cost_usd: float | None = None,
        custom_attributes: dict[str, Any] | None = None,
        analysis: dict[str, Any] | None = None,
    ) -> None:
        await self.session.execute(
            self._UPSERT_SQL,
            {
                "call_id": call_id,
                "agent_id": agent_id,
                "agent_name": agent_name,
                "channel_type": channel_type,
                "session_outcome": session_outcome,
                "end_reason": end_reason,
                "preferred_language": preferred_language,
                "successful": successful,
                "picked_up": picked_up,
                "transfer_requested": transfer_requested,
                "recording_url": recording_url,
                "recording_gcs_uri": recording_gcs_uri,
                "cost_usd": cost_usd,
                "custom_attributes": json.dumps(
                    custom_attributes or {}, default=str
                ),
                "analysis": json.dumps(analysis or {}, default=str),
            },
        )
        log_dataflow(
            "post_call.enrichment.upserted",
            f"call={call_id} outcome={session_outcome} "
            f"successful={successful} transfer={transfer_requested} "
            f"language={preferred_language} cost_usd={cost_usd}",
        )
