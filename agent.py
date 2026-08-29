"""The controller. Turns plain chat into actions on your Telegram account.

Model is gpt-oss-120b, chosen by benchmark: it picks the right tool where the
nemotron models reached for search when asked to send. Read tools run straight
away. Write tools are staged and wait for your tap.
"""
import re
import json
import inspect
import html
import logging
import secrets

from openai import AsyncOpenAI

import config
import tools
import plugins
from schemas import SCHEMAS, _fn

log = logging.getLogger("jarvis.agent")

client = AsyncOpenAI(api_key=config.NVIDIA_API_KEY, base_url=config.NVIDIA_BASE_URL,
                     timeout=120.0, max_retries=2)

MAX_HOPS = 6
HISTORY = {}          # owner_id -> list of messages
PENDING = {}          # token -> staged write
HISTORY_CAP = 40


def _persona():
    try:
        t = config.PERSONA_FILE.read_text(encoding="utf-8").strip()
        return f"\n\nHOW {config.OWNER_NAME.upper()} WRITES, match this when composing any message:\n{t}"
    except FileNotFoundError:
        return ""


SYSTEM = f"""You are Jarvis, {config.OWNER_NAME}'s Telegram controller. You are talking
to {config.OWNER_NAME} himself in a private control window. He types plain requests and
you operate his real Telegram account with the tools you have.

Rules:
1. USE THE TOOLS. Never answer from memory or invent chats, names, message ids or
   contents. If you do not know, call a tool and find out.
1b. The LIVE CHAT LIST below is the truth about what chats exist RIGHT NOW. Pick
   the target from it and pass the NUMERIC ID, not the name. Do not call
   find_contact for a name that is already in that list, it wastes a round trip.
   If the name he used is not in the list, say so instead of guessing.
2. Read before you write. Before replying to anyone, call read_chat so the reply
   fits the conversation.
3. Names are dangerous. If resolve reports AMBIGUOUS, do not pick one. Show the
   options and ask which he means. A word in his sentence is not automatically a
   contact name. "mark everything as read" means ALL chats, even though he has a
   contact called MARK. When he means everyone, use the tool that acts on
   everything, never the single-chat one.
4. Be brief. This is a Telegram window, not a report. No headings, no bullets
   unless listing things, no restating what he asked.
5. When you compose a message for someone else, write it as {config.OWNER_NAME}
   in his voice, not as an assistant.
6. NEVER ask permission in words. To send, forward, mark read or change a
   setting, CALL THE TOOL. The system automatically shows him a confirmation
   card with Do it / Edit / Cancel buttons before anything happens. Calling the
   tool IS how you ask. Writing "shall I go ahead?" instead of calling the tool
   just wastes his time, because he then has to say yes and wait again.
   Never say something was sent. The card reports that.
7. Plain text only. No markdown, no asterisks, no code fences.
7b. Scope words decide the tool. "all", "every", "them", "everything" mean the
   bulk tool. A singular name means the single-chat tool. Getting this backwards
   either does 1 of 40 things or 40 of 1.
8. Destructive actions (delete, clear history, leave, block) are real and cannot
   be undone. Never take one unless he clearly asked for it. Never bundle one
   into a request for something else.
9. If no tool can do what he asked, do NOT say you cannot. Call list_tools to be
   sure, then use propose_tool to write the missing tool yourself. He reviews the
   source and approves it, then it exists permanently.{_persona()}"""


def clean_args(fn, args):
    """Models emit junk keys for zero-parameter tools, notably {"": ""}, which
    Python rejects as an unexpected keyword argument ''. Drop anything that is
    not a real identifier, then anything the function does not accept."""
    if not isinstance(args, dict):
        return {}
    args = {k: v for k, v in args.items()
            if isinstance(k, str) and k.isidentifier()}
    if fn is None:
        return args
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return args
    if any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
        return args
    return {k: v for k, v in args.items() if k in sig.parameters}


def _fn_for(name):
    return tools.REGISTRY.get(name) or plugins.REGISTRY.get(name)


def _stage(name, args):
    token = secrets.token_urlsafe(6)
    PENDING[token] = {"tool": name, "args": args}
    return token


async def _who(ref):
    """Resolve for display, so a card never hides which real person it hits."""
    c, err = await tools.resolve_chat(ref)
    if err:
        return f"{html.escape(str(ref))}  [UNRESOLVED: {html.escape(err[:120])}]"
    return f"{html.escape(c['name'])} ({c['kind']}, {c['id']})"


