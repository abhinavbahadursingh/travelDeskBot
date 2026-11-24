"""Ops settings from the environment (and the project's .env), via pydantic-settings.

Same project root rule as traveldesk.config: TRAVELDESK_HOME, else the cwd.
"""

from __future__ import annotations

import os
import re
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_duration(value: object) -> timedelta:
    """'2h', '90m', '1h30m', '45' (minutes), or a timedelta → timedelta."""
    if isinstance(value, timedelta):
        return value
    s = str(value).strip().lower()
    if re.fullmatch(r"\d+", s):
        return timedelta(minutes=int(s))
    m = re.fullmatch(r"(?:(\d+)h)?\s*(?:(\d+)m)?", s)
    if not s or not m or not any(m.groups()):
        raise ValueError(f"not a duration: {value!r} (use e.g. 2h, 90m, 1h30m)")
    return timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))


def project_root() -> Path:
    return Path(os.getenv("TRAVELDESK_HOME") or Path.cwd()).resolve()


class OpsSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(project_root() / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # SQLAlchemy URL. Empty = the bot's SQLite file (DATABASE_PATH, relative to the project root),
    # so ops_* tables sit next to the legacy tables. Postgres: postgresql+psycopg://user@host/db
    ops_database_url: str = ""
    database_path: str = "traveldesk.db"

    display_tz: str = "Asia/Kolkata"
    supplier: str = "letsfg"  # letsfg | tbo

    # Slack (the scheduler posts with the bot's token; the bot itself handles button clicks)
    slack_bot_token: str = ""
    ops_slack_channel: str = ""  # e.g. #travel-ops or its channel ID (C…)

    # Phase 2 — watchdog
    watchdog_interval_hours: float = 3.0
    fare_drop_threshold_paise: int = 50_000  # ₹500
    schedule_drift_minutes: int = 20
    # Skip a booking re-checked more recently than this (restarted / overlapping runs).
    watchdog_min_recheck_minutes: int = 150

    # Phase 3 — end-of-day leakage digest
    digest_time_ist: str = "19:00"  # HH:MM, Asia/Kolkata
    refund_pending_max_days: int = 7
    company_gstin: str = ""  # invoices must be billed to this GSTIN; empty = only check it's valid
    # GST: company's state code (e.g. "27"); empty = the GSTIN's first two digits. Same state as the
    # supplier → CGST+SGST, else IGST. No COMPANY_GSTIN → GST still computed, ITC status "na".
    company_state_code: str = ""
    digest_max_items_per_bucket: int = 10  # upper bound; lowered automatically to fit Slack's 50 blocks
    dashboard_url: str = ""  # e.g. http://mac-mini.local:5173 → [Open booking] links to /?booking=<id>

    # Ledger: a booking-time (pending) debit with no ticket after this → "Charged, no ticket".
    # Also when a pending group traveller is moved to "needs action".
    ticket_timeout: timedelta = timedelta(hours=2)
    # An unpaid supplier pay link older than this → digest "Awaiting payment".
    awaiting_payment_alert: timedelta = timedelta(minutes=30)

    # Phase 4 — group booking
    # per_traveller: re-search the same flight before each traveller and book a fresh offer.
    # shared: book the one picked offer for everyone (only if the supplier allows offer reuse).
    group_offer_mode: Literal["per_traveller", "shared"] = "per_traveller"
    # A re-priced fare may rise this much (and stay ≤ MAX_PRICE) and still book without asking.
    price_tolerance_pct: float = 5.0

    @field_validator("ticket_timeout", "awaiting_payment_alert", mode="before")
    @classmethod
    def _duration(cls, v: object) -> timedelta:
        return parse_duration(v)

    @property
    def database_url(self) -> str:
        if self.ops_database_url:
            return self.ops_database_url
        path = Path(self.database_path)
        if not path.is_absolute():
            path = project_root() / path
        return f"sqlite:///{path}"


@lru_cache(maxsize=1)
def get_settings() -> OpsSettings:
    return OpsSettings()
