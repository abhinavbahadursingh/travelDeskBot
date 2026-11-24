"""group roster

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ops_roster",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=False),
        sa.Column("slack_id", sa.String(length=32), nullable=False),
        sa.Column("traveler_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "ready",
                "needs_action",
                "booking",
                "pending",
                "booked",
                "dry_run",
                "cancelled",
                name="roster_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("supplier_ref", sa.String(length=128), nullable=True),
        sa.Column("pay_url", sa.Text(), nullable=True),
        sa.Column("booking_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["booking_id"], ["ops_bookings.id"], name=op.f("fk_ops_roster_booking_id_ops_bookings")
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], ["ops_requests.id"], name=op.f("fk_ops_roster_request_id_ops_requests")
        ),
        sa.ForeignKeyConstraint(
            ["traveler_id"], ["ops_travelers.id"], name=op.f("fk_ops_roster_traveler_id_ops_travelers")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ops_roster")),
        sa.UniqueConstraint("request_id", "slack_id", name="uq_ops_roster_request_slack"),
    )
    with op.batch_alter_table("ops_roster", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ops_roster_request_id"), ["request_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("ops_roster", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ops_roster_request_id"))

    op.drop_table("ops_roster")
