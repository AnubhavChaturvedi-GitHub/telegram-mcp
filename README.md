# Telegram MCP

An MCP server that gives an AI assistant real control of a **Telegram user
account** over MTProto: read every chat, search the full history, send, forward,
organise, clean up, and write itself new tools when it hits a gap.

It is not a bot-token integration. A BotFather bot can only see messages sent to
it and replies come from the bot. This runs as **you**, so it sees your DMs,
your groups and your whole scrollback, and messages it sends come from your
account.

That power is the reason for the safety model below.

## Safety model

There is no button UI in MCP, so the human gate is an explicit argument.

- **Read tools run freely.** 10 of them.
- **Write tools refuse unless called with `confirm=true`.** 20 of them.
- **6 are marked `[DESTRUCTIVE]`** in their description: `delete_chat`,
  `clear_history`, `delete_messages`, `leave_chat`, `block_user`,
  `delete_deleted_account_chats`.
- **Bare names never resolve to strangers.** `chat_info("Zzyzx")` used to return
  a person the account had never messaged, because a bare name fell through to a
  global username lookup. Now a name is matched only against your own chats;
  reaching anyone else requires an explicit `@username`.
- **Ambiguity is reported, never guessed.** Fuzzy matching returns ranked
  candidates and refuses when two chats tie.

Relax the gate with `MCP_REQUIRE_CONFIRM=destructive` or `none` if you know what
you are doing. Default is `all`.

## Install

```bash
git clone <your-repo-url> telegram-mcp && cd telegram-mcp
python3.12 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Fill three values in `.env`:

| Variable | Where from |
|---|---|
| `TG_API_ID`, `TG_API_HASH` | https://my.telegram.org → API development tools |
| `NVIDIA_API_KEY` | https://build.nvidia.com (only for the optional bot, not the MCP server) |

Then log in once. The code arrives **inside the Telegram app**, in the blue
`Telegram` service chat, not by SMS.

```bash
./.venv/bin/python auth.py send +91XXXXXXXXXX mcp
./.venv/bin/python auth.py code 12345 mcp
```

Check everything:

```bash
./.venv/bin/python doctor.py
```

## Connect it

**Claude Code**

```bash
claude mcp add telegram -- /abs/path/to/telegram-mcp/.venv/bin/python /abs/path/to/telegram-mcp/mcp_server.py
```

**Claude Desktop** (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "telegram": {
      "command": "/abs/path/to/telegram-mcp/.venv/bin/python",
      "args": ["/abs/path/to/telegram-mcp/mcp_server.py"]
    }
  }
}
```

Any MCP client works; it speaks stdio.

## Tools

**Read, no confirmation**

| Tool | Does |
|---|---|
| `list_chats` | Recent chats with unread counts and last message |
| `unread_digest` | Everything unread, grouped, with the actual messages |
| `read_chat` | Recent messages from one chat |
| `search_messages` | Server-side search across all history, or one chat |
| `find_contact` | Which chats match a name |
| `chat_info` | id, type, autoreply mode, notes, member count |
| `list_contacts` | Saved contacts, flagging deleted accounts |
| `list_deleted_accounts` | Dead DMs from people who deleted their account |
| `get_status` | Totals |
| `list_tools` | Everything available, including self-built |

**Write, `confirm=true` required**

`send_message` · `reply_to` · `forward` · `send_file` · `edit_message` ·
`mark_read` · `mark_all_read` · `archive_chat` · `mute_chat` · `pin_chat` ·
`set_mode` · `set_note` · `propose_tool` · `disable_tool`

**Destructive, `confirm=true` required**

`delete_chat` · `clear_history` · `delete_messages` · `leave_chat` ·
`block_user` · `delete_deleted_account_chats`

## Self-extending tools

Ask for something no tool covers and the server writes one. `propose_tool` takes
the body of an `async run(client, **kwargs)` function, audits it, loads it, and
persists it to `plugins/enabled/`. It appears in `list_tools` from then on.

The audit is the boundary. A plugin may do anything with the Telegram client but
may **not** import `os`, `sys`, `subprocess`, `socket`, `urllib` or `requests`,
call `eval`, `exec`, `compile` or `open`, or touch any dunder attribute or dunder
string. That separates a new Telegram capability from a shell on your machine.

`functions`, `types`, `utils`, `asyncio`, `json`, `re` and `datetime` are already
in scope, so a plugin never needs an import.

> A self-built tool can be confidently wrong. A generated `my_profile` read the
> bio from `get_me()`, which never carries it, and returned an empty bio forever
> with no error. Approving source is not verifying behaviour. Run a new tool once
> and check the output.

## Optional: the standalone bot

`jarvis.py` is a second front end. It runs the same tools behind a private
Telegram bot, so you control your account by chatting to it, with Send / Edit /
Cancel buttons instead of `confirm=true`. It uses two NVIDIA NIM models:
`openai/gpt-oss-120b` to pick tools and `nvidia/nemotron-3-super-120b-a12b` to
compose replies in your voice. It also does unattended draft-and-approve replies.

```bash
./run.sh
```

**Run the bot or the MCP server, not both at once**, unless you log in two
sessions (`auth.py ... mcp` does exactly that). Two Telethon clients sharing one
auth key can trip `AUTH_KEY_DUPLICATED` and log the account out.

## Accuracy

`eval.py` runs 24 realistic commands and checks which tool was chosen and what
was forbidden. Currently 23–24 of 24, about 2.2s mean.

```bash
./.venv/bin/python eval.py
```

Run it after any prompt or tool change. It caught a regression that reading the
code would not: adding a grounded chat roster made "delete **all** the deleted
account chats" call `delete_chat` on **one** of them, because listing the dead
chats individually invited a single-item tool.

## Things that will bite you

1. **Bulk operations get flood-limited.** 40 `delete_dialog` calls back to back
   are mostly rejected. `_bulk()` paces, honours `FloodWaitError`, retries, and
   reports "did N of M, waited Xs, K failed". A plain `except` turns rate
   limiting into a silent partial success.
2. **Models send `{"": ""}` to zero-argument tools**, which Python rejects as
   `unexpected keyword argument ''`. Every argument is filtered through
   `inspect.signature` first.
3. **The session file is your account.** `data/*.session` is a full login. It is
   gitignored. Do not copy it anywhere.
4. **Anti-spam is real.** This is your account, not a bot. Automated replies to
   strangers get accounts limited.
5. **NVIDIA NIM model ids rot.** `meta/llama-3.3-70b-instruct` returns 410 Gone,
   and `GET /v1/models` lists models that 404 when called. Never pick one from
   documentation; pull the list and benchmark.

## License

MIT
