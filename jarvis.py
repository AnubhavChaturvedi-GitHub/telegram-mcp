"""JARVIS AI TELEGRAM

Two Telegram clients on one event loop:

  userbot  - your real account over MTProto. It is the ears and the mouth.
             It sees every DM, group and channel you are in, with full history.
  control  - a BotFather bot that is ONLY yours. It is the cockpit. Every
             drafted reply arrives here with Approve / Edit / Ignore before a
             single character reaches another human.

Brain is NVIDIA NIM. Context is SQLite. Nothing sends itself until you say so.
"""
import asyncio
import logging
import sys
import time

from telethon import TelegramClient, events, Button
from telethon.tl.types import User, Chat, Channel

import config
import memory
import brain
import agent
import tools
import plugins

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOGS / "jarvis.log", encoding="utf-8"),
    ],
)
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("jarvis")

user = TelegramClient(config.SESSION, config.TG_API_ID, config.TG_API_HASH)
control = TelegramClient(config.BOT_SESSION, config.TG_API_ID, config.TG_API_HASH)

ME = {"id": None, "name": ""}
EDIT_WAITING = {}          # owner_id -> pending token
STATS = {"seen": 0, "drafted": 0, "sent": 0, "skipped": 0, "started": time.time()}


# ----------------------------------------------------------------- helpers

def chat_kind(entity):
    if isinstance(entity, User):
        return "private"
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, Channel):
        return "channel" if entity.broadcast else "group"
    return "unknown"


def chat_name(entity):
    if isinstance(entity, User):
        parts = [entity.first_name or "", entity.last_name or ""]
        name = " ".join(p for p in parts if p).strip()
        return name or (entity.username or f"user {entity.id}")
    return getattr(entity, "title", None) or f"chat {getattr(entity, 'id', '?')}"


def trim(text, n=280):
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


async def notify_owner(text, buttons=None):
    if not config.TG_OWNER_ID:
        log.warning("TG_OWNER_ID is not set, cannot reach the cockpit")
        return None
    try:
        return await control.send_message(
            config.TG_OWNER_ID, text, buttons=buttons, link_preview=False
        )
    except ValueError as exc:
        # Telegram forbids a bot from opening a chat with a human. Until the
        # owner presses START, there is no entity for the bot to send to.
        log.error(
            "Cockpit cannot reach you yet. Open Telegram, find the control bot, "
            "press START, then this works for good. (%s)", str(exc)[:80]
        )
        return None
    except Exception as exc:
        log.error("cockpit send failed: %s", exc)
        return None


async def send_as_me(chat_id, text, reply_to=None):
    """Types for a beat, then sends from the real account."""
    try:
        target = await user.get_entity(chat_id)
    except Exception:
        target = chat_id
    try:
        if config.TYPING_DELAY > 0:
            async with user.action(target, "typing"):
                await asyncio.sleep(min(config.TYPING_DELAY, 10))
        await user.send_message(target, text, reply_to=reply_to)
        memory.mark_replied(chat_id)
        STATS["sent"] += 1
        return True
    except Exception as exc:
        log.error("send failed for chat %s: %s", chat_id, exc)
        await notify_owner(f"⚠️ Could not send to {chat_id}\n{exc}")
        return False


# ----------------------------------------------------- userbot: the ears

@user.on(events.NewMessage(outgoing=True))
async def remember_my_own_words(event):
    """Everything you type yourself is context and voice training."""
    try:
        entity = await event.get_chat()
        memory.log_message(
            event.chat_id, event.id, chat_name(entity), chat_kind(entity),
            ME["id"], ME["name"], True, event.raw_text,
        )
    except Exception as exc:
        log.debug("outgoing log skipped: %s", exc)


