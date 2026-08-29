"""The context layer. Every message Jarvis ever sees lands here, so a reply
knows the last N turns of that exact chat instead of answering blind."""
import sqlite3
import threading
import time
import secrets

import config

_lock = threading.Lock()
_conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    msg_id      INTEGER,
    chat_title  TEXT,
    chat_type   TEXT,
    sender_id   INTEGER,
    sender_name TEXT,
    outgoing    INTEGER DEFAULT 0,
    text        TEXT,
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_chat ON messages(chat_id, ts);

CREATE TABLE IF NOT EXISTS peers (
    chat_id       INTEGER PRIMARY KEY,
    title         TEXT,
    chat_type     TEXT,
    mode          TEXT,
    notes         TEXT DEFAULT '',
    last_reply_ts REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pending (
    token       TEXT PRIMARY KEY,
    chat_id     INTEGER NOT NULL,
    reply_to    INTEGER,
    chat_title  TEXT,
    incoming    TEXT,
    draft       TEXT,
    status      TEXT DEFAULT 'open',
    created_ts  REAL NOT NULL
);
"""

with _lock:
    _conn.executescript(SCHEMA)
    _conn.commit()


# ---------------- messages ----------------

def log_message(chat_id, msg_id, chat_title, chat_type, sender_id,
                sender_name, outgoing, text):
    with _lock:
        _conn.execute(
            "INSERT INTO messages (chat_id, msg_id, chat_title, chat_type,"
            " sender_id, sender_name, outgoing, text, ts)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (chat_id, msg_id, chat_title, chat_type, sender_id,
             sender_name, 1 if outgoing else 0, text or "", time.time()),
        )
        _conn.commit()


def history(chat_id, limit=None):
    """Oldest to newest, so it reads like a transcript."""
    limit = limit or config.CONTEXT_MESSAGES
    with _lock:
        rows = _conn.execute(
            "SELECT sender_name, outgoing, text, ts FROM messages"
            " WHERE chat_id=? ORDER BY ts DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return list(reversed([dict(r) for r in rows]))


def message_count(chat_id):
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return row["c"] if row else 0


# ---------------- peers ----------------

def touch_peer(chat_id, title, chat_type):
    with _lock:
        _conn.execute(
            "INSERT INTO peers (chat_id, title, chat_type, mode) VALUES (?,?,?,?)"
            " ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,"
            " chat_type=excluded.chat_type",
            (chat_id, title, chat_type, config.DEFAULT_MODE),
        )
        _conn.commit()


def get_mode(chat_id):
    with _lock:
        row = _conn.execute(
            "SELECT mode FROM peers WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return (row["mode"] if row and row["mode"] else config.DEFAULT_MODE)


def set_mode(chat_id, mode):
    with _lock:
        _conn.execute(
            "INSERT INTO peers (chat_id, mode) VALUES (?,?)"
            " ON CONFLICT(chat_id) DO UPDATE SET mode=excluded.mode",
            (chat_id, mode),
        )
        _conn.commit()


def get_notes(chat_id):
    with _lock:
        row = _conn.execute(
            "SELECT notes FROM peers WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return (row["notes"] if row else "") or ""


def set_notes(chat_id, notes):
    with _lock:
        _conn.execute(
            "INSERT INTO peers (chat_id, notes) VALUES (?,?)"
            " ON CONFLICT(chat_id) DO UPDATE SET notes=excluded.notes",
            (chat_id, notes),
        )
        _conn.commit()


def cooling_down(chat_id):
    with _lock:
        row = _conn.execute(
            "SELECT last_reply_ts FROM peers WHERE chat_id=?", (chat_id,)
        ).fetchone()
    last = row["last_reply_ts"] if row else 0
    return (time.time() - (last or 0)) < config.COOLDOWN_SECONDS


def mark_replied(chat_id):
    with _lock:
        _conn.execute(
            "INSERT INTO peers (chat_id, last_reply_ts) VALUES (?,?)"
            " ON CONFLICT(chat_id) DO UPDATE SET last_reply_ts=excluded.last_reply_ts",
            (chat_id, time.time()),
        )
        _conn.commit()


def all_peers():
    with _lock:
        rows = _conn.execute(
            "SELECT chat_id, title, chat_type, mode FROM peers ORDER BY title"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------- pending approvals ----------------

def add_pending(chat_id, reply_to, chat_title, incoming, draft):
    token = secrets.token_urlsafe(6)
    with _lock:
        _conn.execute(
            "INSERT INTO pending (token, chat_id, reply_to, chat_title,"
            " incoming, draft, created_ts) VALUES (?,?,?,?,?,?,?)",
            (token, chat_id, reply_to, chat_title, incoming, draft, time.time()),
        )
        _conn.commit()
    return token


def get_pending(token):
    with _lock:
        row = _conn.execute(
            "SELECT * FROM pending WHERE token=?", (token,)
        ).fetchone()
    return dict(row) if row else None


def close_pending(token, status):
    with _lock:
        _conn.execute(
            "UPDATE pending SET status=? WHERE token=?", (status, token)
        )
        _conn.commit()


def update_draft(token, draft):
    with _lock:
        _conn.execute(
            "UPDATE pending SET draft=? WHERE token=?", (draft, token)
        )
        _conn.commit()
