"""Tool definitions, shared by the NIM agent and the MCP server.

Kept out of agent.py on purpose: the MCP server exposes these to any MCP client
and must not need an LLM key it never uses.
"""


def _p(name, typ, desc, required=False):
    return name, {"type": typ, "description": desc}, required


def _fn(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


SCHEMAS = [
    _fn("list_chats", "List recent Telegram chats with unread counts and the last message. Use this to see what is going on.",
        {"limit": {"type": "integer", "description": "how many, default 20"},
         "unread_only": {"type": "boolean", "description": "only chats with unread messages"}}, []),
    _fn("unread_digest", "Everything unread right now, grouped by chat, with the actual messages. Use for 'what did I miss', 'who messaged me', 'catch me up'.",
        {"limit": {"type": "integer", "description": "max chats, default 15"}}, []),
    _fn("read_chat", "Read recent messages from one chat. Use before replying so you know the context.",
        {"chat": {"type": "string", "description": "chat name or numeric id"},
         "limit": {"type": "integer", "description": "how many messages, default 25"}}, ["chat"]),
    _fn("search_messages", "Search the user's Telegram history by text. Leave chat empty to search everywhere.",
        {"query": {"type": "string", "description": "words to find"},
         "chat": {"type": "string", "description": "optional, restrict to one chat"},
         "days": {"type": "integer", "description": "optional, only the last N days"},
         "limit": {"type": "integer", "description": "max hits, default 25"}}, ["query"]),
    _fn("find_contact", "Find which chats match a person's name. Use when a name is ambiguous.",
        {"name": {"type": "string", "description": "person or group name"}}, ["name"]),
    _fn("chat_info", "Who or what a chat is: id, type, autoreply mode, standing notes, member count. Use for 'who is X', 'what is X', 'tell me about X'.",
        {"chat": {"type": "string", "description": "chat name or id"}}, ["chat"]),
    _fn("get_status", "Overall status: chat count, total unread, autoreply modes.", {}, []),

    _fn("send_message", "Send a NEW message to a chat, as the user. Needs the user's confirmation.",
        {"chat": {"type": "string", "description": "chat name or id"},
         "text": {"type": "string", "description": "the exact message to send"}}, ["chat", "text"]),
    _fn("reply_to", "Reply to one specific message by its id. Needs confirmation.",
        {"chat": {"type": "string"}, "message_id": {"type": "integer"},
         "text": {"type": "string"}}, ["chat", "message_id", "text"]),
    _fn("forward", "Forward a message from one chat to another. Needs confirmation.",
        {"from_chat": {"type": "string"}, "message_id": {"type": "integer"},
         "to_chat": {"type": "string"}}, ["from_chat", "message_id", "to_chat"]),
    _fn("mark_read", "Mark a chat as read. Needs confirmation.",
        {"chat": {"type": "string"}}, ["chat"]),
    _fn("mark_all_read", "Mark EVERY chat as read at once. Use this whenever the user says mark everything, mark all, or clear my unread. Do NOT use mark_read for that. Needs confirmation.",
        {"unread_only": {"type": "boolean", "description": "only chats that actually have unread, default true"}}, []),
    _fn("set_mode", "Set the autoreply mode for a chat: draft, auto or off. Needs confirmation.",
        {"chat": {"type": "string"}, "mode": {"type": "string",
         "description": "draft, auto or off"}}, ["chat", "mode"]),
    _fn("list_deleted_accounts", "List DM chats belonging to people who deleted their Telegram account. Read only, safe.", {}, []),
    _fn("delete_deleted_account_chats", "Remove EVERY deleted-account chat from the chat list in one go. Needs confirmation.", {}, []),
    _fn("delete_chat", "Remove ONE named chat from the chat list. Never use this to clear deleted accounts in bulk, use delete_deleted_account_chats for that. Irreversible. Needs confirmation.",
        {"chat": {"type": "string"}}, ["chat"]),
    _fn("clear_history", "Clear your copy of a chat's history, keeping the chat. Irreversible. Needs confirmation.",
        {"chat": {"type": "string"}}, ["chat"]),
    _fn("leave_chat", "Leave a group or channel. Needs confirmation.",
        {"chat": {"type": "string"}}, ["chat"]),
    _fn("delete_messages", "Delete specific messages by id. Needs confirmation.",
        {"chat": {"type": "string"}, "message_ids": {"type": "array", "items": {"type": "integer"}},
         "for_everyone": {"type": "boolean", "description": "also delete on their side"}},
        ["chat", "message_ids"]),
    _fn("edit_message", "Edit one of your own sent messages. Needs confirmation.",
        {"chat": {"type": "string"}, "message_id": {"type": "integer"},
         "text": {"type": "string"}}, ["chat", "message_id", "text"]),
    _fn("archive_chat", "Archive or unarchive a chat. Needs confirmation.",
        {"chat": {"type": "string"}, "archive": {"type": "boolean"}}, ["chat"]),
    _fn("mute_chat", "Mute or unmute a chat. Needs confirmation.",
        {"chat": {"type": "string"}, "mute": {"type": "boolean"}}, ["chat"]),
    _fn("pin_chat", "Pin or unpin a chat. Needs confirmation.",
        {"chat": {"type": "string"}, "pin": {"type": "boolean"}}, ["chat"]),
    _fn("block_user", "Block or unblock a person. Needs confirmation.",
        {"chat": {"type": "string"}, "block": {"type": "boolean"}}, ["chat"]),
    _fn("list_contacts", "List saved contacts, flagging deleted accounts. Read only.",
        {"limit": {"type": "integer"}}, []),
    _fn("send_file", "Send a file from a local path. Needs confirmation.",
        {"chat": {"type": "string"}, "path": {"type": "string"},
         "caption": {"type": "string"}}, ["chat", "path"]),

    _fn("list_tools", "List every tool available, including self-built ones. Use this when unsure whether a capability exists.", {}, []),
    _fn("propose_tool", "Write yourself a NEW permanent tool when no existing tool can do what the user asked. The user reviews the source and approves before it loads. Write the body of an async run(client, **kwargs) function; you get the Telethon client and must return a string. Already in scope, do NOT import them: functions, types, utils (telethon), asyncio, json, re, datetime. You may not import os, sys, subprocess, socket, urllib or requests, and may not call eval, exec or open.",
        {"name": {"type": "string", "description": "snake_case tool name"},
         "description": {"type": "string", "description": "what it does, for the tool list"},
         "parameters": {"type": "string", "description": "JSON object of JSON-schema properties"},
         "required": {"type": "string", "description": "JSON array of required parameter names"},
         "code": {"type": "string", "description": "the body of run(), or a full module with META and async def run"},
         "write": {"type": "boolean", "description": "true if it changes anything"}},
        ["name", "description", "code"]),
    _fn("disable_tool", "Turn off a self-built tool. Needs confirmation.",
        {"name": {"type": "string"}}, ["name"]),
    _fn("set_note", "Save a standing note about a chat, used when drafting replies there. Needs confirmation.",
        {"chat": {"type": "string"}, "note": {"type": "string"}}, ["chat", "note"]),
]


WRITE_HINT = (
    "This changes something on the real account. "
    "Pass confirm=true only after the human has agreed."
)