async def preview(name, args):
    a = {k: str(v) for k, v in args.items()}

    if name == "__approve_plugin__":
        pname = args.get("name")
        src = plugins.PROPOSALS.get(pname, "")
        body = html.escape(src[:2600]) + ("\n..." if len(src) > 2600 else "")
        return ("\u26a0\ufe0f <b>NEW TOOL</b>, written by Jarvis, not by a human.\n"
                f"Name: <b>{html.escape(str(pname))}</b>\n"
                "It has passed the import and call audit. Read it before you allow it.\n"
                f"<pre>{body}</pre>")

    if name == "disable_tool":
        return f"Disable self-built tool <b>{html.escape(a.get('name',''))}</b>"

    if name == "delete_deleted_account_chats":
        dead = await tools.find_deleted_accounts()
        names = "\n".join("  - " + html.escape(d["name"]) for d in dead[:20])
        return (f"\u26a0\ufe0f <b>IRREVERSIBLE</b>\n"
                f"Remove <b>{len(dead)} deleted-account chats</b>:\n{names}"
                + ("\n  ..." if len(dead) > 20 else ""))

    if name in tools.DESTRUCTIVE and "chat" in args:
        who = await _who(args.get("chat"))
        verb = {"delete_chat": "DELETE the chat with",
                "clear_history": "WIPE your history with",
                "leave_chat": "LEAVE",
                "block_user": "BLOCK",
                "delete_messages": "DELETE messages in"}.get(name, name)
        extra = ""
        if name == "delete_messages":
            extra = f"\nMessage ids: {args.get('message_ids')}"
            if args.get("for_everyone"):
                extra += "\nAlso deletes on THEIR side."
        return f"\u26a0\ufe0f <b>IRREVERSIBLE</b>\n{verb} <b>{who}</b>{extra}"

    if name in ("archive_chat", "mute_chat", "pin_chat", "edit_message", "send_file"):
        who = await _who(args.get("chat"))
        rest = {k: v for k, v in a.items() if k != "chat"}
        return f"{name} on <b>{who}</b>\n{html.escape(json.dumps(rest))}"

    if name == "mark_all_read":
        chats, msgs = await tools.unread_totals()
        return (f"Mark <b>ALL {chats} chats</b> as read\n"
                f"That clears {msgs} unread messages. This cannot be undone.")
    if name in ("send_message", "reply_to", "mark_read", "set_mode", "set_note"):
        a["chat"] = await _who(args.get("chat"))
    if name == "forward":
        a["from_chat"] = await _who(args.get("from_chat"))
        a["to_chat"] = await _who(args.get("to_chat"))
    if name == "send_message":
        return f"Send to <b>{a.get('chat','')}</b>:\n\n{html.escape(a.get('text',''))}"
    if name == "reply_to":
        return (f"Reply in <b>{a.get('chat','')}</b> to message "
                f"{a.get('message_id')}:\n\n{html.escape(a.get('text',''))}")
    if name == "forward":
        return (f"Forward message {a.get('message_id')} from "
                f"<b>{a.get('from_chat','')}</b> to <b>{a.get('to_chat','')}</b>")
    if name == "mark_read":
        return f"Mark <b>{a.get('chat','')}</b> as read"
    if name == "set_mode":
        return f"Set <b>{a.get('chat','')}</b> autoreply to <b>{a.get('mode')}</b>"
    if name == "set_note":
        return f"Note for <b>{a.get('chat','')}</b>:\n{html.escape(a.get('note',''))}"
    return f"{name}({html.escape(json.dumps(a))})"


META_READ = {"list_tools"}
META_WRITE = {"propose_tool", "disable_tool"}


def all_schemas():
    return SCHEMAS + plugins.schemas()


def is_write(name):
    return (name in tools.WRITE_TOOLS or name in META_WRITE
            or name in plugins.write_tools())


def clean_out(t):
    """The model leaks markdown despite being told not to, and we send plain
    text, so asterisks would render literally. Also kills en/em dashes."""
    if not t:
        return t
    t = t.replace("### ", "").replace("## ", "").replace("`", "")
    t = re.sub(r"\*+([^*\n]+?)\*+", r"\1", t)   # **bold** and *italic* both leak
    t = t.replace("**", "")
    t = t.replace("\u2014", "-").replace("\u2013", "-")
    t = re.sub(r"^\s*[*\u2022]\s+", "  - ", t, flags=re.M)
    return t.strip()


def history(owner):
    return HISTORY.setdefault(owner, [])


