"""The agent's hands on your real Telegram account.

Every function here runs over MTProto as you. Read tools execute immediately.
Write tools are NOT executed here by the agent loop, they are staged for your
confirmation first. See WRITE_TOOLS at the bottom.
"""
import time
import asyncio
import difflib
import logging
from datetime import datetime, timedelta, timezone

from telethon import utils, functions
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    User, Chat, Channel, InputPeerNotifySettings, InputNotifyPeer,
    InputFolderPeer,
)

import memory

log = logging.getLogger("jarvis.tools")

_client = None
_dialog_cache = {"at": 0, "items": []}
CACHE_TTL = 45
DIALOG_LIMIT = 500      # one number, used everywhere, so counts never disagree


def bind(client):
    global _client
    _client = client


def _name(entity):
    if entity is None:
        return "unknown"
    if isinstance(entity, User):
        n = " ".join(p for p in [entity.first_name or "", entity.last_name or ""] if p)
        return n.strip() or (entity.username or f"user{entity.id}")
    return getattr(entity, "title", None) or f"chat{getattr(entity, 'id', '?')}"


def _kind(entity):
    if isinstance(entity, User):
        return "dm"
    if isinstance(entity, Channel) and entity.broadcast:
        return "channel"
    return "group"


def _when(dt):
    if not dt:
        return ""
    local = dt.astimezone()
    now = datetime.now(timezone.utc).astimezone()
    if local.date() == now.date():
        return local.strftime("%H:%M")
    if (now.date() - local.date()).days == 1:
        return "yesterday " + local.strftime("%H:%M")
    return local.strftime("%d %b %H:%M")


async def _dialogs(force=False):
    if not force and time.time() - _dialog_cache["at"] < CACHE_TTL:
        return _dialog_cache["items"]
    out = []
    async for d in _client.iter_dialogs(limit=DIALOG_LIMIT):
        out.append({
            "id": d.id, "name": d.name or _name(d.entity), "kind": _kind(d.entity),
            "unread": d.unread_count,
            "last": (d.message.message or "")[:120] if d.message else "",
            "when": _when(d.message.date) if d.message else "",
            "ts": d.message.date.timestamp() if d.message else 0,
            "deleted": bool(isinstance(d.entity, User)
                            and getattr(d.entity, "deleted", False)),
        })
    _dialog_cache.update(at=time.time(), items=out)
    return out


async def _bulk(items, action, pace=0.4, tries=3):
    """Telegram flood-limits bursts. Firing 40 deletes back to back gets most of
    them rejected, and a plain except turns that into a silent partial success.
    This paces, honours FloodWait, retries, and reports the truth."""
    done, failed, waited = 0, [], 0
    for item in items:
        for attempt in range(tries):
            try:
                await action(item)
                done += 1
                break
            except FloodWaitError as exc:
                wait = min(int(getattr(exc, "seconds", 5)) + 1, 90)
                waited += wait
                log.warning("flood wait %ss (%d/%d)", wait, attempt + 1, tries)
                await asyncio.sleep(wait)
            except Exception as exc:
                failed.append(f"{item.get('name', item)}: {str(exc)[:60]}")
                break
        else:
            failed.append(f"{item.get('name', item)}: still rate limited")
        await asyncio.sleep(pace)
    return done, failed, waited


def _report(verb, done, total, failed, waited):
    msg = f"{verb} {done} of {total}."
    if waited:
        msg += f" Telegram rate limited us, waited {waited}s."
    if failed:
        msg += f" {len(failed)} failed: " + "; ".join(failed[:4])
        if len(failed) > 4:
            msg += f" and {len(failed)-4} more"
    return msg


def _score(query, name):
    """Ranked fuzzy match. Substring alone matched MARK inside 'mark everything'."""
    q, n = query.lower().strip(), name.lower().strip()
    if q == n:
        return 1.0
    if n.startswith(q):
        return 0.92
    if q in n.split():
        return 0.88
    if q in n:
        return 0.75
    return difflib.SequenceMatcher(None, q, n).ratio() * 0.7


