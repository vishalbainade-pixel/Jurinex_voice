"""Case-status lookup — demo implementation, ready to wire to a real Jurinex API."""

from __future__ import annotations

import hashlib
from typing import Any

from app.db.schemas import CheckCaseStatusInput
from app.observability.logger import log_dataflow

# TODO: replace with real Jurinex case-management API call.
_DEMO_STATUSES = ["filed", "under_review", "scheduled_hearing", "awaiting_documents", "closed"]


async def check_case_status(payload: CheckCaseStatusInput) -> dict[str, Any]:
    """Return a deterministic fake case status — replace with Jurinex API in prod."""
    digest = hashlib.sha1(payload.case_id.encode("utf-8")).digest()[0]
    status = _DEMO_STATUSES[digest % len(_DEMO_STATUSES)]
    log_dataflow(
        "tool.case.status",
        f"case {payload.case_id} → {status}",
        payload={"case_id": payload.case_id, "status": status},
    )
    return {
        "success": True,
        "case_id": payload.case_id,
        "status": status,
        "message": f"Case {payload.case_id} is currently '{status}'.",
    }
