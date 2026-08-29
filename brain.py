"""The reply brain, running on NVIDIA NIM.

NIM speaks the OpenAI wire format, so the official openai SDK works as-is once
base_url points at integrate.api.nvidia.com. Nothing here is Nvidia specific
beyond the two env vars, so swapping providers later is a one line change.
"""
import re
import asyncio
import logging

from openai import AsyncOpenAI

import config
import memory

log = logging.getLogger("jarvis.brain")

client = AsyncOpenAI(
    api_key=config.NVIDIA_API_KEY,
    base_url=config.NVIDIA_BASE_URL,
    timeout=90.0,
    max_retries=2,
)

# Reasoning models (deepseek-r1, nemotron thinking variants) emit a scratchpad.
# It must never reach a human.
_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_OPEN_THINK = re.compile(r"<think>.*", re.DOTALL | re.IGNORECASE)

SKIP = "[SKIP]"

BASE_RULES = f"""You are Jarvis, the private messaging assistant for {config.OWNER_NAME}.
{('Role: ' + config.OWNER_ROLE) if config.OWNER_ROLE else ''}

You are drafting a reply that will be sent FROM {config.OWNER_NAME}'s own Telegram
account. To the other person it looks like {config.OWNER_NAME} typed it. So:

1. Write in first person as {config.OWNER_NAME}. Never mention being an AI, a bot,
   an assistant, or a model. Never apologise for being automated.
2. Match the register of the conversation. Casual chat gets casual, short replies.
   Work chat gets crisp and professional. Mirror the language the other person is
   using, including Hinglish or a regional language if that is what they wrote in.
3. Length matches the incoming message. A one line question gets a one line answer.
   Do not pad, do not open with pleasantries, do not close with a signature.
4. Never invent a commitment. No dates, prices, promises, approvals, numbers or
   personal details unless they already appear in the conversation or the notes.
   If the answer needs a fact you do not have, keep it non committal and warm.
5. Output ONLY the message text. No quotes around it, no preamble, no
   explanation, no markdown headings, no "Here is the reply".
6. If a reply would be wrong, unwelcome or risky, output exactly {SKIP}.
   Use {SKIP} for: anything about money transfers, passwords, OTPs, credentials,
   legal or medical decisions, arguments, bad news, anything needing a real
   decision from {config.OWNER_NAME}, or a message that is clearly not addressed
   to you.
"""


def _persona():
    try:
        text = config.PERSONA_FILE.read_text(encoding="utf-8").strip()
        return f"\n\nHOW {config.OWNER_NAME.upper()} WRITES:\n{text}" if text else ""
    except FileNotFoundError:
        return ""


def _clean(text):
    if not text:
        return ""
    text = _THINK.sub("", text)
    text = _OPEN_THINK.sub("", text)
    text = text.strip()
    # Models love wrapping the whole reply in quotes. Undo that.
    if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    for junk in ("Reply:", "Response:", "Message:", "Jarvis:"):
        if text.lower().startswith(junk.lower()):
            text = text[len(junk):].strip()
    return text


def build_messages(chat_id, chat_title, chat_type, incoming_text, sender_name):
    system = BASE_RULES + _persona()

    notes = memory.get_notes(chat_id)
    context_bits = [f"Chat: {chat_title or 'unknown'} ({chat_type})"]
    if chat_type != "private":
        context_bits.append(
            "This is a GROUP. Several people talk here. Reply only to the message "
            "addressed to you, and keep it brief so it does not derail the room."
        )
    if notes:
        context_bits.append(f"Standing notes about this chat: {notes}")
    system += "\n\nCONTEXT:\n" + "\n".join(f"- {b}" for b in context_bits)

    msgs = [{"role": "system", "content": system}]

    for row in memory.history(chat_id):
        body = (row["text"] or "").strip()
        if not body:
            continue
        if row["outgoing"]:
            msgs.append({"role": "assistant", "content": body})
        else:
            who = row["sender_name"] or "Them"
            prefix = "" if chat_type == "private" else f"{who}: "
            msgs.append({"role": "user", "content": prefix + body})

    # The live message, always last and always explicit.
    prefix = "" if chat_type == "private" else f"{sender_name}: "
    if not msgs[-1].get("content", "").endswith(incoming_text.strip()):
        msgs.append({"role": "user", "content": prefix + incoming_text.strip()})

    return msgs


async def draft_reply(chat_id, chat_title, chat_type, incoming_text, sender_name):
    """Returns the reply text, or None when Jarvis should stay quiet."""
    msgs = build_messages(chat_id, chat_title, chat_type, incoming_text, sender_name)
    try:
        resp = await client.chat.completions.create(
            model=config.NVIDIA_MODEL,
            messages=msgs,
            temperature=config.NVIDIA_TEMPERATURE,
            max_tokens=config.NVIDIA_MAX_TOKENS,
            top_p=0.95,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.error("NIM call failed: %s", exc)
        return None

    if not resp.choices:
        return None

    text = _clean(resp.choices[0].message.content)
    if not text or SKIP in text.upper():
        return None
    return text


async def health_check():
    """Proves the key, the base url and the model name are all real."""
    resp = await client.chat.completions.create(
        model=config.NVIDIA_MODEL,
        messages=[{"role": "user", "content": "Reply with the single word: ready"}],
        max_tokens=2000,
        temperature=0.0,
    )
    return _clean(resp.choices[0].message.content)