async def resolve_chat(ref):
    """Turn 'Rahul' or an id into one chat. Ambiguity is reported, never guessed,
    because sending to the wrong Rahul is the whole risk of this system."""
    if ref is None:
        return None, "no chat given"
    ref = str(ref).strip()
    if ref.lstrip("-").isdigit():
        try:
            e = await _client.get_entity(int(ref))
            return {"id": utils.get_peer_id(e), "name": _name(e), "kind": _kind(e)}, None
        except Exception:
            return None, f"no chat with id {ref}"

    low = ref.lower().lstrip("@")
    ds = await _dialogs()

    scored = sorted(((_score(low, d["name"]), d) for d in ds),
                    key=lambda x: -x[0])
    best = [(sc, d) for sc, d in scored if sc >= 0.6]

    if best and best[0][0] >= 0.9:
        # A clear winner, unless something else ties it exactly.
        ties = [d for sc, d in best if sc >= best[0][0] - 0.01]
        if len(ties) == 1:
            return best[0][1], None
        names = ", ".join(f"{d['name']} ({d['id']})" for d in ties[:8])
        return None, f"AMBIGUOUS, {len(ties)} chats match '{ref}': {names}. Ask which one."

    if len(best) == 1:
        return best[0][1], None
    if len(best) > 1:
        names = ", ".join(f"{d['name']} ({d['id']})" for _, d in best[:8])
        return None, f"AMBIGUOUS, {len(best)} chats match '{ref}': {names}. Ask which one."

    # A bare name must NEVER fall through to a global username lookup. That is
    # how "Zzyzx" resolved to a stranger called Sipp Sipp. Reaching someone
    # outside your own chats has to be spelled with an explicit @.
    if ref.startswith("@"):
        try:
            e = await _client.get_entity(ref)
            return {"id": utils.get_peer_id(e), "name": _name(e),
                    "kind": _kind(e), "stranger": True}, None
        except Exception:
            return None, f"no Telegram account with username {ref}"

    near = ", ".join(d["name"] for _, d in scored[:5])
    return None, (f"no chat matching '{ref}' among your {len(ds)} chats. "
                  f"Closest names: {near}. If you meant someone you have never "
                  f"messaged, give their @username explicitly.")


# ------------------------------------------------------------------ READ

async def list_chats(limit=20, unread_only=False):
    ds = await _dialogs()
    if unread_only:
        ds = [d for d in ds if d["unread"] > 0]
    ds = sorted(ds, key=lambda d: d["ts"], reverse=True)[: int(limit)]
    if not ds:
        return "No chats match."
    return "\n".join(
        f"[{d['id']}] {d['name']} ({d['kind']})"
        f"{'  UNREAD x' + str(d['unread']) if d['unread'] else ''}"
        f"  {d['when']}  | {d['last'][:70]}"
        for d in ds
    )


async def read_chat(chat, limit=25):
    c, err = await resolve_chat(chat)
    if err:
        return err
    lines = []
    async for m in _client.iter_messages(c["id"], limit=int(limit)):
        if not (m.message or "").strip():
            continue
        if m.out:
            who = "YOU"
        else:
            s = await m.get_sender()
            who = _name(s)
        lines.append(f"[{m.id}] {_when(m.date)}  {who}: {m.message[:400]}")
    if not lines:
        return f"No text messages in {c['name']}."
    return f"--- {c['name']} ({c['id']}) newest first ---\n" + "\n".join(reversed(lines))


async def search_messages(query, chat=None, days=None, limit=25):
    entity = None
    label = "everywhere"
    if chat:
        c, err = await resolve_chat(chat)
        if err:
            return err
        entity = c["id"]
        label = c["name"]
    cutoff = None
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
    lines = []
    try:
        async for m in _client.iter_messages(entity, search=query, limit=int(limit)):
            if cutoff and m.date < cutoff:
                continue
            if not (m.message or "").strip():
                continue
            s = await m.get_sender()
            who = "YOU" if m.out else _name(s)
            try:
                ch = await m.get_chat()
                where = _name(ch)
            except Exception:
                where = ""
            lines.append(f"[{m.id}] {_when(m.date)}  {where} | {who}: {m.message[:300]}")
    except Exception as exc:
        return f"search failed: {exc}"
    if not lines:
        return f"Nothing found for '{query}' in {label}."
    return f"--- {len(lines)} hits for '{query}' in {label} ---\n" + "\n".join(lines)


