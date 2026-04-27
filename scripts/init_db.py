"""Create all tables directly from models — convenient for first-time setup.

Prefer ``alembic upgrade head`` for production; this is a quick local helper.
"""

from __future__ import annotations

import asyncio

from app.db.database import Base, engine
from app.db import models  # noqa: F401  side-effect: register tables


async def main() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✅ tables created")


if __name__ == "__main__":
    asyncio.run(main())
