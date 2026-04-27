"""Compliance helpers — guardrails the agent must respect (legal advice, PII)."""

from __future__ import annotations

LEGAL_ADVICE_KEYWORDS = (
    "legal advice",
    "lawyer",
    "litigation",
    "मुकदमा",
    "वकील",
    "खटला",
)


class ComplianceService:
    @staticmethod
    def detects_legal_advice_request(text: str) -> bool:
        lower = text.lower()
        return any(k in lower for k in LEGAL_ADVICE_KEYWORDS)

    @staticmethod
    def safe_redirect_message() -> str:
        return (
            "I'm not able to provide legal advice on this call. "
            "I can connect you with a Jurinex legal expert — would you like that?"
        )
