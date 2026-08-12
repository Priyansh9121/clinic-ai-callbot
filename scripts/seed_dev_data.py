#!/usr/bin/env python
"""Load fictional development data.

    uv run python scripts/seed_dev_data.py [--reset]

Refuses to run against APP_ENV=production.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.database.seed import seed_all  # noqa: E402
from app.database.session import dispose_engine, session_scope  # noqa: E402


async def main(reset: bool) -> int:
    settings = get_settings()
    configure_logging(settings)

    if settings.is_production:
        print("refusing to seed fictional data into a production environment", file=sys.stderr)
        return 2

    async with session_scope() as session:
        if reset:
            from sqlalchemy import text

            print("resetting clinic data…")
            await session.execute(
                text(
                    "TRUNCATE call_events, calls, audit_events, appointments, "
                    "availability_slots, patients, practitioners, appointment_types, "
                    "locations, clinics RESTART IDENTITY CASCADE"
                )
            )
            await session.commit()

        clinic = await seed_all(session)
        print(f"seeded: {clinic.name} ({clinic.timezone})")
        print("NOTE: all seeded records are fictional development data.")

    await dispose_engine()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="truncate clinic tables first")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.reset)))
