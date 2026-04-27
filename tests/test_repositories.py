"""Repository contract checks — live DB tests are skipped if not reachable."""

import asyncio

import pytest

from app.db.database import ping_database


@pytest.mark.asyncio
async def test_database_ping_or_skip():
    try:
        ok = await ping_database()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"db not reachable: {exc}")
    assert ok is True


def test_event_loop_works():
    # Sanity check: pytest-asyncio config is wired up.
    loop = asyncio.new_event_loop()
    assert loop.run_until_complete(asyncio.sleep(0)) is None
    loop.close()
