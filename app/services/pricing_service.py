"""Per-call cost stamper using the admin's ``voice_model_pricing`` table.

Reads the row matching the agent's ``live_model``, multiplies the audio
minutes (caller-side and agent-side) by the per-minute USD rates, and
returns a small cost dict. The caller (bridge teardown) then stuffs it
into ``calls.raw_metadata.pricing`` and into
``voice_call_enrichments.cost_usd`` via the enrichment writer.

The table also carries an INR-equivalent (``inr_one_minute_total``) which
the admin dashboard renders directly. We surface both so the dashboard
doesn't have to recompute anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import log_dataflow


@dataclass(slots=True)
class ModelPricing:
    model_id: str
    display_name: str
    input_audio_usd_per_minute: float | None
    output_audio_usd_per_minute: float | None
    inr_one_minute_total: float | None

    def cost_usd(self, caller_seconds: float, agent_seconds: float) -> float | None:
        """Return USD cost for ``(caller, agent)`` audio durations.

        Returns None when neither USD column is populated (e.g. the
        gemini-2.5-flash text row, which has nulls).
        """
        if (
            self.input_audio_usd_per_minute is None
            and self.output_audio_usd_per_minute is None
        ):
            return None
        in_rate = self.input_audio_usd_per_minute or 0.0
        out_rate = self.output_audio_usd_per_minute or 0.0
        return round(
            (caller_seconds / 60.0) * in_rate
            + (agent_seconds / 60.0) * out_rate,
            6,
        )


class VoiceModelPricingRepository:
    _SQL = text(
        """
        SELECT model_id, display_name,
               input_audio_usd_per_minute,
               output_audio_usd_per_minute,
               inr_one_minute_total
        FROM voice_model_pricing
        WHERE model_id = :model_id AND is_active = TRUE
        LIMIT 1
        """
    )

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lookup(self, model_id: str) -> ModelPricing | None:
        if not model_id:
            return None
        result = await self.session.execute(self._SQL, {"model_id": model_id})
        row = result.mappings().first()
        if row is None:
            log_dataflow(
                "pricing.lookup.miss",
                f"no row in voice_model_pricing for model_id={model_id!r}",
                level="warning",
            )
            return None
        return ModelPricing(
            model_id=row["model_id"],
            display_name=row["display_name"],
            input_audio_usd_per_minute=(
                float(row["input_audio_usd_per_minute"])
                if row["input_audio_usd_per_minute"] is not None
                else None
            ),
            output_audio_usd_per_minute=(
                float(row["output_audio_usd_per_minute"])
                if row["output_audio_usd_per_minute"] is not None
                else None
            ),
            inr_one_minute_total=(
                float(row["inr_one_minute_total"])
                if row["inr_one_minute_total"] is not None
                else None
            ),
        )


async def compute_call_cost(
    session: AsyncSession,
    *,
    model_id: str,
    caller_seconds: float,
    agent_seconds: float,
) -> dict[str, Any] | None:
    """Convenience wrapper used by the bridge teardown."""
    pricing = await VoiceModelPricingRepository(session).lookup(model_id)
    if pricing is None:
        return None
    cost_usd = pricing.cost_usd(caller_seconds, agent_seconds)
    total_minutes = (caller_seconds + agent_seconds) / 60.0
    cost_inr = (
        round(total_minutes * pricing.inr_one_minute_total, 4)
        if pricing.inr_one_minute_total is not None
        else None
    )
    out = {
        "model_id": pricing.model_id,
        "display_name": pricing.display_name,
        "caller_seconds": round(caller_seconds, 2),
        "agent_seconds": round(agent_seconds, 2),
        "total_minutes": round(total_minutes, 4),
        "input_audio_usd_per_minute": pricing.input_audio_usd_per_minute,
        "output_audio_usd_per_minute": pricing.output_audio_usd_per_minute,
        "cost_usd": cost_usd,
        "cost_inr_estimate": cost_inr,
    }
    log_dataflow(
        "pricing.computed",
        f"model={pricing.model_id} minutes={total_minutes:.2f} "
        f"usd={cost_usd} inr={cost_inr}",
    )
    return out