@user.on(events.NewMessage(incoming=True))
async def on_incoming(event):
    try:
        entity = await event.get_chat()
    except Exception:
        return

    kind = chat_kind(entity)
    title = chat_name(entity)
    chat_id = event.chat_id

    sender = await event.get_sender()
    sender_name = chat_name(sender) if sender else "unknown"
    sender_id = getattr(sender, "id", None)
    text = (event.raw_text or "").strip()

    # Always remember, even in chats we will never answer.
    memory.touch_peer(chat_id, title, kind)
    memory.log_message(chat_id, event.id, title, kind, sender_id,
                       sender_name, False, text)
    STATS["seen"] += 1

    # ---- gates, cheapest first ----
    if kind == "channel":
        return
    if getattr(sender, "bot", False):
        return
    if not text:
        return
    if kind == "private" and not config.HANDLE_PRIVATE:
        return
    if kind == "group" and not config.HANDLE_GROUPS:
        return
    if config.ALLOWLIST and chat_id not in config.ALLOWLIST:
        return
    if chat_id in config.BLOCKLIST:
        return

    mode = memory.get_mode(chat_id)
    if mode == "off":
        return

    # In a group, only answer when actually spoken to.
    if kind == "group":
        addressed = bool(event.mentioned)
        if not addressed and event.is_reply:
            try:
                parent = await event.get_reply_message()
                addressed = parent and parent.sender_id == ME["id"]
            except Exception:
                addressed = False
        if not addressed:
            return

    if mode == "auto" and memory.cooling_down(chat_id):
        log.info("cooldown, holding fire in %s", title)
        return

    log.info("[%s] %s: %s", title, sender_name, trim(text, 90))

    reply = await brain.draft_reply(chat_id, title, kind, text, sender_name)
    if not reply:
        STATS["skipped"] += 1
        log.info("brain declined to answer in %s", title)
        return

    STATS["drafted"] += 1

    if mode == "auto":
        await send_as_me(chat_id, reply, reply_to=event.id)
        await notify_owner(
            f"✅ Auto replied in <b>{title}</b>\n\n"
            f"<i>{trim(text, 160)}</i>\n→ {reply}",
            buttons=[[Button.inline("Switch to draft", f"dr:{chat_id}".encode())]],
        )
        return

    token = memory.add_pending(chat_id, event.id, title, text, reply)
    await notify_owner(
        f"✍️ <b>{title}</b>  ·  {sender_name}\n"
        f"<code>{chat_id}</code>\n\n"
        f"<b>They said</b>\n<i>{trim(text, 400)}</i>\n\n"
        f"<b>Jarvis would reply</b>\n{reply}",
        buttons=[
            [Button.inline("✅ Send", f"ok:{token}".encode()),
             Button.inline("✏️ Edit", f"ed:{token}".encode())],
            [Button.inline("\U0001f6ab Ignore", f"no:{token}".encode()),
             Button.inline("⚡ Auto this chat", f"au:{chat_id}".encode())],
        ],
    )


# ------------------------------------------- control bot: the chat window
# You type plain requests here. The agent operates your real account.
# Anything that writes stops for a tap first.

def owner_only(event):
    return event.sender_id == config.TG_OWNER_ID


def confirm_buttons(token, editable):
    row = [Button.inline("\u2705 Do it", f"go:{token}".encode())]
    if editable:
        row.append(Button.inline("\u270f\ufe0f Edit", f"ex:{token}".encode()))
    row.append(Button.inline("\u274c Cancel", f"xx:{token}".encode()))
    return [row]


async def ask_confirm(event, token, note):
    item = agent.get_pending(token)
    if not item:
        await event.reply("That action vanished. Ask again.")
        return
    body = await agent.preview(item["tool"], item["args"])
    editable = "text" in item["args"] or "note" in item["args"]
    head = (note + "\n\n") if note else ""
    await event.reply(head + body, buttons=confirm_buttons(token, editable),
                      parse_mode="html", link_preview=False)


@control.on(events.CallbackQuery)
async def on_button(event):
    if not owner_only(event):
        await event.answer("Not your account.", alert=True)
        return

    action, _, arg = event.data.decode().partition(":")

    # ---- agent actions ----
    if action == "go":
        item = agent.get_pending(arg)
        if not item:
            await event.answer("Expired.", alert=True)
            return
        ok, result = await agent.execute(arg, event.sender_id)
        STATS["sent"] += 1 if ok else 0
        await event.edit(("\u2705 " if ok else "\u26a0\ufe0f ") + str(result),
                         parse_mode=None)
        return

    if action == "xx":
        agent.PENDING.pop(arg, None)
        await event.edit("\u274c Cancelled.", parse_mode=None)
        return

    if action == "ex":
        if not agent.get_pending(arg):
            await event.answer("Expired.", alert=True)
            return
        EDIT_WAITING[event.sender_id] = ("agent", arg)
        await event.answer("Send me the text you want.")
        await event.respond("Type the replacement text. /cancel to drop it.")
        return

    # ---- unattended autoreply drafts ----
    if action in ("au", "dr"):
        chat_id = int(arg)
        mode = "auto" if action == "au" else "draft"
        memory.set_mode(chat_id, mode)
        await event.answer(f"This chat is now {mode}.")
        return

    row = memory.get_pending(arg)
    if not row:
        await event.answer("That draft has expired.", alert=True)
        return
    if row["status"] != "open":
        await event.answer(f"Already {row['status']}.", alert=True)
        return

    if action == "ok":
        ok = await send_as_me(row["chat_id"], row["draft"], reply_to=row["reply_to"])
        memory.close_pending(arg, "sent" if ok else "failed")
        await event.edit(f"\u2705 Sent to {row['chat_title']}\n\n{row['draft']}"
                         if ok else f"\u26a0\ufe0f Send failed for {row['chat_title']}",
                         parse_mode=None)
    elif action == "no":
        memory.close_pending(arg, "ignored")
        await event.edit(f"Ignored - {row['chat_title']}", parse_mode=None)
    elif action == "ed":
        EDIT_WAITING[event.sender_id] = ("autoreply", arg)
        await event.answer("Send me the replacement text.")
        await event.respond(f"Type the reply for {row['chat_title']}. /cancel to drop it.")


