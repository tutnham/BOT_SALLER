"""Backfill channel history (doc §6.1).

Usage:
  python -m app.mtproto.backfill --channel @my_channel --from-date 2026-01-01
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import func, select, update

from app.config import get_settings
from app.db.models import ParserChannel, ParserChannelPurpose, ParserPost
from app.db.session import dispose_engine, get_session_factory
from app.logging_setup import setup_logging
from app.mtproto.client_factory import create_client
from app.mtproto.flood_wait import with_flood_wait
from app.mtproto.upsert import upsert_channel, upsert_post


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    # accept YYYY-MM-DD or ISO
    if "T" in value:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def run_backfill(
    *,
    channel: str,
    from_date: datetime | None,
    to_date: datetime | None,
    from_message_id: int | None,
    purpose: ParserChannelPurpose,
) -> int:
    settings = get_settings()
    settings.require_mtproto()
    client = create_client("backfill", settings=settings)
    await client.start()
    inserted = 0
    try:
        chat = await with_flood_wait(lambda: client.get_chat(channel))
        factory = get_session_factory()
        async with factory() as session:
            await upsert_channel(
                session,
                channel_id=chat.id,
                username=getattr(chat, "username", None),
                title=getattr(chat, "title", None),
                purpose=purpose,
            )
            await session.commit()

        kwargs: dict = {"chat_id": chat.id}
        if from_message_id is not None:
            kwargs["offset_id"] = from_message_id
        elif from_date is not None:
            kwargs["offset_date"] = from_date

        async for message in client.get_chat_history(**kwargs):
            msg_date = message.date
            if msg_date and msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            if to_date is not None and msg_date is not None and msg_date > to_date:
                continue
            if from_date is not None and msg_date is not None and msg_date < from_date:
                # history walks newest→oldest typically; stop when older than from_date
                break

            async with factory() as session:
                post = await upsert_post(session, message)
                if post is not None:
                    inserted += 1
                await session.commit()

        async with factory() as session:
            max_id = await session.scalar(
                select(func.max(ParserPost.message_id)).where(
                    ParserPost.channel_id == chat.id
                )
            )
            if max_id is not None:
                await session.execute(
                    update(ParserChannel)
                    .where(ParserChannel.channel_id == chat.id)
                    .values(last_synced_message_id=max_id)
                )
                await session.commit()

        logger.info("Backfill done channel={} inserted={}", chat.id, inserted)
        print(f"OK inserted={inserted} channel_id={chat.id}")
        return 0
    finally:
        await client.stop()
        await dispose_engine()


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.secret_values())

    parser = argparse.ArgumentParser(description="Backfill Telegram channel history")
    parser.add_argument("--channel", required=True)
    parser.add_argument("--from-date", default=None)
    parser.add_argument("--to-date", default=None)
    parser.add_argument("--from-message-id", type=int, default=None)
    parser.add_argument(
        "--purpose",
        choices=[p.value for p in ParserChannelPurpose],
        default=ParserChannelPurpose.monitoring.value,
        help="Used only when auto-registering unregistered channel",
    )
    args = parser.parse_args()

    if args.from_date and args.from_message_id:
        parser.error("--from-date and --from-message-id are mutually exclusive")

    raise SystemExit(
        asyncio.run(
            run_backfill(
                channel=args.channel,
                from_date=_parse_date(args.from_date),
                to_date=_parse_date(args.to_date),
                from_message_id=args.from_message_id,
                purpose=ParserChannelPurpose(args.purpose),
            )
        )
    )


if __name__ == "__main__":
    main()
