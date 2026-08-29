"""One time Telegram login.

Telegram sends a code to your app. Only you can read it, so this step is yours.
Afterwards the session in data/ keeps you logged in and you never do this again.
It also writes TG_OWNER_ID and TG_PHONE back into .env for you.
"""
import asyncio
import re
from pathlib import Path

from telethon import TelegramClient
import config


def put_env(key, value):
    p = Path(config.ROOT / ".env")
    s = p.read_text()
    line = f"{key}={value}"
    if re.search(rf"^{key}=.*$", s, flags=re.M):
        s = re.sub(rf"^{key}=.*$", line, s, flags=re.M)
    else:
        s = s.rstrip("\n") + "\n" + line + "\n"
    p.write_text(s)
    p.chmod(0o600)


async def main():
    missing = [k for k, v in [("TG_API_ID", config.TG_API_ID),
                              ("TG_API_HASH", config.TG_API_HASH)] if not v]
    if missing:
        print(f"\n  Fill {', '.join(missing)} in .env first.\n")
        return

    print("\n  " + "=" * 58)
    print("  JARVIS  ·  one time Telegram login")
    print("  " + "=" * 58)
    print("\n  1. Enter your phone in international form, e.g. +919876543210")
    print("  2. Telegram sends a 5 digit code INSIDE the Telegram app")
    print("     (check the 'Telegram' service chat, not SMS)")
    print("  3. If you have 2FA on, it asks for your cloud password too\n")

    client = TelegramClient(config.SESSION, config.TG_API_ID, config.TG_API_HASH)
    # Telethon needs a value OR a callable here. Passing None kills its own
    # built-in prompt and it raises instead of asking.
    phone = config.TG_PHONE or (lambda: input("  Phone (+91...): ").strip())
    await client.start(phone=phone)

    me = await client.get_me()
    name = " ".join(p for p in [me.first_name or "", me.last_name or ""] if p)

    put_env("TG_OWNER_ID", me.id)
    if me.phone:
        put_env("TG_PHONE", "+" + str(me.phone).lstrip("+"))

    print("\n  " + "=" * 58)
    print(f"  Logged in as   {name}")
    print(f"  Username       @{me.username or 'none'}")
    print(f"  TG_OWNER_ID    {me.id}   (written into .env for you)")
    print("  " + "=" * 58)
    print("\n  Next:")
    print("    1. Open Telegram, find @Jarvis_Telegram_Server_bot, press START")
    print("    2. ./run.sh\n")

    await client.disconnect()


asyncio.run(main())