@control.on(events.NewMessage(incoming=True))
async def on_control_message(event):
    if not owner_only(event):
        return
    text = (event.raw_text or "").strip()
    if not text:
        return

    # An edit in progress swallows the next plain message.
    waiting = EDIT_WAITING.get(event.sender_id)
    if waiting and not text.startswith("/"):
        kind, token = waiting
        EDIT_WAITING.pop(event.sender_id, None)
        if kind == "agent":
            ok, result = await agent.execute(token, event.sender_id, override_text=text)
            await event.reply(("\u2705 " if ok else "\u26a0\ufe0f ") + str(result),
                              parse_mode=None)
        else:
            row = memory.get_pending(token)
            if not row or row["status"] != "open":
                await event.reply("That draft is gone.")
                return
            memory.update_draft(token, text)
            ok = await send_as_me(row["chat_id"], text, reply_to=row["reply_to"])
            memory.close_pending(token, "sent" if ok else "failed")
            await event.reply(f"\u2705 Sent your version to {row['chat_title']}"
                              if ok else "\u26a0\ufe0f Send failed.", parse_mode=None)
        return

    low = text.lower()

    if low in ("/start", "/help"):
        await event.reply(
            "I control your Telegram. Just tell me what you want.\n\n"
            "  what did I miss today\n"
            "  read my chat with Rahul\n"
            "  search everywhere for fee structure\n"
            "  tell Priya I will call her at 6\n"
            "  reply to the last message in Java Batch 47\n"
            "  mark the college group as read\n"
            "  put Rahul on auto reply\n"
            "  remember that Rohit is my manager, stay formal\n\n"
            "Anything that sends or changes something asks you first.\n\n"
            "/new  forget this conversation\n"
            "/status  what is running\n"
            "/model  which models are loaded",
            parse_mode=None)
        return

    if low == "/cancel":
        EDIT_WAITING.pop(event.sender_id, None)
        await event.reply("Dropped.")
        return

    if low == "/new":
        agent.reset(event.sender_id)
        await event.reply("Fresh start. I have forgotten the thread, not your chats.")
        return

    if low == "/model":
        await event.reply(
            f"controller: {config.AGENT_MODEL}\n"
            f"writer: {config.NVIDIA_MODEL}\n"
            f"via {config.NVIDIA_BASE_URL}", parse_mode=None)
        return

    if low == "/status":
        up = int(time.time() - STATS["started"])
        await event.reply(
            f"Up {up // 3600}h {(up % 3600) // 60}m as {ME['name']}\n"
            f"controller {config.AGENT_MODEL}\n"
            f"writer {config.NVIDIA_MODEL}\n\n"
            f"messages seen {STATS['seen']}\n"
            f"drafts written {STATS['drafted']}\n"
            f"replies sent {STATS['sent']}\n"
            f"deliberately skipped {STATS['skipped']}", parse_mode=None)
        return

    # ---- everything else is a request for the agent ----
    async with control.action(event.chat_id, "typing"):
        try:
            out = await agent.run(event.sender_id, text)
        except Exception as exc:
            log.exception("agent crashed")
            await event.reply(f"I broke: {str(exc)[:250]}", parse_mode=None)
            return

    if out.get("pending"):
        await ask_confirm(event, out["pending"], out.get("text") or "")
    else:
        body = out.get("text") or "Done."
        for i in range(0, len(body), 3800):
            await event.reply(body[i:i + 3800], parse_mode=None, link_preview=False)


# ----------------------------------------------------------------- startup

async def main():
    if config.MISSING:
        print("\n  Missing in .env: " + ", ".join(config.MISSING))
        print("  Fill them in and run again.\n")
        return

    print("\n  Checking NVIDIA NIM ...")
    try:
        pong = await brain.health_check()
        print(f"  NIM ok  ({config.NVIDIA_MODEL})  ->  {trim(pong, 40)}")
    except Exception as exc:
        print(f"\n  NIM FAILED: {exc}\n  Check NVIDIA_API_KEY and NVIDIA_MODEL.\n")
        return

    await user.start(
        phone=config.TG_PHONE or (lambda: input("  Phone (+91...): ").strip())
    )
    me = await user.get_me()
    ME["id"] = me.id
    ME["name"] = chat_name(me)
    tools.bind(user)
    plugins.bind(user)
    n_ok, n_bad = plugins.load_all()
    if n_ok or n_bad:
        print(f"  Self-built tools: {n_ok} loaded"
              + (f", rejected {n_bad}" if n_bad else ""))
    print(f"  Account linked: {ME['name']}  (id {ME['id']})")

    if config.TG_BOT_TOKEN:
        await control.start(bot_token=config.TG_BOT_TOKEN)
        control.parse_mode = "html"
        bot_me = await control.get_me()
        print(f"  Cockpit online: @{bot_me.username}")
        await notify_owner(
            f"\U0001f7e2 <b>Jarvis is online</b>\n"
            f"Account: {ME['name']}\n"
            f"Model: <code>{config.NVIDIA_MODEL}</code>\n"
            f"Default mode: <b>{config.DEFAULT_MODE}</b>\n\n"
            "Just tell me what you want. /help for examples."
        )
    else:
        print("  No TG_BOT_TOKEN, running headless (no approval panel).")

    print("\n  Listening. Ctrl+C to stop.\n")
    await user.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  Jarvis stopped.\n")
