"""Central settings. Everything comes from .env, nothing is hardcoded."""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

DATA = ROOT / "data"
LOGS = ROOT / "logs"
DATA.mkdir(exist_ok=True)
LOGS.mkdir(exist_ok=True)


def _s(key, default=""):
    return (os.getenv(key) or default).strip()


def _i(key, default=0):
    try:
        return int(_s(key) or default)
    except ValueError:
        return default


def _f(key, default=0.0):
    try:
        return float(_s(key) or default)
    except ValueError:
        return default


def _b(key, default=False):
    v = _s(key).lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _ids(key):
    out = set()
    for chunk in _s(key).replace(" ", "").split(","):
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            pass
    return out


# --- Telegram user account (MTProto) ---
TG_API_ID = _i("TG_API_ID")
TG_API_HASH = _s("TG_API_HASH")
TG_PHONE = _s("TG_PHONE")
SESSION = str(DATA / _s("SESSION_NAME", "jarvis_user"))
# The MCP server needs its OWN session. Two Telethon clients sharing one auth
# key can trip AUTH_KEY_DUPLICATED, which logs the account out entirely.
MCP_SESSION = str(DATA / _s("MCP_SESSION_NAME", "jarvis_mcp"))
# all | destructive | none
MCP_REQUIRE_CONFIRM = _s("MCP_REQUIRE_CONFIRM", "all").lower()

# --- Control bot ---
TG_BOT_TOKEN = _s("TG_BOT_TOKEN")
TG_OWNER_ID = _i("TG_OWNER_ID")
BOT_SESSION = str(DATA / "jarvis_bot")

# --- NVIDIA NIM ---
NVIDIA_API_KEY = _s("NVIDIA_API_KEY")
NVIDIA_BASE_URL = _s("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
NVIDIA_MODEL = _s("NVIDIA_MODEL", "nvidia/nemotron-3-super-120b-a12b")
NVIDIA_TEMPERATURE = _f("NVIDIA_TEMPERATURE", 0.6)
NVIDIA_MAX_TOKENS = _i("NVIDIA_MAX_TOKENS", 512)
# The controller that understands your commands and picks tools. Benchmarked:
# gpt-oss-120b picks the right tool where both nemotron models reached for
# search when asked to send, and it is ~10x faster.
AGENT_MODEL = _s("AGENT_MODEL", "openai/gpt-oss-120b")
AGENT_MAX_TOKENS = _i("AGENT_MAX_TOKENS", 3000)

# --- Behaviour ---
DEFAULT_MODE = _s("DEFAULT_MODE", "draft").lower()
HANDLE_PRIVATE = _b("HANDLE_PRIVATE", True)
HANDLE_GROUPS = _b("HANDLE_GROUPS", True)
BLOCKLIST = _ids("BLOCKLIST")
ALLOWLIST = _ids("ALLOWLIST")
CONTEXT_MESSAGES = _i("CONTEXT_MESSAGES", 40)
COOLDOWN_SECONDS = _i("COOLDOWN_SECONDS", 45)
TYPING_DELAY = _f("TYPING_DELAY", 2.5)

# --- Identity ---
OWNER_NAME = _s("OWNER_NAME", "the owner")
OWNER_ROLE = _s("OWNER_ROLE", "")

PERSONA_FILE = ROOT / "persona.md"
DB_PATH = DATA / "jarvis.db"

MISSING = [
    name for name, val in [
        ("TG_API_ID", TG_API_ID),
        ("TG_API_HASH", TG_API_HASH),
        ("NVIDIA_API_KEY", NVIDIA_API_KEY),
    ] if not val
]
