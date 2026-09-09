"""Register channel in parser_channels (doc §6.0).

Usage:
  python -m app.mtproto.add_channel --channel @supplier_x --purpose supplier_price_source
"""

from __future__ import annotations

import argparse
import asyncio

from app.config import get_settings
from app.db.models import ParserChannelPurpose
from app.db.session import dispose_engine, get_session_factory
from app.logging_setup import setup_logging
from app.mtproto.client_factory import create_client
from app.mtproto.flood_wait import with_flood_wait
from app.mtproto.upsert import upsert_channel


async def _run(channel: str, purpose: ParserChannelPurpose) -> int:
    settings = get_settings()
    setup_logging(settings.log_level, settings.secret_values())
    settings.require_mtproto()

    client = create_client("add_channel", settings=settings)
    await client.start()
    try:
        chat = await with_flood_wait(lambda: client.get_chat(channel))
        chat_id = getattr(chat, "id", None)
        if chat_id is None:
            raise RuntimeError("telegram_chat_id_missing")
        factory = get_session_factory()
        async with factory() as session:
            row = await upsert_channel(
                session,
                channel_id=int(chat_id),
                username=getattr(chat, "username", None),
                title=getattr(chat, "title", None),
                purpose=purpose,
            )
            await session.commit()
            print(
                f"OK channel_id={row.channel_id} username={row.username} "
                f"purpose={row.purpose.value} id={row.id}"
            )
            print("Restart tg-listener to pick up new channel in realtime filter.")
        return 0
    finally:
        await client.stop()
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description="Register Telegram channel for parsing")
    parser.add_argument("--channel", required=True, help="@username or numeric id")
    parser.add_argument(
        "--purpose",
        choices=[p.value for p in ParserChannelPurpose],
        default=ParserChannelPurpose.monitoring.value,
    )
    args = parser.parse_args()
    purpose = ParserChannelPurpose(args.purpose)
    raise SystemExit(asyncio.run(_run(args.channel, purpose)))


if __name__ == "__main__":
    main()