async def unread_digest(limit=15):
    ds = [d for d in await _dialogs(force=True) if d["unread"] > 0]
    if not ds:
        return "Nothing unread. Inbox is clear."
    ds = sorted(ds, key=lambda d: d["ts"], reverse=True)[: int(limit)]
    out = []
    for d in ds:
        msgs = []
        async for m in _client.iter_messages(d["id"], limit=min(d["unread"], 6)):
            if not (m.message or "").strip() or m.out:
                continue
            s = await m.get_sender()
            msgs.append(f"    {_name(s)}: {m.message[:200]}")
        out.append(f"[{d['id']}] {d['name']} ({d['kind']}) x{d['unread']} unread  {d['when']}\n"
                   + "\n".join(reversed(msgs)))
    return f"--- {len(ds)} chats with unread ---\n" + "\n\n".join(out)


async def find_contact(name):
    ds = await _dialogs()
    low = name.lower().lstrip("@")
    hits = [d for d in ds if low in d["name"].lower()]
    if not hits:
        return f"No chat matching '{name}'."
    return "\n".join(f"[{d['id']}] {d['name']} ({d['kind']})  last {d['when']}"
                     for d in hits[:15])


async def chat_info(chat):
    c, err = await resolve_chat(chat)
    if err:
        return err
    mode = memory.get_mode(c["id"])
    notes = memory.get_notes(c["id"]) or "none"
    seen = memory.message_count(c["id"])
    extra = ""
    if c["kind"] == "group":
        try:
            parts = await _client.get_participants(c["id"], limit=0)
            extra = f"\nMembers: {parts.total}"
        except Exception:
            pass
    return (f"{c['name']}\nid: {c['id']}\ntype: {c['kind']}\n"
            f"autoreply mode: {mode}\nnotes: {notes}\n"
            f"messages Jarvis has logged: {seen}{extra}")


async def get_status():
    ds = await _dialogs()
    unread = sum(d["unread"] for d in ds)
    modes = {}
    for p in memory.all_peers():
        modes[p["mode"] or "draft"] = modes.get(p["mode"] or "draft", 0) + 1
    return (f"Chats: {len(ds)}\nUnread messages: {unread}\n"
            f"Autoreply modes: {modes or 'all default'}\n"
            f"Chats Jarvis has logged: {len(memory.all_peers())}")


# ----------------------------------------------------------------- WRITE
# These execute only after you confirm. The agent stages them, it does not fire.

async def send_message(chat, text):
    c, err = await resolve_chat(chat)
    if err:
        raise ValueError(err)
    await _client.send_message(c["id"], text)
    memory.mark_replied(c["id"])
    return f"Sent to {c['name']}."


async def reply_to(chat, message_id, text):
    c, err = await resolve_chat(chat)
    if err:
        raise ValueError(err)
    await _client.send_message(c["id"], text, reply_to=int(message_id))
    memory.mark_replied(c["id"])
    return f"Replied in {c['name']}."


async def forward(from_chat, message_id, to_chat):
    src, e1 = await resolve_chat(from_chat)
    dst, e2 = await resolve_chat(to_chat)
    if e1 or e2:
        raise ValueError(e1 or e2)
    await _client.forward_messages(dst["id"], int(message_id), src["id"])
    return f"Forwarded from {src['name']} to {dst['name']}."


async def mark_read(chat):
    c, err = await resolve_chat(chat)
    if err:
        raise ValueError(err)
    await _client.send_read_acknowledge(c["id"])
    _dialog_cache["at"] = 0
    return f"Marked {c['name']} as read."


async def mark_all_read(unread_only=True):
    """The tool whose absence made the agent mangle 'mark everything as read'
    into marking one contact called MARK."""
    ds = await _dialogs(force=True)
    targets = [d for d in ds if d["unread"] > 0] if unread_only else ds
    total = len(targets)
    done, failed, waited = await _bulk(
        targets, lambda d: _client.send_read_acknowledge(d["id"]), pace=0.25)
    _dialog_cache["at"] = 0
    return _report("Marked", done, total, failed, waited) + " chats read."


async def unread_totals():
    ds = await _dialogs(force=True)
    hot = [d for d in ds if d["unread"] > 0]
    return len(hot), sum(d["unread"] for d in hot)


