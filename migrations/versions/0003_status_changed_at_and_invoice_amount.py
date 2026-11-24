"""status changed at and invoice amount

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ops_bookings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True))
    # Existing rows: best known value is the last update.
    op.execute("UPDATE ops_bookings SET status_changed_at = updated_at WHERE status_changed_at IS NULL")

    with op.batch_alter_table("ops_ledger", schema=None) as batch_op:
        batch_op.add_column(sa.Column("invoice_amount_paise", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ops_ledger", schema=None) as batch_op:
        batch_op.drop_column("invoice_amount_paise")

    with op.batch_alter_table("ops_bookings", schema=None) as batch_op:
        batch_op.drop_column("status_changed_at")
