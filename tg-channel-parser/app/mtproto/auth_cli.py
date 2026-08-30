"""One-shot interactive MTProto auth → print TELEGRAM_SESSION_STRING.

Usage:
  python -m app.mtproto.auth_cli

Does NOT write .env automatically (doc §5.2).
"""

from __future__ import annotations

import asyncio
import getpass
import sys

from pyrogram.errors import SessionPasswordNeeded

from app.config import get_settings
from app.logging_setup import setup_logging
from app.mtproto.client_factory import create_auth_client


async def _run() -> int:
    settings = get_settings()
    setup_logging(settings.log_level, settings.secret_values())

    if not settings.telegram_api_id or not settings.telegram_api_hash:
        print("ERROR: TELEGRAM_API_ID and TELEGRAM_API_HASH required in .env", file=sys.stderr)
        return 1

    client = create_auth_client(settings)
    print("Starting interactive authorization...", file=sys.stderr)
    print("Telegram will send a code to your app / SMS.", file=sys.stderr)

    await client.connect()
    try:
        if await client.get_me() is not None:
            session_string = await client.export_session_string()
            print("\n# Already authorized. Copy into .env as TELEGRAM_SESSION_STRING=\n")
            print(session_string)
            return 0
    except Exception:
        pass

    phone = settings.telegram_phone_number
    if not phone:
        phone = input("Phone number (+7...): ").strip()

    sent = await client.send_code(phone)
    code = input("Telegram login code: ").strip()
    try:
        await client.sign_in(phone, sent.phone_code_hash, code)
    except SessionPasswordNeeded:
        if settings.telegram_2fa_password:
            password = settings.telegram_2fa_password
        else:
            password = getpass.getpass("2FA password (hidden): ")
        await client.check_password(password)

    session_string = await client.export_session_string()
    await client.disconnect()

    print("\n# Copy this value into .env as TELEGRAM_SESSION_STRING= (do not commit)\n")
    print(session_string)
    print("\n# Done. Delete any *.session files if present.", file=sys.stderr)
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
