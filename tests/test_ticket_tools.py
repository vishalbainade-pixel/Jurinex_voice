"""Smoke test for ticket-number generation pattern (no DB required)."""

import re

PATTERN = re.compile(r"^JX-\d{8}-\d{4}$")


def test_ticket_number_pattern_example():
    sample = "JX-20260427-0001"
    assert PATTERN.match(sample)
