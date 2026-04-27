"""Phone number utilities (E.164 normalization + validation)."""

from __future__ import annotations

import phonenumbers
from phonenumbers import NumberParseException


def normalize_e164(raw: str, *, default_region: str = "IN") -> str:
    """Return canonical E.164 form (e.g. ``+919226408823``).

    Raises ``ValueError`` if the number is not parseable / valid.
    """
    if not raw:
        raise ValueError("phone number is empty")
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except NumberParseException as e:
        raise ValueError(f"unparseable phone number: {raw}") from e
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError(f"invalid phone number: {raw}")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def is_valid_e164(raw: str) -> bool:
    try:
        normalize_e164(raw)
        return True
    except ValueError:
        return False
