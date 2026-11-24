"""GST breakdown on ops_bookings

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = [
    ("category", sa.String(length=16)),
    ("taxable_value_paise", sa.BigInteger()),
    ("gst_rate", sa.Numeric(5, 2)),
    ("cgst_paise", sa.BigInteger()),
    ("sgst_paise", sa.BigInteger()),
    ("igst_paise", sa.BigInteger()),
    ("total_gst_paise", sa.BigInteger()),
    ("computed_gst_paise", sa.BigInteger()),
    ("gst_mismatch", sa.Boolean()),
    ("supplier_gstin", sa.String(length=15)),
    ("supplier_state", sa.String(length=2)),
    ("invoice_no", sa.String(length=64)),
    ("invoice_date", sa.Date()),
    ("invoice_url", sa.Text()),
    ("itc_eligible", sa.Boolean()),
    ("itc_status", sa.String(length=32)),
]


def upgrade() -> None:
    with op.batch_alter_table("ops_bookings", schema=None) as batch_op:
        for name, type_ in COLUMNS:
            batch_op.add_column(sa.Column(name, type_, nullable=True))
        batch_op.create_check_constraint(
            op.f("ck_ops_bookings_itc_status"), "itc_status IN ('na', 'pending', 'claimed')"
        )


def downgrade() -> None:
    with op.batch_alter_table("ops_bookings", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_ops_bookings_itc_status"), type_="check")
        for name, _ in reversed(COLUMNS):
            batch_op.drop_column(name)
