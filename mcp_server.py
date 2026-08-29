#!/usr/bin/env python3
"""Telegram MCP server.

Exposes a real Telegram USER account (MTProto, not the Bot API) to any MCP
client: read every chat, search the full history, send, forward, organise,
clean up, and write itself new tools.

Why this is not another bot-token integration:
  1. It is your account. It sees your DMs, your groups and your whole
     scrollback, and anything it sends comes from you, not from a bot.
  2. MCP has no button UI, so the human gate is an explicit confirm=true
     argument on every tool that writes. The calling agent has to ask you and
     pass it deliberately. Set MCP_REQUIRE_CONFIRM=destructive or none to relax.

Login (its own session, on purpose):
    python auth.py send +91XXXXXXXXXX mcp
    python auth.py code 12345 mcp
Run:
    python mcp_server.py
"""
import asyncio
import inspect
import json
import logging
import sys
from typing import Annotated

from pydantic import Field
from telethon import TelegramClient
from mcp.server import MCPServer

import config
import tools
import plugins
import schemas

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("telegram-mcp")

mcp = MCPServer(
    name="telegram",
    version="1.0.0",
    instructions=(
        "Controls a real Telegram user account over MTProto. Read tools are "
        "free. Anything that writes requires confirm=true, which you must not "
        "pass until the human has explicitly approved that specific action. "
        "Names are resolved fuzzily and ambiguity is reported rather than "
        "guessed: if a tool says AMBIGUOUS, ask which chat is meant instead of "
        "picking one. If no tool does what is needed, propose_tool writes a new "
        "one."),
)

_client = None

PY = {"string": "str", "integer": "int", "boolean": "bool",
      "number": "float", "array": "list", "object": "dict"}

CONFIRM_NOTE = ("REQUIRED. Acts on the real account. Ask the human first, then "
                "pass true. Omitting it or passing false is refused.")


def needs_confirm(name):
    policy = config.MCP_REQUIRE_CONFIRM
    if policy == "none":
        return False
    if policy == "destructive":
        return name in tools.DESTRUCTIVE
    return (name in tools.WRITE_TOOLS or name in plugins.write_tools()
            or name in {"propose_tool", "disable_tool"})


async def dispatch(name, args, confirmed):
    if needs_confirm(name) and not confirmed:
        return (f"REFUSED. '{name}' changes the real Telegram account. Ask the "
                f"human to approve, then call again with confirm=true.")

    args = {k: v for k, v in args.items() if v is not None}
    try:
        if name == "list_tools":
            return (f"Built in: {', '.join(sorted(tools.REGISTRY))}\n\n"
                    f"Self built:\n{plugins.listing()}")

        if name == "propose_tool":
            pname, err = plugins.propose(
                args.get("name"), args.get("description", ""),
                args.get("parameters", "{}"), args.get("required", "[]"),
                args.get("code", ""), args.get("write", True))
            if err:
                return f"Rejected: {err}"
            src = plugins.PROPOSALS[pname]
            ok, msg = plugins.approve(pname)
            if ok:
                _register(_spec_for_plugin(pname))
                return f"{msg}\n\nLoaded source:\n{src}"
            return msg

        if name == "disable_tool":
            target = args.get("name", "")
            ok, msg = plugins.disable(target)
            if ok:
                mcp.remove_tool(target)
            return msg

        fn = tools.REGISTRY.get(name) or plugins.REGISTRY.get(name)
        if fn is None:
            return f"No such tool: {name}"
        try:
            sig = inspect.signature(fn)
            if not any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
                args = {k: v for k, v in args.items() if k in sig.parameters}
        except (TypeError, ValueError):
            pass
        return str(await fn(**args))
    except Exception as exc:
        log.exception("tool %s failed", name)
        return f"ERROR in {name}: {exc}"


def _register(spec):
    """MCPServer builds the schema from the Python signature, so generate a
    real typed function per tool rather than handing over raw JSON schema."""
    f = spec["function"]
    name = f["name"]
    params = f.get("parameters", {}) or {}
    props = dict(params.get("properties", {}) or {})
    req = list(params.get("required", []) or [])

    gated = needs_confirm(name)
    if gated:
        # Deliberately OPTIONAL. Marking it required makes pydantic reject the
        # call with a schema error, which reads like a bug rather than a policy.
        # Optional lets dispatch() answer with a clear, actionable refusal.
        props["confirm"] = {"type": "boolean", "description": CONFIRM_NOTE}

    ordered = [k for k in props if k in req] + [k for k in props if k not in req]
    sig, passthrough = [], []
    for k in ordered:
        spec = props[k] or {}
        base = PY.get(spec.get("type"), "str")
        # Without Annotated+Field the per-parameter descriptions are dropped and
        # the calling agent has to guess what "chat" or "confirm" mean.
        desc = repr(spec.get("description", "") or k)
        if k in req:
            sig.append(f"{k}: Annotated[{base}, Field(description={desc})]")
        else:
            sig.append(f"{k}: Annotated[{base} | None, "
                       f"Field(default=None, description={desc})] = None")
        if k != "confirm":
            passthrough.append(f'"{k}": {k}')

    src = (f"async def {name}({', '.join(sig)}) -> str:\n"
           f"    return await _dispatch('{name}', {{{', '.join(passthrough)}}}, "
           f"{'bool(confirm)' if gated else 'True'})\n")
    ns = {"_dispatch": dispatch, "Annotated": Annotated, "Field": Field}
    exec(compile(src, f"<mcp:{name}>", "exec"), ns)

    desc = f.get("description", "")
    if gated:
        desc = ("[DESTRUCTIVE] " if name in tools.DESTRUCTIVE else "[WRITES] ") + desc

    mcp.add_tool(ns[name], name=name, description=desc)


def _spec_for_plugin(name):
    for s in plugins.schemas():
        if s["function"]["name"] == name:
            return s
    raise KeyError(name)


def register_all():
    n = 0
    for spec in schemas.SCHEMAS + plugins.schemas():
        try:
            _register(spec)
            n += 1
        except Exception as exc:
            log.error("could not register %s: %s", spec["function"]["name"], exc)
    return n


async def main():
    global _client
    missing = [k for k, v in [("TG_API_ID", config.TG_API_ID),
                              ("TG_API_HASH", config.TG_API_HASH)] if not v]
    if missing:
        print(f"Missing in .env: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    _client = TelegramClient(config.MCP_SESSION, config.TG_API_ID,
                             config.TG_API_HASH)
    await _client.connect()
    if not await _client.is_user_authorized():
        print("\nThe MCP session is not logged in yet. Run:\n"
              "  python auth.py send +91XXXXXXXXXX mcp\n"
              "  python auth.py code 12345 mcp\n\n"
              "It is a separate session from the bot on purpose: two Telethon "
              "clients sharing one auth key can trip AUTH_KEY_DUPLICATED, which "
              "logs the account out entirely.\n", file=sys.stderr)
        sys.exit(1)

    tools.bind(_client)
    plugins.bind(_client)
    n_plug, bad = plugins.load_all()
    n = register_all()
    me = await _client.get_me()
    log.info("telegram-mcp ready as @%s | %d tools exposed | %d self-built | "
             "confirm policy: %s", me.username or me.first_name, n, n_plug,
             config.MCP_REQUIRE_CONFIRM)
    if bad:
        log.warning("rejected plugins: %s", bad)

    await mcp.run_stdio_async()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