async def set_mode(chat, mode):
    c, err = await resolve_chat(chat)
    if err:
        raise ValueError(err)
    if mode not in ("draft", "auto", "off"):
        raise ValueError("mode must be draft, auto or off")
    memory.set_mode(c["id"], mode)
    return f"{c['name']} autoreply is now {mode}."


async def set_note(chat, note):
    c, err = await resolve_chat(chat)
    if err:
        raise ValueError(err)
    memory.set_notes(c["id"], note)
    return f"Note saved for {c['name']}."


async def roster(limit=70):
    """Grounding. The model used to guess names blind and call find_contact on
    hope. Now it starts every turn holding the real list with real ids."""
    ds = await _dialogs()
    dead = [d for d in ds if d["deleted"]]
    # Listing dead chats individually made the model call delete_chat on ONE of
    # them when asked to delete ALL. Give the count and the bulk tool instead.
    live = [d for d in ds if not d["deleted"]]
    hot = [d for d in live if d["unread"] > 0]
    rest = sorted((d for d in live if d["unread"] == 0), key=lambda d: -d["ts"])
    picked = (hot + rest)[:limit]
    lines = [f"{d['id']} | {d['name']} | {d['kind']}"
             + (f" | {d['unread']} unread" if d["unread"] else "")
             for d in picked]
    head = (f"{len(ds)} chats total, {sum(d['unread'] for d in ds)} unread.")
    if dead:
        head += (f"\nAlso {len(dead)} dead chats from people who deleted their "
                 f"Telegram account. They are NOT listed below by design. To "
                 f"remove them all use delete_deleted_account_chats, which takes "
                 f"no arguments. Never use delete_chat for those.")
    return head + "\n" + "\n".join(lines)


# ------------------------------------------------- chat lifecycle (WRITE)

async def find_deleted_accounts(fresh=True):
    """Telegram leaves dead DMs behind when someone deletes their account.
    Reads the same dialog source as everything else, so counts never disagree."""
    return [d for d in await _dialogs(force=fresh) if d["deleted"]]


async def list_deleted_accounts():
    dead = await find_deleted_accounts()
    if not dead:
        return "No deleted-account chats. Nothing to clean up."
    lines = "\n".join(f"[{d['id']}] {d['name']}  last {d['when']}" for d in dead)
    return f"--- {len(dead)} deleted-account chats ---\n{lines}"


async def delete_deleted_account_chats():
    dead = await find_deleted_accounts()
    if not dead:
        return "Nothing to delete."
    total = len(dead)
    done, failed, waited = await _bulk(
        dead, lambda d: _client.delete_dialog(d["id"]))
    _dialog_cache["at"] = 0
    left = len(await find_deleted_accounts())
    msg = _report("Removed", done, total, failed, waited)
    msg += f" {left} deleted-account chats remain." if left else " None remain."
    return msg


async def delete_chat(chat):
    c, err = await resolve_chat(chat)
    if err:
        raise ValueError(err)
    await _client.delete_dialog(c["id"])
    _dialog_cache["at"] = 0
    return f"Removed {c['name']} from your chat list."


async def clear_history(chat):
    c, err = await resolve_chat(chat)
    if err:
        raise ValueError(err)
    e = await _client.get_entity(c["id"])
    await _client(functions.messages.DeleteHistoryRequest(
        peer=e, max_id=0, revoke=False))
    _dialog_cache["at"] = 0
    return f"Cleared your copy of the history in {c['name']}."


async def leave_chat(chat):
    c, err = await resolve_chat(chat)
    if err:
        raise ValueError(err)
    if c["kind"] == "dm":
        raise ValueError("That is a DM, not a group. Use delete_chat.")
    await _client.delete_dialog(c["id"])
    _dialog_cache["at"] = 0
    return f"Left {c['name']}."


async def delete_messages(chat, message_ids, for_everyone=False):
    c, err = await resolve_chat(chat)
    if err:
        raise ValueError(err)
    ids = message_ids if isinstance(message_ids, list) else [message_ids]
    ids = [int(i) for i in ids]
    await _client.delete_messages(c["id"], ids, revoke=bool(for_everyone))
    scope = "for everyone" if for_everyone else "on your side"
    return f"Deleted {len(ids)} message(s) in {c['name']} {scope}."


