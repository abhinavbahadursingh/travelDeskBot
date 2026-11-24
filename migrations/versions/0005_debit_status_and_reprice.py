"""debit status, awaiting payment, re-pricing

- ops_ledger.status: pending | confirmed | void (existing rows: confirmed)
- ops_bookings.status gains awaiting_payment, pending, failed; + pay_url, pay_expires_at
- ops_roster.status gains price_changed; + fare_paise, quote_offer_json

Status values are VARCHAR + named CHECK constraints, so widening them = drop + recreate the
constraint (works the same on SQLite batch mode and Postgres).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BOOKING_OLD = ("held", "ticketed", "cancelled", "refund_pending", "refunded")
BOOKING_NEW = (
    "awaiting_payment",
    "pending",
    "held",
    "ticketed",
    "failed",
    "cancelled",
    "refund_pending",
    "refunded",
)
ROSTER_OLD = ("ready", "needs_action", "booking", "pending", "booked", "dry_run", "cancelled")
ROSTER_NEW = (
    "ready",
    "needs_action",
    "booking",
    "pending",
    "booked",
    "dry_run",
    "price_changed",
    "cancelled",
)
LEDGER_STATUS = ("pending", "confirmed", "void")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _swap_check(table: str, name: str, column: str, values: Sequence[str]) -> None:
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.drop_constraint(op.f(name), type_="check")
        batch_op.create_check_constraint(op.f(name), _in(column, values))


def upgrade() -> None:
    with op.batch_alter_table("ops_ledger", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("status", sa.String(length=32), nullable=False, server_default="confirmed")
        )
        batch_op.create_check_constraint(op.f("ck_ops_ledger_ledger_status"), _in("status", LEDGER_STATUS))

    with op.batch_alter_table("ops_bookings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("pay_url", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("pay_expires_at", sa.DateTime(timezone=True), nullable=True))
    _swap_check("ops_bookings", "ck_ops_bookings_booking_status", "status", BOOKING_NEW)

    with op.batch_alter_table("ops_roster", schema=None) as batch_op:
        batch_op.add_column(sa.Column("fare_paise", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("quote_offer_json", sa.JSON(), nullable=True))
    _swap_check("ops_roster", "ck_ops_roster_roster_status", "status", ROSTER_NEW)


def downgrade() -> None:
    # Rows in the new states can't be represented in the old schema: map them to the nearest old one.
    op.execute("UPDATE ops_roster SET status = 'needs_action' WHERE status = 'price_changed'")
    op.execute(
        "UPDATE ops_bookings SET status = 'cancelled' WHERE status IN ('awaiting_payment', 'pending', 'failed')"
    )
    _swap_check("ops_roster", "ck_ops_roster_roster_status", "status", ROSTER_OLD)
    with op.batch_alter_table("ops_roster", schema=None) as batch_op:
        batch_op.drop_column("quote_offer_json")
        batch_op.drop_column("fare_paise")

    _swap_check("ops_bookings", "ck_ops_bookings_booking_status", "status", BOOKING_OLD)
    with op.batch_alter_table("ops_bookings", schema=None) as batch_op:
        batch_op.drop_column("pay_expires_at")
        batch_op.drop_column("pay_url")

    with op.batch_alter_table("ops_ledger", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_ops_ledger_ledger_status"), type_="check")
        batch_op.drop_column("status")
