"""Loads settings from .env and travellers.json."""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")


def _req(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing {name} in .env")
    return value


def _opt(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


SLACK_BOT_TOKEN = _req("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = _req("SLACK_APP_TOKEN")

# Claude is the primary provider. Groq remains available as an optional
# fallback for testing or if the Anthropic API is unreachable.
AI_PROVIDER = _opt("AI_PROVIDER", "claude").lower()
if AI_PROVIDER not in ("claude", "groq"):
    raise SystemExit(f"AI_PROVIDER must be 'claude' or 'groq', got {AI_PROVIDER!r}")

# Claude API key only required when Claude is the primary provider.
if AI_PROVIDER == "claude":
    _req("ANTHROPIC_API_KEY")  # read by the anthropic SDK itself

# Intelligent model routing. Haiku handles routine/fast tasks (intent, airport
# lookup, data extraction, formatting); Sonnet handles complex itineraries,
# multi-city, ambiguous requests and recovery. Both IDs are configurable so a
# newer Anthropic release can be rolled in without a code change.
CLAUDE_HAIKU_MODEL = _opt("CLAUDE_HAIKU_MODEL", "claude-haiku-5-5")
CLAUDE_SONNET_MODEL = _opt("CLAUDE_SONNET_MODEL", "claude-sonnet-5-5")
# Legacy single-model env var still accepted; defaults to the Sonnet id.
CLAUDE_MODEL = _opt("CLAUDE_MODEL", CLAUDE_SONNET_MODEL)
# Toggle automatic fallback Sonnet → Haiku (or vice-versa) on transient errors.
CLAUDE_FALLBACK = _opt("CLAUDE_FALLBACK", "true").lower() in ("1", "true", "yes")
MCP_BETA = _opt("MCP_BETA", "mcp-client-2025-11-20")

# Groq (optional fallback provider).
GROQ_API_KEY = _req("GROQ_API_KEY") if AI_PROVIDER == "groq" else _opt("GROQ_API_KEY")
GROQ_MODEL = _opt("GROQ_MODEL", "llama-3.3-70b-versatile")

LETSFG_MCP_URL = _req("LETSFG_MCP_URL")
LETSFG_TOKEN = _req("LETSFG_TOKEN")

# "rest" = Developer REST API (search + book with X-API-Key);
# "mcp"  = MCP traveller-card flow (requires an MCP-scoped token).
LETSFG_BACKEND = _opt("LETSFG_BACKEND", "rest").lower()
if LETSFG_BACKEND not in ("rest", "mcp"):
    raise SystemExit(f"LETSFG_BACKEND must be 'rest' or 'mcp', got {LETSFG_BACKEND!r}")
LETSFG_API_BASE = _opt("LETSFG_API_BASE", "https://letsfg.co/developers/api/v1").rstrip("/")
LETSFG_API_KEY = _opt("LETSFG_API_KEY") or LETSFG_TOKEN

CURRENCY = _opt("CURRENCY", "INR")
BOOKING_RULE = _opt(
    "BOOKING_RULE",
    "Pick the cheapest nonstop economy flight inside the requested time window.",
)
_cap = _opt("MAX_PRICE")
MAX_PRICE = float(_cap) if _cap else None
DRY_RUN = _opt("DRY_RUN", "true").lower() in ("1", "true", "yes")

# Slack member IDs (comma-separated) told when a booking needs a human:
# e.g. LetsFG still refuses after passport details were added.
TRAVEL_ADMINS = [u.strip() for u in _opt("TRAVEL_ADMIN_SLACK_IDS").split(",") if u.strip()]
# "I don't have a passport": alternatives departing within ± this many hours.
ALT_WINDOW_HOURS = float(_opt("ALT_WINDOW_HOURS", "3"))

POLL_SECONDS =int(_opt("POLL_SECONDS", "45"))
POLL_MAX_MINUTES = int(_opt("POLL_MAX_MINUTES", "30"))

DATABASE_PATH = _opt("DATABASE_PATH", str(BASE / "traveldesk.db"))

_travellers_file = BASE / "travellers.json"
if not _travellers_file.exists():
    raise SystemExit("Create travellers.json (copy travellers.example.json)")
TRAVELLERS: dict = {
    k: v
    for k, v in json.loads(_travellers_file.read_text()).items()
    if not k.startswith("_")
}
