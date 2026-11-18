"""Loads settings from .env and travellers.json.

Everything is read from one project root:
  TRAVELDESK_HOME (environment variable; not read from .env) if set,
  otherwise the current working directory.
That folder holds .env, travellers.json and, by default, traveldesk.db.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(os.getenv("TRAVELDESK_HOME") or Path.cwd()).resolve()
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

# The AI provider is Claude (recorded on each request and in ai_usage).
AI_PROVIDER = "claude"
_req("ANTHROPIC_API_KEY")  # read by the anthropic SDK itself

# Intelligent model routing. Haiku handles routine/fast tasks (intent, airport
# lookup, data extraction, formatting); Sonnet handles complex itineraries,
# multi-city, ambiguous requests and recovery. Both IDs are configurable so a
# newer Anthropic release can be rolled in without a code change.
CLAUDE_HAIKU_MODEL = _opt("CLAUDE_HAIKU_MODEL", "claude-haiku-5-5")
CLAUDE_SONNET_MODEL = _opt("CLAUDE_SONNET_MODEL", "claude-sonnet-5-5")

# LetsFG Developer REST API (search + book with X-API-Key).
# LETSFG_TOKEN is still accepted as the key for existing .env files.
LETSFG_API_BASE = _opt("LETSFG_API_BASE", "https://letsfg.co/developers/api/v1").rstrip("/")
LETSFG_API_KEY = _opt("LETSFG_API_KEY") or _opt("LETSFG_TOKEN")
if not LETSFG_API_KEY:
    raise SystemExit("Missing LETSFG_API_KEY (or LETSFG_TOKEN) in .env")

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

POLL_SECONDS = int(_opt("POLL_SECONDS", "45"))
POLL_MAX_MINUTES = int(_opt("POLL_MAX_MINUTES", "30"))
# Health signal: one "heartbeat" log line this often (0 = off).
HEARTBEAT_SECONDS = int(_opt("HEARTBEAT_SECONDS", "300"))

# A relative DATABASE_PATH is relative to the project root.
DATABASE_PATH = str(BASE / _opt("DATABASE_PATH", "traveldesk.db"))

_travellers_file = BASE / "travellers.json"
if not _travellers_file.exists():
    raise SystemExit("Create travellers.json (copy travellers.example.json)")
TRAVELLERS: dict = {
    k: v for k, v in json.loads(_travellers_file.read_text()).items() if not k.startswith("_")
}
