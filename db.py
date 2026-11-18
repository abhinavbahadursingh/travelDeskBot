"""SQLite log of every request, confirmation, booking, and AI call.

Schema is created on first connect and migrated in place; existing rows are kept.

Two tables:
  * bookings  — one row per Slack request, from search through PNR.
  * ai_usage  — one row per Claude API call, with token counts
                and estimated USD cost, linked back to a booking.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from datetime import datetime

import config

_lock = threading.Lock()

# bookings columns. If a legacy DB lacks any of these, add them.
# Older column names (approver / manager_message_ts / manager_channel /
# approval_ts) are kept for backward compatibility with existing DBs and are
# reused by the employee-confirmation flow: approver=confirming employee,
# manager_message_ts=confirmation card ts, manager_channel=employee DM channel.
_COLUMNS = [
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("created_at", "TEXT"),
    ("slack_user", "TEXT"),
    ("request", "TEXT"),
    ("status", "TEXT"),
    ("booking_ref", "TEXT"),
    ("pnr", "TEXT"),
    ("price", "REAL"),
    ("currency", "TEXT"),
    ("details", "TEXT"),
    ("request_key", "TEXT"),  # idempotency key
    ("approver", "TEXT"),  # employee who confirmed (same as slack_user)
    ("approval_ts", "TEXT"),  # ISO timestamp of confirm/cancel click
    ("payment_status", "TEXT"),  # unpaid / awaiting_payment / paid / failed
    ("manager_message_ts", "TEXT"),  # Slack ts of the confirmation card
    ("manager_channel", "TEXT"),  # DM channel the card lives in
    ("provider", "TEXT"),  # ai provider used (claude)
    ("charged_amount", "REAL"),  # actual amount charged; set only on completed + PNR
]

_AI_USAGE_COLUMNS = [
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("created_at", "TEXT"),
    ("booking_id", "INTEGER"),  # nullable; FK to bookings.id
    ("provider", "TEXT"),  # claude
    ("model", "TEXT"),  # exact model id used
    ("task_kind", "TEXT"),  # classify / pick / confirm / status / ...
    ("input_tokens", "INTEGER"),
    ("output_tokens", "INTEGER"),
    ("cache_read_tokens", "INTEGER"),
    ("cache_creation_tokens", "INTEGER"),
    ("est_cost_usd", "REAL"),
]


def _conn():
    conn = sqlite3.connect(config.DATABASE_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE TABLE IF NOT EXISTS bookings (" + ", ".join(f"{n} {t}" for n, t in _COLUMNS) + ")")
    existing = {row[1] for row in conn.execute("PRAGMA table_info(bookings)")}
    for name, coltype in _COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE bookings ADD COLUMN {name} {coltype}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_request_key ON bookings(request_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_slack_user ON bookings(slack_user)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status)")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS ai_usage (" + ", ".join(f"{n} {t}" for n, t in _AI_USAGE_COLUMNS) + ")"
    )
    existing_usage = {row[1] for row in conn.execute("PRAGMA table_info(ai_usage)")}
    for name, coltype in _AI_USAGE_COLUMNS:
        if name not in existing_usage:
            conn.execute(f"ALTER TABLE ai_usage ADD COLUMN {name} {coltype}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_booking ON ai_usage(booking_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_model ON ai_usage(model)")
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_request(slack_user: str, request: str, provider: str) -> tuple[int, str]:
    """Create a new row and return (row_id, request_key)."""
    key = secrets.token_urlsafe(10)
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO bookings (created_at, slack_user, request, status, request_key, provider, payment_status)"
            " VALUES (?,?,?,?,?,?,?)",
            (_now(), slack_user, request, "searching", key, provider, "unpaid"),
        )
        return cur.lastrowid, key


def update(row_id: int, **fields) -> None:
    if "details" in fields and not isinstance(fields["details"], str):
        fields["details"] = json.dumps(fields["details"], ensure_ascii=False)
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock, _conn() as c:
        c.execute(f"UPDATE bookings SET {cols} WHERE id=?", (*fields.values(), row_id))


def get(row_id: int) -> dict | None:
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM bookings WHERE id=?", (row_id,)).fetchone()
        return dict(row) if row else None


def by_request_key(key: str) -> dict | None:
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM bookings WHERE request_key=?", (key,)).fetchone()
        return dict(row) if row else None


def claim_decision(row_id: int, actor: str, decision: str) -> bool:
    """Atomically transition a row from 'awaiting_confirm' to the decision.

    The valid decisions are 'confirmed' or 'cancelled'. Returns True if this
    call made the change (first click wins); False if the row was already
    decided, preventing double-booking from repeated button clicks, Slack
    event retries, or race conditions.
    """
    assert decision in ("confirmed", "cancelled")
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE bookings SET status=?, approver=?, approval_ts=? "
            "WHERE id=? AND status='awaiting_confirm'",
            (decision, actor, _now(), row_id),
        )
        return cur.rowcount == 1


def claim_booking_start(row_id: int) -> bool:
    """Atomically transition a row from 'confirmed' → 'booking_started'.

    Returns True if this call made the change. Used by `agent.confirm()` to
    guarantee that even if two threads race (restart + resume, Slack retry
    after a lost ack, etc.) only one of them will actually call
    LetsFG's `book_flight`. If the row already has a `booking_ref` this
    returns False and the caller must skip booking and reuse the ref.
    """
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE bookings SET status='booking_started' "
            "WHERE id=? AND status='confirmed' AND booking_ref IS NULL",
            (row_id,),
        )
        return cur.rowcount == 1


def transition(row_id: int, from_status: str, to_status: str) -> bool:
    """Atomic status change; True only for the caller that made it (no booking_ref yet)."""
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE bookings SET status=? WHERE id=? AND status=? AND booking_ref IS NULL",
            (to_status, row_id, from_status),
        )
        return cur.rowcount == 1


# --- need_info log: which airlines / fares ask for which passenger fields ----

_DOC_WORDS = ("passport", "nationality", "document", "identity")


def _ensure_need_info(c) -> None:
    c.execute(
        "CREATE TABLE IF NOT EXISTS need_info_log ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, booking_id INTEGER, "
        "airline TEXT, airline_name TEXT, fare_type TEXT, route TEXT, domestic INTEGER, "
        "price REAL, currency TEXT, fields TEXT, http_status INTEGER, error TEXT)"
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_need_info_airline ON need_info_log(airline)")


def log_need_info(
    *,
    booking_id,
    airline,
    airline_name,
    fare_type,
    route,
    domestic,
    price,
    currency,
    fields,
    http_status=None,
    error=None,
) -> None:
    """One row per LetsFG need_info answer. Field NAMES only, never values."""
    with _lock, _conn() as c:
        _ensure_need_info(c)
        c.execute(
            "INSERT INTO need_info_log (created_at, booking_id, airline, airline_name, fare_type, "
            "route, domestic, price, currency, fields, http_status, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _now(),
                booking_id,
                airline,
                airline_name,
                fare_type,
                route,
                None if domestic is None else int(bool(domestic)),
                price,
                currency,
                json.dumps(sorted(set(map(str, fields)))),
                http_status,
                error,
            ),
        )


def passport_history(airline: str | None, fare_type: str | None = None) -> str | None:
    """Has this airline asked for passport/document fields before?

    "fare" = same airline and fare type, "airline" = same airline (other or
    unknown fare type), None = never seen."""
    if not airline:
        return None
    with _lock, _conn() as c:
        _ensure_need_info(c)
        rows = c.execute("SELECT fare_type, fields FROM need_info_log WHERE airline=?", (airline,)).fetchall()
    seen = None
    for ft, fields in rows:
        try:
            names = json.loads(fields or "[]")
        except ValueError:
            names = []
        if not any(w in str(n).lower() for n in names for w in _DOC_WORDS):
            continue
        if fare_type and ft == fare_type:
            return "fare"
        seen = "airline"
    return seen


# --- provider restrictions per traveller -------------------------------------

PASSPORT_REQUIRED = "passport_required"


def _ensure_restrictions(c) -> None:
    c.execute(
        "CREATE TABLE IF NOT EXISTS provider_restrictions ("
        "slack_user TEXT NOT NULL, provider TEXT NOT NULL, restriction TEXT NOT NULL, "
        "fields TEXT, source TEXT, created_at TEXT, updated_at TEXT, "
        "PRIMARY KEY (slack_user, provider, restriction))"
    )


def record_restriction(slack_user, provider, restriction, *, fields=(), source="") -> None:
    """Remember a provider rule for one traveller (field NAMES only)."""
    if not slack_user:
        return
    now = _now()
    with _lock, _conn() as c:
        _ensure_restrictions(c)
        c.execute(
            "INSERT INTO provider_restrictions (slack_user, provider, restriction, fields, source, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(slack_user, provider, restriction) DO UPDATE SET "
            "fields=excluded.fields, source=excluded.source, updated_at=excluded.updated_at",
            (
                slack_user,
                provider,
                restriction,
                json.dumps(list(dict.fromkeys(map(str, fields)))),
                source,
                now,
                now,
            ),
        )


def get_restriction(slack_user, provider, restriction) -> dict | None:
    if not slack_user:
        return None
    with _lock, _conn() as c:
        _ensure_restrictions(c)
        r = c.execute(
            "SELECT fields, source, created_at FROM provider_restrictions "
            "WHERE slack_user=? AND provider=? AND restriction=?",
            (slack_user, provider, restriction),
        ).fetchone()
    if not r:
        return None
    try:
        fields = json.loads(r[0] or "[]")
    except ValueError:
        fields = []
    return {"fields": fields, "source": r[1], "created_at": r[2]}


def passport_airlines_for_user(slack_user) -> set:
    """Distinct airlines whose need_info asked this traveller for passport fields."""
    return set(passport_requests_for_user(slack_user))


def passport_requests_for_user(slack_user) -> dict:
    """airline → field names it asked this traveller for (passport requests only)."""
    with _lock, _conn() as c:
        _ensure_need_info(c)
        rows = c.execute(
            "SELECT n.airline, n.fields FROM need_info_log n JOIN bookings b ON b.id = n.booking_id "
            "WHERE b.slack_user=?",
            (slack_user,),
        ).fetchall()
    out: dict = {}
    for airline, fields in rows:
        try:
            names = json.loads(fields or "[]")
        except ValueError:
            names = []
        if airline and any("passport" in str(n).lower() for n in names):
            seen = out.setdefault(airline, [])
            seen += [n for n in names if n not in seen]
    return out


def unfinished_bookings() -> list[dict]:
    """Rows with a booking_ref but no terminal status — used on startup to
    resume polling after a restart."""
    terminal = (
        "completed",
        "failed",
        "cancelled",
        "timeout",
        "approval_expired",
        "needs_attention",
        "not_started",
    )
    placeholders = ",".join("?" for _ in terminal)
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            f"SELECT * FROM bookings WHERE booking_ref IS NOT NULL AND status NOT IN ({placeholders})",
            terminal,
        ).fetchall()
        return [dict(r) for r in rows]


def recent_pending_for(slack_user: str, request: str, within_seconds: int = 300) -> dict | None:
    """Look for a near-duplicate still in flight. Used by duplicate-request protection."""
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT * FROM bookings WHERE slack_user=? AND request=? "
            "AND status IN ('searching','awaiting_confirm','confirmed','booking_started','awaiting_approval','awaiting_payment') "
            "AND datetime(created_at) > datetime('now', ?) "
            "ORDER BY id DESC LIMIT 1",
            (slack_user, request, f"-{within_seconds} seconds"),
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# AI usage tracking
# ---------------------------------------------------------------------------

# Approximate public pricing in USD per 1M tokens, for cost estimation only.
# These are intentionally conservative; update if your billing diverges.
_PRICING_PER_MTOK = {
    # Claude current-gen defaults; actual rates live at anthropic.com/pricing.
    "haiku": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
    "sonnet": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "opus": {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_write": 18.75},
}


def _price_tier(model: str) -> dict | None:
    if not model:
        return None
    m = model.lower()
    for key in ("opus", "sonnet", "haiku"):
        if key in m:
            return _PRICING_PER_MTOK[key]
    return None


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    tier = _price_tier(model)
    if not tier:
        return 0.0
    cost = (
        input_tokens * tier["input"]
        + output_tokens * tier["output"]
        + cache_read_tokens * tier["cache_read"]
        + cache_creation_tokens * tier["cache_write"]
    ) / 1_000_000.0
    return round(cost, 6)


def record_ai_usage(
    *,
    booking_id: int | None,
    provider: str,
    model: str,
    task_kind: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> int:
    """Insert one row into ai_usage. Returns the new row id."""
    est = estimate_cost_usd(
        model,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_creation_tokens,
    )
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO ai_usage ("
            "created_at, booking_id, provider, model, task_kind, "
            "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, "
            "est_cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                _now(),
                booking_id,
                provider,
                model,
                task_kind,
                int(input_tokens or 0),
                int(output_tokens or 0),
                int(cache_read_tokens or 0),
                int(cache_creation_tokens or 0),
                est,
            ),
        )
        return cur.lastrowid


def usage_for_booking(booking_id: int) -> list[dict]:
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM ai_usage WHERE booking_id=? ORDER BY id",
            (booking_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def usage_totals() -> dict:
    """Aggregate totals across all AI calls, for quick cost reporting."""
    with _lock, _conn() as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT COUNT(*) AS calls, "
            "COALESCE(SUM(input_tokens),0) AS input_tokens, "
            "COALESCE(SUM(output_tokens),0) AS output_tokens, "
            "COALESCE(SUM(est_cost_usd),0) AS est_cost_usd "
            "FROM ai_usage"
        ).fetchone()
        return dict(row)
