"""Stepwise Telegram auth, so the login can be driven one command at a time.

  python auth.py send  +919876543210    -> asks Telegram to send the code
  python auth.py code  12345            -> signs in with that code
  python auth.py password               -> only if 2FA is on (getpass, never echoed)
  python auth.py status                 -> who am I

The phone_code_hash does not survive a process exit, so it is parked in
data/auth_state.json between the two steps.
"""
import asyncio
import json
import re
import sys
from getpass import getpass
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    PhoneNumberInvalidError, PhoneCodeInvalidError, PhoneCodeExpiredError,
    SessionPasswordNeededError, FloodWaitError,
)
import config

# `python auth.py send +91... mcp` logs in the separate MCP session.
TARGET = "mcp" if "mcp" in sys.argv[2:] else "user"
SESSION = config.MCP_SESSION if TARGET == "mcp" else config.SESSION
STATE = config.DATA / f"auth_state_{TARGET}.json"


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(d):
    STATE.write_text(json.dumps(d))
    STATE.chmod(0o600)


def put_env(key, value):
    p = Path(config.ROOT / ".env")
    s = p.read_text()
    if re.search(rf"^{key}=.*$", s, flags=re.M):
        s = re.sub(rf"^{key}=.*$", f"{key}={value}", s, flags=re.M)
    else:
        s = s.rstrip("\n") + f"\n{key}={value}\n"
    p.write_text(s)
    p.chmod(0o600)


def normalise(raw):
    """Returns (e164, error). Catches the 9-digit slip before Telegram does."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None, "empty"
    if digits.startswith("0"):
        digits = digits.lstrip("0")
    if len(digits) == 10 and digits[0] in "6789":
        digits = "91" + digits              # bare Indian mobile
    if digits.startswith("91"):
        rest = digits[2:]
        if len(rest) != 10:
            return None, (f"India needs exactly 10 digits after +91, you gave "
                          f"{len(rest)} ({rest}). Count them: a mobile looks "
                          f"like 9343682800, not 934368280.")
        if rest[0] not in "6789":
            return None, f"Indian mobiles start with 6, 7, 8 or 9. Yours starts with {rest[0]}."
    if not (8 <= len(digits) <= 15):
        return None, f"{len(digits)} digits is outside the valid range of 8 to 15."
    return "+" + digits, None


def client():
    return TelegramClient(SESSION, config.TG_API_ID, config.TG_API_HASH)


async def finish(c):
    me = await c.get_me()
    name = " ".join(p for p in [me.first_name or "", me.last_name or ""] if p)
    if TARGET == "user":
        put_env("TG_OWNER_ID", me.id)
        if me.phone:
            put_env("TG_PHONE", "+" + str(me.phone).lstrip("+"))
    STATE.unlink(missing_ok=True)
    print(f"\n  LOGGED IN ({TARGET} session)  ->  {name}   @{me.username or 'no username'}")
    print(f"  TG_OWNER_ID {me.id} written to .env\n")


async def cmd_send(raw):
    phone, err = normalise(raw)
    if err:
        print(f"\n  BAD NUMBER: {err}\n")
        return
    c = client()
    await c.connect()
    if await c.is_user_authorized():
        print("\n  Already logged in.")
        await finish(c)
        await c.disconnect()
        return
    try:
        sent = await c.send_code_request(phone)
    except PhoneNumberInvalidError:
        print(f"\n  Telegram rejected {phone}. Wrong number, or no Telegram account on it.\n")
        await c.disconnect()
        return
    except FloodWaitError as e:
        print(f"\n  Rate limited. Wait {e.seconds}s before retrying.\n")
        await c.disconnect()
        return
    save_state({"phone": phone, "hash": sent.phone_code_hash})
    print(f"\n  Code sent to {phone}")
    print(f"  Delivery: {type(sent.type).__name__.replace('SentCodeType','')}")
    print("  Look in the Telegram app, the blue 'Telegram' service chat.\n")
    await c.disconnect()


async def cmd_code(code):
    st = load_state()
    if not st:
        print("\n  No pending code. Run:  python auth.py send +91XXXXXXXXXX\n")
        return
    code = re.sub(r"\D", "", code or "")
    c = client()
    await c.connect()
    try:
        await c.sign_in(phone=st["phone"], code=code, phone_code_hash=st["hash"])
    except PhoneCodeInvalidError:
        print("\n  Wrong code. Check the digits and run the code command again.\n")
        await c.disconnect()
        return
    except PhoneCodeExpiredError:
        print("\n  That code expired. Run the send command again for a fresh one.\n")
        STATE.unlink(missing_ok=True)
        await c.disconnect()
        return
    except SessionPasswordNeededError:
        print("\n  2FA is on. Your cloud password is needed.")
        print("  Run:  ./.venv/bin/python auth.py password")
        print("  It is typed straight into Telethon, never shown, never stored.\n")
        await c.disconnect()
        return
    await finish(c)
    await c.disconnect()


async def cmd_password():
    c = client()
    await c.connect()
    if await c.is_user_authorized():
        print("\n  Already logged in.")
        await finish(c)
        await c.disconnect()
        return
    pw = getpass("  Telegram cloud password (hidden): ")
    try:
        await c.sign_in(password=pw)
    except Exception as exc:
        print(f"\n  Rejected: {exc}\n")
        await c.disconnect()
        return
    await finish(c)
    await c.disconnect()


async def cmd_status():
    c = client()
    await c.connect()
    if await c.is_user_authorized():
        await finish(c)
    else:
        st = load_state()
        print(f"\n  Not logged in. Pending code for: {st.get('phone', 'nothing')}\n")
    await c.disconnect()


CMDS = {"send": cmd_send, "code": cmd_code, "password": cmd_password, "status": cmd_status}

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] not in CMDS:
        print(__doc__)
        sys.exit(1)
    fn = CMDS[args[0]]
    asyncio.run(fn(args[1]) if args[0] in ("send", "code") else fn())