async def edit_message(chat, message_id, text):
    c, err = await resolve_chat(chat)
    if err:
        raise ValueError(err)
    await _client.edit_message(c["id"], int(message_id), text)
    return f"Edited message {message_id} in {c['name']}."


# ------------------------------------------------- organisation (WRITE)

async def archive_chat(chat, archive=True):
    c, err = await resolve_chat(chat)
    if err:
        raise ValueError(err)
    e = await _client.get_input_entity(c["id"])
    await _client(functions.folders.EditPeerFoldersRequest(
        folder_peers=[InputFolderPeer(peer=e, folder_id=1 if archive else 0)]))
    _dialog_cache["at"] = 0
    return f"{'Archived' if archive else 'Unarchived'} {c['name']}."


async def mute_chat(chat, mute=True):
    c, err = await resolve_chat(chat)
    if err:
        raise ValueError(err)
    e = await _client.get_input_entity(c["id"])
    await _client(functions.account.UpdateNotifySettingsRequest(
        peer=InputNotifyPeer(e),
        settings=InputPeerNotifySettings(mute_until=2 ** 31 - 1 if mute else 0)))
    return f"{'Muted' if mute else 'Unmuted'} {c['name']}."


async def pin_chat(chat, pin=True):
    c, err = await resolve_chat(chat)
    if err:
        raise ValueError(err)
    e = await _client.get_input_entity(c["id"])
    await _client(functions.messages.ToggleDialogPinRequest(peer=e, pinned=bool(pin)))
    _dialog_cache["at"] = 0
    return f"{'Pinned' if pin else 'Unpinned'} {c['name']}."


async def block_user(chat, block=True):
    c, err = await resolve_chat(chat)
    if err:
        raise ValueError(err)
    if c["kind"] != "dm":
        raise ValueError("Only a person can be blocked.")
    e = await _client.get_input_entity(c["id"])
    fn = functions.contacts.BlockRequest if block else functions.contacts.UnblockRequest
    await _client(fn(id=e))
    return f"{'Blocked' if block else 'Unblocked'} {c['name']}."


async def list_contacts(limit=100):
    res = await _client(functions.contacts.GetContactsRequest(hash=0))
    users = getattr(res, "users", [])[: int(limit)]
    if not users:
        return "No saved contacts."
    return "\n".join(
        f"[{u.id}] {_name(u)}" + ("  DELETED ACCOUNT" if getattr(u, 'deleted', False) else "")
        for u in users)


async def send_file(chat, path, caption=None):
    c, err = await resolve_chat(chat)
    if err:
        raise ValueError(err)
    await _client.send_file(c["id"], path, caption=caption)
    return f"Sent {path} to {c['name']}."


REGISTRY = {
    "list_chats": list_chats, "read_chat": read_chat,
    "search_messages": search_messages, "unread_digest": unread_digest,
    "find_contact": find_contact, "chat_info": chat_info, "get_status": get_status,
    "send_message": send_message, "reply_to": reply_to, "forward": forward,
    "mark_read": mark_read, "mark_all_read": mark_all_read,
    "set_mode": set_mode, "set_note": set_note,
    "list_deleted_accounts": list_deleted_accounts,
    "delete_deleted_account_chats": delete_deleted_account_chats,
    "delete_chat": delete_chat, "clear_history": clear_history,
    "leave_chat": leave_chat, "delete_messages": delete_messages,
    "edit_message": edit_message, "archive_chat": archive_chat,
    "mute_chat": mute_chat, "pin_chat": pin_chat, "block_user": block_user,
    "list_contacts": list_contacts, "send_file": send_file,
}

# Anything here stops for a human tap before it runs.
WRITE_TOOLS = {"send_message", "reply_to", "forward", "mark_read",
               "mark_all_read", "set_mode", "set_note",
               "delete_deleted_account_chats", "delete_chat", "clear_history",
               "leave_chat", "delete_messages", "edit_message",
               "archive_chat", "mute_chat", "pin_chat", "block_user",
               "send_file"}

# Irreversible. The confirmation card shouts about these.
DESTRUCTIVE = {"delete_chat", "clear_history", "delete_messages", "leave_chat",
               "delete_deleted_account_chats", "block_user"}