def reset(owner):
    HISTORY[owner] = []


async def build_system():
    """Grounding beats guessing. The model starts every turn already holding the
    real chat list with real ids, instead of probing for names it half remembers."""
    try:
        live = await tools.roster()
    except Exception as exc:
        log.warning("roster unavailable: %s", exc)
        live = "(chat list unavailable, resolve names with find_contact)"
    return (SYSTEM + "\n\nLIVE CHAT LIST (id | name | type | flags)\n"
            + live)


async def run(owner_id, text):
    """Returns {'text': str, 'pending': token or None}."""
    hist = history(owner_id)
    hist.append({"role": "user", "content": text})
    system = await build_system()

    for hop in range(MAX_HOPS):
        try:
            resp = await client.chat.completions.create(
                model=config.AGENT_MODEL,
                messages=[{"role": "system", "content": system}] + hist[-HISTORY_CAP:],
                tools=all_schemas(), tool_choice="auto",
                temperature=0.3, max_tokens=config.AGENT_MAX_TOKENS,
            )
        except Exception as exc:
            log.error("agent call failed: %s", exc)
            return {"text": f"Model call failed: {str(exc)[:200]}", "pending": None}

        msg = resp.choices[0].message
        calls = msg.tool_calls or []

        hist.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [{"id": c.id, "type": "function",
                            "function": {"name": c.function.name,
                                         "arguments": c.function.arguments}}
                           for c in calls] or None,
        })
        if not calls:
            hist[-1].pop("tool_calls", None)
            return {"text": clean_out(msg.content) or "Done.", "pending": None}

        staged = None
        for c in calls:
            name = c.function.name
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            if name == "propose_tool":
                pname, perr = plugins.propose(
                    args.get("name"), args.get("description", ""),
                    args.get("parameters", "{}"), args.get("required", "[]"),
                    args.get("code", ""), args.get("write", True))
                if perr:
                    result = f"proposal rejected: {perr}"
                elif staged is None:
                    staged = (c.id, "__approve_plugin__", {"name": pname})
                    result = "STAGED for the user to review the source. Do not call again."
                else:
                    result = "DEFERRED. One action at a time."
                hist.append({"role": "tool", "tool_call_id": c.id,
                             "content": str(result)[:6000]})
                continue

            if name == "list_tools":
                result = ("Built in: " + ", ".join(sorted(tools.REGISTRY))
                          + "\n\nSelf built:\n" + plugins.listing())
                hist.append({"role": "tool", "tool_call_id": c.id,
                             "content": str(result)[:6000]})
                continue

            if is_write(name):
                if staged is None:
                    staged = (c.id, name, clean_args(_fn_for(name), args))
                    result = "STAGED. Waiting for the user to confirm. Do not call it again."
                else:
                    result = "DEFERRED. One action at a time."
            elif name in tools.REGISTRY or name in plugins.REGISTRY:
                fn = _fn_for(name)
                try:
                    result = await fn(**clean_args(fn, args))
                except TypeError as exc:
                    result = f"bad arguments: {exc}"
                except Exception as exc:
                    log.warning("tool %s failed: %s", name, exc)
                    result = f"tool failed: {exc}"
            else:
                result = f"no such tool: {name}"

            hist.append({"role": "tool", "tool_call_id": c.id,
                         "content": str(result)[:6000]})

        if staged:
            _, name, args = staged
            token = _stage(name, args)
            return {"text": clean_out(msg.content), "pending": token}

    return {"text": "That took too many steps. Try asking for one thing.", "pending": None}


async def execute(token, owner_id, override_text=None):
    item = PENDING.pop(token, None)
    if not item:
        return False, "That action expired."
    name, args = item["tool"], dict(item["args"])

    if name == "__approve_plugin__":
        ok, msg = plugins.approve(args["name"])
        history(owner_id).append(
            {"role": "user", "content": f"[system] plugin approval: {msg}"})
        return ok, msg

    if name == "disable_tool":
        ok, msg = plugins.disable(args["name"])
        return ok, msg
    if override_text is not None:
        for key in ("text", "note"):
            if key in args:
                args[key] = override_text
                break
    fn = _fn_for(name)
    if fn is None:
        return False, f"no such tool: {name}"
    try:
        result = await fn(**clean_args(fn, args))
    except Exception as exc:
        return False, str(exc)
    history(owner_id).append(
        {"role": "user", "content": f"[system] I confirmed. Result: {result}"})
    return True, result


def get_pending(token):
    return PENDING.get(token)
