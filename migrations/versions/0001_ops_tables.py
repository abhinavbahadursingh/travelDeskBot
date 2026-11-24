"""ops tables

Revision ID: 0001
Revises:
Create Date: 2026-11-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ops_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slack_user", sa.String(length=32), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("parsed_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "received",
                "booking",
                "booked",
                "partial",
                "failed",
                "cancelled",
                name="request_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("legacy_booking_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ops_requests")),
        sa.UniqueConstraint("legacy_booking_id", name=op.f("uq_ops_requests_legacy_booking_id")),
    )
    with op.batch_alter_table("ops_requests", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ops_requests_slack_user"), ["slack_user"], unique=False)

    op.create_table(
        "ops_travelers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slack_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("passport_no", sa.String(length=32), nullable=True),
        sa.Column("passport_exp", sa.Date(), nullable=True),
        sa.Column("meal_pref", sa.String(length=32), nullable=True),
        sa.Column("seat_pref", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ops_travelers")),
        sa.UniqueConstraint("slack_id", name=op.f("uq_ops_travelers_slack_id")),
    )
    op.create_table(
        "ops_bookings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=False),
        sa.Column("traveler_id", sa.Integer(), nullable=True),
        sa.Column("supplier", sa.String(length=32), nullable=False),
        sa.Column("supplier_ref", sa.String(length=128), nullable=False),
        sa.Column("pnr", sa.String(length=16), nullable=True),
        sa.Column("route", sa.String(length=64), nullable=False),
        sa.Column("depart_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("arrive_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fare_paise", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "held",
                "ticketed",
                "cancelled",
                "refund_pending",
                "refunded",
                name="booking_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("ticketed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refundable", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("fare_paise >= 0", name=op.f("ck_ops_bookings_fare_nonneg")),
        sa.ForeignKeyConstraint(
            ["request_id"], ["ops_requests.id"], name=op.f("fk_ops_bookings_request_id_ops_requests")
        ),
        sa.ForeignKeyConstraint(
            ["traveler_id"], ["ops_travelers.id"], name=op.f("fk_ops_bookings_traveler_id_ops_travelers")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ops_bookings")),
        sa.UniqueConstraint("supplier", "supplier_ref", name="uq_ops_bookings_supplier_ref"),
    )
    with op.batch_alter_table("ops_bookings", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ops_bookings_request_id"), ["request_id"], unique=False)
        batch_op.create_index("ix_ops_bookings_status_depart", ["status", "depart_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_ops_bookings_traveler_id"), ["traveler_id"], unique=False)

    op.create_table(
        "ops_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=True),
        sa.ForeignKeyConstraint(
            ["booking_id"], ["ops_bookings.id"], name=op.f("fk_ops_events_booking_id_ops_bookings")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ops_events")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_ops_events_dedupe_key")),
    )
    with op.batch_alter_table("ops_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ops_events_booking_id"), ["booking_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_ops_events_kind"), ["kind"], unique=False)

    op.create_table(
        "ops_ledger",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "debit",
                "refund",
                "fee",
                "credit_note",
                name="ledger_type",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("invoice_no", sa.String(length=64), nullable=True),
        sa.Column("gst_no", sa.String(length=15), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.CheckConstraint("amount_paise >= 0", name=op.f("ck_ops_ledger_amount_nonneg")),
        sa.ForeignKeyConstraint(
            ["booking_id"], ["ops_bookings.id"], name=op.f("fk_ops_ledger_booking_id_ops_bookings")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ops_ledger")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_ops_ledger_idempotency_key")),
    )
    with op.batch_alter_table("ops_ledger", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ops_ledger_booking_id"), ["booking_id"], unique=False)

    op.create_table(
        "ops_segments_snapshot",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=False),
        sa.Column("depart_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("arrive_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fare_paise", sa.BigInteger(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["booking_id"], ["ops_bookings.id"], name=op.f("fk_ops_segments_snapshot_booking_id_ops_bookings")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ops_segments_snapshot")),
    )
    with op.batch_alter_table("ops_segments_snapshot", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ops_segments_snapshot_booking_id"), ["booking_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("ops_segments_snapshot", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ops_segments_snapshot_booking_id"))

    op.drop_table("ops_segments_snapshot")
    with op.batch_alter_table("ops_ledger", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ops_ledger_booking_id"))

    op.drop_table("ops_ledger")
    with op.batch_alter_table("ops_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ops_events_kind"))
        batch_op.drop_index(batch_op.f("ix_ops_events_booking_id"))

    op.drop_table("ops_events")
    with op.batch_alter_table("ops_bookings", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ops_bookings_traveler_id"))
        batch_op.drop_index("ix_ops_bookings_status_depart")
        batch_op.drop_index(batch_op.f("ix_ops_bookings_request_id"))

    op.drop_table("ops_bookings")
    op.drop_table("ops_travelers")
    with op.batch_alter_table("ops_requests", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ops_requests_slack_user"))

    op.drop_table("ops_requests")
