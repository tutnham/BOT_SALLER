"""Wipe all requests and related operational rows. Directories stay.

Usage (from backend/):
  python -m scripts.purge_all_requests --confirm PURGE_ALL_REQUESTS
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.db.session import get_session_factory
from app.services.purge_service import purge_all_request_data

CONFIRM_TOKEN = "PURGE_ALL_REQUESTS"


def _print_counts(label: str, counts: dict[str, int]) -> None:
    print(label)
    for name, value in counts.items():
        print(f"  {name}={value}")


async def _run() -> dict[str, dict[str, int]]:
    factory = get_session_factory()
    async with factory() as session:
        result = await purge_all_request_data(session)
        await session.commit()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete all requests on DATABASE_URL")
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if args.confirm != CONFIRM_TOKEN:
        print("refusing: pass --confirm PURGE_ALL_REQUESTS")
        sys.exit(1)
    result = asyncio.run(_run())
    _print_counts("before", result["before"])
    _print_counts("after", result["after"])
    leftover = {
        name: value
        for name, value in result["after"].items()
        if name not in ("markup_rules", "suppliers") and value
    }
    if leftover:
        print("leftover operational rows:", leftover)
        sys.exit(2)


if __name__ == "__main__":
    main()
