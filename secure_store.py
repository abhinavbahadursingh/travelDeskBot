"""Encrypted storage for sensitive traveller fields (DOB, passport, etc.).

* Fernet (AES-128-CBC + HMAC-SHA256) with the key in TRAVELDESK_DATA_KEY.
  Without a key the store refuses to read or write (fails closed).
* One encrypted blob per Slack user in the `traveller_secure` table.
* Access is per user only: there is no list/export function. The bot reads
  a record only to book for that same user, and writes it only from that
  user's own Slack modal submission.
* The database file is restricted to owner-only permissions on every write.
* Nothing here logs field values.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken

import config
import db

log = logging.getLogger("secure_store")

ALLOWED_FIELDS = (
    # basic profile (entered by the traveller in Slack)
    "given_name",
    "family_name",
    "phone",
    "date_of_birth",
    "gender",
    # type of ID carried on domestic trips — the type only, never a number
    "id_type",
    # passport profile
    "nationality",
    "passport_number",
    "passport_country",
    "passport_expiry",
    "passport_issue",
    # only collected when LetsFG asks for them
    "address_line1",
    "address_city",
    "address_postal",
    "address_country",
    "phone_country",
)


class SecureStoreUnavailable(RuntimeError):
    pass


def is_configured() -> bool:
    try:
        _fernet()
        return True
    except SecureStoreUnavailable:
        return False


def _fernet() -> Fernet:
    key = os.getenv("TRAVELDESK_DATA_KEY", "").strip()
    if not key:
        raise SecureStoreUnavailable("TRAVELDESK_DATA_KEY is not set")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as e:
        raise SecureStoreUnavailable("TRAVELDESK_DATA_KEY is not a valid Fernet key") from e


def _ensure_table(c) -> None:
    c.execute(
        "CREATE TABLE IF NOT EXISTS traveller_secure ("
        "slack_user TEXT PRIMARY KEY, ciphertext BLOB NOT NULL, updated_at TEXT)"
    )


def _restrict_db_permissions() -> None:
    for suffix in ("", "-wal", "-shm"):
        path = f"{config.DATABASE_PATH}{suffix}"
        try:
            if os.path.exists(path):
                os.chmod(path, 0o600)
        except OSError as e:
            log.warning("could not restrict %s permissions: %s", suffix or "db", type(e).__name__)


def load(slack_user: str) -> dict:
    f = _fernet()
    with db._lock, db._conn() as c:
        _ensure_table(c)
        row = c.execute(
            "SELECT ciphertext FROM traveller_secure WHERE slack_user=?", (slack_user,)
        ).fetchone()
    if not row:
        return {}
    try:
        data = json.loads(f.decrypt(row[0]).decode())
    except (InvalidToken, ValueError):
        log.warning("secure record for %s could not be decrypted (key changed?)", slack_user)
        return {}
    return {k: v for k, v in data.items() if k in ALLOWED_FIELDS}


def save(slack_user: str, fields: dict) -> None:
    """Merge `fields` into the user's encrypted record."""
    f = _fernet()
    clean = {k: v for k, v in fields.items() if k in ALLOWED_FIELDS and v not in (None, "")}
    merged = {**load(slack_user), **clean}
    token = f.encrypt(json.dumps(merged).encode())
    with db._lock, db._conn() as c:
        _ensure_table(c)
        c.execute(
            "INSERT INTO traveller_secure (slack_user, ciphertext, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(slack_user) DO UPDATE SET ciphertext=excluded.ciphertext, "
            "updated_at=excluded.updated_at",
            (slack_user, token, datetime.now().isoformat(timespec="seconds")),
        )
    _restrict_db_permissions()
    log.info("secure traveller record updated for %s (fields: %s)", slack_user, ", ".join(sorted(clean)))


def delete(slack_user: str) -> None:
    with db._lock, db._conn() as c:
        _ensure_table(c)
        c.execute("DELETE FROM traveller_secure WHERE slack_user=?", (slack_user,))
