"""Checks every credential independently and tells you exactly which one is
wrong, instead of a stack trace at 2am."""
import asyncio
import config


def line(ok, label, detail="", optional=False):
    tag = "OK " if ok else ("---" if optional else "BAD")
    print(f"  [{tag}]  {label:<22} {detail}")
    return ok


async def main():
    print("\n  JARVIS DOCTOR\n  " + "-" * 46)
    good = True

    good &= line(bool(config.TG_API_ID), "TG_API_ID",
                 str(config.TG_API_ID) if config.TG_API_ID else "missing")
    good &= line(len(config.TG_API_HASH) == 32, "TG_API_HASH",
                 f"{len(config.TG_API_HASH)} chars, expected 32")
    line(bool(config.TG_PHONE), "TG_PHONE",
         config.TG_PHONE or "optional, login asks for it", optional=True)
    good &= line(bool(config.NVIDIA_API_KEY), "NVIDIA_API_KEY",
                 f"{config.NVIDIA_API_KEY[:8]}..." if config.NVIDIA_API_KEY else "missing")
    line(bool(config.TG_BOT_TOKEN), "TG_BOT_TOKEN",
         "set" if config.TG_BOT_TOKEN else "missing, no approval panel",
         optional=True)
    line(bool(config.TG_OWNER_ID), "TG_OWNER_ID",
         str(config.TG_OWNER_ID) if config.TG_OWNER_ID
         else "missing, run login.py to get it", optional=True)

    print("  " + "-" * 46)

    if config.NVIDIA_API_KEY:
        import brain
        try:
            out = await brain.health_check()
            line(True, "NVIDIA NIM live", f"{config.NVIDIA_MODEL} -> {out[:30]}")
        except Exception as exc:
            good = False
            line(False, "NVIDIA NIM live", str(exc)[:70])

    if config.TG_BOT_TOKEN and config.TG_API_ID:
        from telethon import TelegramClient
        try:
            bot = TelegramClient(config.BOT_SESSION, config.TG_API_ID,
                                 config.TG_API_HASH)
            await bot.start(bot_token=config.TG_BOT_TOKEN)
            me = await bot.get_me()
            line(True, "Control bot", f"@{me.username}")
            await bot.disconnect()
        except Exception as exc:
            good = False
            line(False, "Control bot", str(exc)[:70])

    import os
    line(os.path.exists(config.SESSION + ".session"), "User session",
         "logged in" if os.path.exists(config.SESSION + ".session")
         else "run: python login.py", optional=True)

    print("  " + "-" * 46)
    print(f"  {'READY' if good else 'NOT READY, fix the BAD lines above'}\n")


asyncio.run(main())
