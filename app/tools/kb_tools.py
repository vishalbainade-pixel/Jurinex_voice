"""KB search tool — RAG against the admin-owned `kb_chunks` table."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import AgentToolEventRepository
from app.db.schemas import SearchKnowledgeBaseInput
from app.observability.logger import log_dataflow, log_event_panel
from app.services.kb_search import KbSearchService


async def search_knowledge_base(
    session: AsyncSession,
    payload: SearchKnowledgeBaseInput,
    *,
    call_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    log_dataflow(
        "tool.kb.search",
        f"q={payload.query[:80]!r} k={payload.k}",
    )

    result = await KbSearchService(session).search(
        query=payload.query,
        k=payload.k,
        call_id=call_id,
    )

    if call_id:
        await AgentToolEventRepository(session).add(
            call_id=call_id,
            tool_name="search_knowledge_base",
            input_json=payload.model_dump(),
            output_json={
                "n_results": len(result.get("results", [])),
                "top_score": result.get("top_score"),
                "confident": result.get("confident"),
                "latency_ms": result.get("latency_ms"),
            },
            success=bool(result.get("success")),
            error_message=None if result.get("success") else result.get("message"),
        )

    if result.get("success"):
        log_event_panel(
            "KB SEARCH",
            {
                "Query": payload.query[:60],
                "Results": len(result.get("results", [])),
                "Top score": f"{result.get('top_score', 0):.3f}",
                "Confident": result.get("confident"),
                "Latency": f"{result.get('latency_ms', 0)}ms",
            },
            style="cyan",
            icon_key="db",
        )

    # Trim what we hand back to the model — the LLM doesn't need full IDs/URIs
    # for every chunk, just enough to ground its answer.
    trimmed = [
        {
            "rank": i + 1,
            "score": round(r["score"], 3),
            "document": r["document_title"],
            "section": r.get("heading_path"),
            "text": r["text"],
        }
        for i, r in enumerate(result.get("results", []))
    ]
    return {
        "success": result.get("success", False),
        "confident": result.get("confident", False),
        "top_score": result.get("top_score", 0.0),
        "min_score_threshold": result.get("min_score_threshold"),
        "message": result.get("message", ""),
        "results": trimmed,
    }
