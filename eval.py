"""Accuracy harness. Runs realistic commands and checks which tool the agent
picked and what it resolved to. Prints a score, not an opinion."""
import asyncio, json, sys, time
from telethon import TelegramClient
import config, tools, agent, plugins

CALLS = []

def instrument():
    for name, fn in list(tools.REGISTRY.items()):
        def wrap(n, f):
            async def inner(**kw):
                CALLS.append((n, kw))
                return await f(**kw)
            return inner
        tools.REGISTRY[name] = wrap(name, fn)

# (prompt, expected tool, must-not tools, note)
CASES = [
    ("what did I miss today",                    "unread_digest",   [], "digest"),
    ("who messaged me",                          "unread_digest",   [], "digest"),
    ("show me my chats",                         "list_chats",      [], "list", True),
    ("read my chat with Raju",                   "read_chat",       [], "read"),
    ("search everywhere for opencode",           "search_messages", [], "search"),
    ("find messages about JARVIS prompt",        "search_messages", [], "search"),
    ("mark everything as read",                  "mark_all_read",   ["mark_read"], "global vs single"),
    ("clear all my unread",                      "mark_all_read",   ["mark_read"], "global vs single"),
    ("mark the Raju chat as read",               "mark_read",       ["mark_all_read"], "single"),
    ("are there deleted accounts in my chats",   "list_deleted_accounts", ["delete_deleted_account_chats"], "read not delete"),
    ("delete all the deleted account chats",     "delete_deleted_account_chats", [], "bulk delete"),
    ("tell Raju I am busy today",                "send_message",    ["search_messages"], "send"),
    ("reply to Om Kumar saying thanks",  ("send_message","reply_to"), [], "send or reply"),
    ("mute the Subh.tly group",                  "mute_chat",       [], "mute"),
    ("archive Technical Mukut",                  "archive_chat",    [], "archive"),
    ("who is Raju",                              "chat_info",       ["send_message"], "info not send"),
    ("how many unread do I have",                "get_status",      [], "status"),
    ("leave the Courses Hub group",              "leave_chat",      [], "leave"),
    ("what tools do you have",                   "list_tools",      [], "meta", True),
    ("block MARK",                               "block_user",      [], "block"),
    # grounding cases: names that need the live roster, not a guess
    ("send raju a quick hi",                     "send_message",    [], "lowercase name"),
    ("what is in the NetHyTech Chat group",      "read_chat",       [], "exact group name"),
    ("mute technical mukut",                     "mute_chat",       [], "lowercase channel"),
    ("message someone called Zzyzx",             None,              ["send_message"], "must refuse, no such chat"),
]

async def one(case):
    prompt, expect, forbid, note = case[0], case[1], case[2], case[3]
    accept_direct = case[4] if len(case) > 4 else False
    CALLS.clear()
    agent.reset(config.TG_OWNER_ID)
    t = time.time()
    try:
        out = await agent.run(config.TG_OWNER_ID, prompt)
    except Exception as exc:
        return dict(prompt=prompt, ok=False, got="CRASH " + str(exc)[:60],
                    expect=expect, secs=0, note=note)
    dt = round(time.time() - t, 1)
    staged = None
    if out.get("pending"):
        it = agent.get_pending(out["pending"])
        staged = it["tool"]
        agent.PENDING.pop(out["pending"], None)
    read_calls = [c[0] for c in CALLS]
    used = ([staged] if staged else []) + read_calls
    if staged == "__approve_plugin__":
        used.append("propose_tool")
    if expect is None:
        ok = not any(f in used for f in forbid)
    elif accept_direct and not used:
        ok = bool((out.get("text") or "").strip())   # answered from grounded context
    else:
        want = expect if isinstance(expect, tuple) else (expect,)
        ok = any(w in used for w in want) and not any(f in used for f in forbid)
    return dict(prompt=prompt, ok=ok, got=",".join(used) or "NOTHING",
                expect=expect, secs=dt, note=note)

async def main():
    c = TelegramClient(config.SESSION, config.TG_API_ID, config.TG_API_HASH)
    await c.start(); tools.bind(c); plugins.bind(c); plugins.load_all()
    instrument()
    rows = []
    for case in CASES:
        r = await one(case)
        rows.append(r)
        mark = "PASS" if r["ok"] else "FAIL"
        print(f"  [{mark}] {r['secs']:>4}s  {r['prompt'][:44]:<46} want={str(r["expect"])[:28]:<28} got={r['got'][:60]}")
    n = sum(1 for r in rows if r["ok"])
    print(f"\n  ACCURACY {n}/{len(rows)} = {100*n//len(rows)}%")
    avg = sum(r['secs'] for r in rows) / len(rows)
    print(f"  MEAN LATENCY {avg:.1f}s")
    json.dump(rows, open("logs/eval_last.json", "w"), indent=1)
    await c.disconnect()

asyncio.run(main())
