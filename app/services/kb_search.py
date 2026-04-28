"""Knowledge-base similarity search against the admin-owned KB tables.

The admin module owns ingestion (chunking, embedding, GCS, status). This
module is **read-only** against `voice_agents`, `kb_documents`, `kb_chunks`,
and **insert-only** into `kb_search_logs`.

Embedding model + 768-dim cosine match the admin pipeline exactly so the
two vector spaces are compatible.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.observability.logger import log_dataflow, log_error


# ---------------------------------------------------------------------------
# Agent-id resolution (cached at first use)
# ---------------------------------------------------------------------------

_agent_id_cache: dict[str, uuid.UUID | None] = {}
_agent_id_lock = asyncio.Lock()


async def _resolve_agent_id(session: AsyncSession) -> uuid.UUID | None:
    """Look up `voice_agents.id` for the configured agent name (cached)."""
    name = settings.kb_agent_name
    if name in _agent_id_cache:
        return _agent_id_cache[name]
    async with _agent_id_lock:
        if name in _agent_id_cache:
            return _agent_id_cache[name]
        try:
            row = (
                await session.execute(
                    text("SELECT id FROM voice_agents WHERE name = :n"),
                    {"n": name},
                )
            ).first()
            agent_id = row[0] if row else None
        except Exception as exc:
            log_dataflow(
                "kb.agent_id.lookup_error", str(exc), level="warning"
            )
            agent_id = None
        _agent_id_cache[name] = agent_id
        log_dataflow(
            "kb.agent_id.resolved",
            f"name={name!r} agent_id={agent_id}",
        )
        return agent_id


# ---------------------------------------------------------------------------
# Embedding (uses google-genai, matches admin's gemini-embedding-001 / 768d)
# ---------------------------------------------------------------------------


def _format_pgvector(values: list[float]) -> str:
    """Render a Python list as a pgvector literal: ``[v0,v1,...]``."""
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


async def _embed_query(query: str) -> list[float] | None:
    """Embed a single query string. Returns None on failure."""
    try:
        from google import genai  # noqa: PLC0415

        client = genai.Client(api_key=settings.gemini_key)
        # google-genai's embed_content is sync — push to a thread.
        result = await asyncio.to_thread(
            lambda: client.models.embed_content(
                model=settings.kb_embedding_model,
                contents=[query],
                config={
                    "task_type": "RETRIEVAL_QUERY",
                    "output_dimensionality": settings.kb_embedding_dim,
                },
            )
        )
        embeddings = getattr(result, "embeddings", None) or []
        if not embeddings:
            log_dataflow("kb.embed.empty", "no embeddings returned", level="warning")
            return None
        values = list(getattr(embeddings[0], "values", []) or [])
        if len(values) != settings.kb_embedding_dim:
            log_dataflow(
                "kb.embed.dim_mismatch",
                f"got {len(values)}, expected {settings.kb_embedding_dim}",
                level="warning",
            )
            return None
        return values
    except Exception as exc:
        log_error("KB EMBED FAILED", str(exc))
        return None


# ---------------------------------------------------------------------------
# Public service
# ---------------------------------------------------------------------------


SEARCH_SQL = text(
    """
    SELECT
      c.id           AS chunk_id,
      c.text         AS chunk_text,
      c.heading_path AS heading_path,
      c.chunk_index  AS chunk_index,
      c.document_id  AS document_id,
      d.title        AS document_title,
      d.source_type  AS source_type,
      d.gcs_uri      AS gcs_uri,
      d.agent_id     AS agent_id,
      1 - (c.embedding <=> CAST(:q AS vector)) AS score
    FROM kb_chunks c
    JOIN kb_documents d ON d.id = c.document_id
    WHERE d.status = 'ready'
      AND (
        CAST(:agent AS uuid) IS NULL
        OR d.agent_id = CAST(:agent AS uuid)
        OR d.agent_id IS NULL
      )
    ORDER BY c.embedding <=> CAST(:q AS vector)
    LIMIT :k
    """
)


class KbSearchService:
    """Embed → similarity-search → log."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def search(
        self,
        *,
        query: str,
        k: int | None = None,
        call_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        if not settings.kb_enabled:
            return {"success": False, "message": "knowledge base disabled", "results": []}
        if not query or not query.strip():
            return {"success": False, "message": "empty query", "results": []}

        k = k or settings.kb_search_k

        t0 = time.monotonic()
        embedding = await _embed_query(query)
        if embedding is None:
            return {
                "success": False,
                "message": "failed to embed query",
                "results": [],
            }

        agent_id = await _resolve_agent_id(self.session)

        try:
            rows = (
                await self.session.execute(
                    SEARCH_SQL,
                    {
                        "q": _format_pgvector(embedding),
                        "agent": str(agent_id) if agent_id else None,
                        "k": k,
                    },
                )
            ).mappings().all()
        except Exception as exc:
            log_error("KB SEARCH FAILED", str(exc), {"query": query[:80]})
            return {"success": False, "message": str(exc), "results": []}

        latency_ms = int((time.monotonic() - t0) * 1000)
        results = [
            {
                "chunk_id": str(r["chunk_id"]),
                "document_id": str(r["document_id"]),
                "document_title": r["document_title"],
                "heading_path": r["heading_path"],
                "chunk_index": r["chunk_index"],
                "text": r["chunk_text"],
                "source_type": r["source_type"],
                "gcs_uri": r["gcs_uri"],
                "score": float(r["score"]),
            }
            for r in rows
        ]
        top_score = results[0]["score"] if results else 0.0
        confident = top_score >= settings.kb_min_score

        await self._log_search(
            query=query,
            results=results,
            latency_ms=latency_ms,
            call_id=call_id,
            agent_id=agent_id,
        )

        log_dataflow(
            "kb.search.done",
            f"k={len(results)} top_score={top_score:.3f} "
            f"latency={latency_ms}ms confident={confident}",
        )

        return {
            "success": True,
            "results": results,
            "top_score": top_score,
            "confident": confident,
            "latency_ms": latency_ms,
            "min_score_threshold": settings.kb_min_score,
            "message": (
                "ok"
                if confident
                else f"top score {top_score:.2f} below threshold "
                     f"{settings.kb_min_score:.2f}; consider escalating"
            ),
        }

    async def _log_search(
        self,
        *,
        query: str,
        results: list[dict[str, Any]],
        latency_ms: int,
        call_id: uuid.UUID | None,
        agent_id: uuid.UUID | None,
    ) -> None:
        if not results and not settings.kb_enabled:
            return
        try:
            await self.session.execute(
                text(
                    """
                    INSERT INTO kb_search_logs
                      (id, call_id, agent_id, query, top_chunk_ids, top_scores,
                       latency_ms, source, created_at)
                    VALUES
                      (gen_random_uuid(), CAST(:call_id AS uuid),
                       CAST(:agent_id AS uuid), :query, CAST(:ids AS uuid[]),
                       CAST(:scores AS double precision[]),
                       :latency, 'voice_agent', now())
                    """
                ),
                {
                    "call_id": str(call_id) if call_id else None,
                    "agent_id": str(agent_id) if agent_id else None,
                    "query": query[:2000],
                    "ids": [r["chunk_id"] for r in results] or None,
                    "scores": [r["score"] for r in results] or None,
                    "latency": latency_ms,
                },
            )
        except Exception as exc:
            log_dataflow("kb.search.log_error", str(exc), level="warning")
