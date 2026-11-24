"""SQLAlchemy models for the ops layer (SQLite today, Postgres-ready).

Conventions
- Money: integer paise (BigInteger). Ledger amounts are always positive; `type` gives the direction.
- Time: timezone-aware UTC in Python. SQLite drops the tz, so UTCDateTime re-attaches it on read.
- Enums: VARCHAR + CHECK constraint (native_enum=False), so the same migration runs on both engines.
- Idempotency: unique keys on (supplier, supplier_ref), ledger.idempotency_key, events.dedupe_key,
  requests.legacy_booking_id — re-running a job or an ingest never duplicates a row.
"""

from __future__ import annotations

import enum
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, validates

from traveldesk.ops.units import utcnow

TABLE_PREFIX = "ops_"
VERSION_TABLE = "ops_alembic_version"  # Alembic's own bookkeeping, never a model

# Deterministic constraint names: Alembic needs them to alter/drop on Postgres and in SQLite batch mode.
NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Alembic filter: only ops_* tables (and their indexes/constraints) exist for migrations.

    Autogenerate can then never emit a drop for the legacy bot's tables in the same DB.
    """
    if type_ == "table":
        return bool(name) and name.startswith(TABLE_PREFIX) and name != VERSION_TABLE
    table = getattr(obj, "table", None)
    return table is None or table.name.startswith(TABLE_PREFIX)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class UTCDateTime(TypeDecorator):
    """Stores UTC; always returns an aware UTC datetime. Naive input is rejected (ambiguous)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime given to a UTC column; attach a timezone first")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _enum(cls: type[enum.Enum], name: str) -> Enum:
    return Enum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class RequestStatus(enum.StrEnum):
    RECEIVED = "received"
    BOOKING = "booking"
    BOOKED = "booked"  # every traveller booked
    PARTIAL = "partial"  # group: some booked, some need action
    FAILED = "failed"
    CANCELLED = "cancelled"


class BookingStatus(enum.StrEnum):
    AWAITING_PAYMENT = "awaiting_payment"  # supplier gave a pay link; nothing charged yet
    PENDING = "pending"  # supplier accepted (and charged); ticket not issued yet
    HELD = "held"
    TICKETED = "ticketed"
    FAILED = "failed"  # ended before ticketing (refused, link expired); any pending debit is void
    CANCELLED = "cancelled"
    REFUND_PENDING = "refund_pending"
    REFUNDED = "refunded"


# A booking only moves forward through these (a late/stale supplier answer never moves it back).
BOOKING_PROGRESS = {
    BookingStatus.AWAITING_PAYMENT: 0,
    BookingStatus.PENDING: 1,
    BookingStatus.HELD: 2,
    BookingStatus.TICKETED: 3,
}


class ItcStatus(enum.StrEnum):
    NA = "na"  # no company GSTIN, or the category isn't ITC-eligible
    PENDING = "pending"
    CLAIMED = "claimed"


class LedgerType(enum.StrEnum):
    DEBIT = "debit"
    REFUND = "refund"
    FEE = "fee"
    CREDIT_NOTE = "credit_note"


class LedgerStatus(enum.StrEnum):
    PENDING = "pending"  # charged at booking, ticket not issued yet
    CONFIRMED = "confirmed"  # ticket issued (or not a booking-time entry)
    VOID = "void"  # booking ended before ticketing; not money spent


class Traveler(Base):
    __tablename__ = "ops_travelers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slack_id: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(32))
    # Masked (last 4 only). The full number stays in the encrypted traveller_secure store.
    passport_no: Mapped[str | None] = mapped_column(String(32))
    passport_exp: Mapped[date | None] = mapped_column(Date)
    meal_pref: Mapped[str | None] = mapped_column(String(32))
    seat_pref: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    bookings: Mapped[list[Booking]] = relationship(back_populates="traveler")


class Request(Base):
    __tablename__ = "ops_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slack_user: Mapped[str] = mapped_column(String(32), index=True)
    raw_text: Mapped[str] = mapped_column(Text)
    parsed_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    status: Mapped[RequestStatus] = mapped_column(
        _enum(RequestStatus, "request_status"), default=RequestStatus.RECEIVED
    )
    trace_id: Mapped[str | None] = mapped_column(String(64))
    # Row in the legacy `bookings` table this request mirrors (NULL for ops-native requests).
    legacy_booking_id: Mapped[int | None] = mapped_column(Integer, unique=True)

    bookings: Mapped[list[Booking]] = relationship(back_populates="request")


class Booking(Base):
    __tablename__ = "ops_bookings"
    __table_args__ = (
        UniqueConstraint("supplier", "supplier_ref", name="uq_ops_bookings_supplier_ref"),
        CheckConstraint("fare_paise >= 0", name="fare_nonneg"),
        Index("ix_ops_bookings_status_depart", "status", "depart_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("ops_requests.id"), index=True)
    traveler_id: Mapped[int | None] = mapped_column(ForeignKey("ops_travelers.id"), index=True)
    supplier: Mapped[str] = mapped_column(String(32))
    supplier_ref: Mapped[str] = mapped_column(String(128))
    pnr: Mapped[str | None] = mapped_column(String(16))
    route: Mapped[str] = mapped_column(String(64))  # "DEL-BOM" or "DEL-BOM-DEL"
    depart_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    arrive_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    fare_paise: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[BookingStatus] = mapped_column(_enum(BookingStatus, "booking_status"))
    ticketed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # When `status` last changed (set automatically): ages refund_pending in the digest.
    status_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    refundable: Mapped[bool | None] = mapped_column(Boolean)  # None = supplier didn't say
    # The offer as booked (supplier.base.offer_to_json): what the watchdog re-checks.
    offer_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # awaiting_payment: the supplier's secure pay page and when it stops working (if it says).
    pay_url: Mapped[str | None] = mapped_column(Text)
    pay_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # GST (traveldesk.ops.gst). Amounts in paise. total_gst = the invoice's figure once one is
    # attached (else computed); gst_mismatch = it differs from computed_gst by more than ₹1.
    category: Mapped[str | None] = mapped_column(String(16))  # flight | hotel | service_fee
    taxable_value_paise: Mapped[int | None] = mapped_column(BigInteger)
    gst_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))  # percent
    cgst_paise: Mapped[int | None] = mapped_column(BigInteger)
    sgst_paise: Mapped[int | None] = mapped_column(BigInteger)
    igst_paise: Mapped[int | None] = mapped_column(BigInteger)
    total_gst_paise: Mapped[int | None] = mapped_column(BigInteger)
    computed_gst_paise: Mapped[int | None] = mapped_column(BigInteger)
    gst_mismatch: Mapped[bool | None] = mapped_column(Boolean)
    supplier_gstin: Mapped[str | None] = mapped_column(String(15))
    supplier_state: Mapped[str | None] = mapped_column(String(2))
    invoice_no: Mapped[str | None] = mapped_column(String(64))
    invoice_date: Mapped[date | None] = mapped_column(Date)
    invoice_url: Mapped[str | None] = mapped_column(Text)
    itc_eligible: Mapped[bool | None] = mapped_column(Boolean)
    itc_status: Mapped[ItcStatus | None] = mapped_column(_enum(ItcStatus, "itc_status"))

    @validates("status")
    def _stamp_status_change(self, _key, value):
        new = BookingStatus(value)
        if self.status != new:
            self.status_changed_at = utcnow()
        return new

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    request: Mapped[Request] = relationship(back_populates="bookings")
    traveler: Mapped[Traveler | None] = relationship(back_populates="bookings")
    snapshots: Mapped[list[SegmentSnapshot]] = relationship(back_populates="booking")
    ledger: Mapped[list[LedgerEntry]] = relationship(back_populates="booking")
    events: Mapped[list[Event]] = relationship(back_populates="booking")


class SegmentSnapshot(Base):
    __tablename__ = "ops_segments_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("ops_bookings.id"), index=True)
    depart_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    arrive_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    fare_paise: Mapped[int | None] = mapped_column(BigInteger)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    booking: Mapped[Booking] = relationship(back_populates="snapshots")


class LedgerEntry(Base):
    __tablename__ = "ops_ledger"
    __table_args__ = (CheckConstraint("amount_paise >= 0", name="amount_nonneg"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("ops_bookings.id"), index=True)
    type: Mapped[LedgerType] = mapped_column(_enum(LedgerType, "ledger_type"))
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    invoice_no: Mapped[str | None] = mapped_column(String(64))
    # Total on the supplier's invoice; the digest flags it when it differs from the debit.
    invoice_amount_paise: Mapped[int | None] = mapped_column(BigInteger)
    # GSTIN the invoice is billed to (should be the company's, for input tax credit). 15 chars.
    gst_no: Mapped[str | None] = mapped_column(String(15))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    status: Mapped[LedgerStatus] = mapped_column(
        _enum(LedgerStatus, "ledger_status"), default=LedgerStatus.CONFIRMED, server_default="confirmed"
    )

    booking: Mapped[Booking] = relationship(back_populates="ledger")


class Event(Base):
    __tablename__ = "ops_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_id: Mapped[int | None] = mapped_column(ForeignKey("ops_bookings.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # Same key twice = same event (a job re-run); the second insert is skipped.
    dedupe_key: Mapped[str | None] = mapped_column(String(160), unique=True)

    booking: Mapped[Booking | None] = relationship(back_populates="events")


class RosterStatus(enum.StrEnum):
    READY = "ready"  # passed checks, waiting for Confirm
    NEEDS_ACTION = "needs_action"  # failed a check or the booking; `reason` says why
    BOOKING = "booking"  # supplier call in flight (claimed; blocks double clicks)
    PENDING = "pending"  # supplier accepted, ticket not issued yet
    BOOKED = "booked"  # ticketed/held; booking_id set
    DRY_RUN = "dry_run"  # would have booked (DRY_RUN=true)
    PRICE_CHANGED = "price_changed"  # fresh fare too far from the shown one; waits for [Book at ₹X]
    CANCELLED = "cancelled"


class RosterMember(Base):
    """One traveller in a group request (Phase 4 split-roster booking)."""

    __tablename__ = "ops_roster"
    __table_args__ = (UniqueConstraint("request_id", "slack_id", name="uq_ops_roster_request_slack"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("ops_requests.id"), index=True)
    slack_id: Mapped[str] = mapped_column(String(32))
    traveler_id: Mapped[int | None] = mapped_column(ForeignKey("ops_travelers.id"))
    status: Mapped[RosterStatus] = mapped_column(_enum(RosterStatus, "roster_status"))
    reason: Mapped[str | None] = mapped_column(Text)  # field labels only, never document values
    attempt: Mapped[int] = mapped_column(
        Integer, default=0
    )  # bumped by [Fix & retry]; part of the booking key
    supplier_ref: Mapped[str | None] = mapped_column(String(128))  # while pending
    pay_url: Mapped[str | None] = mapped_column(Text)
    booking_id: Mapped[int | None] = mapped_column(ForeignKey("ops_bookings.id"))
    # Fare last shown to the admin for this traveller (or booked); NULL = the card's fare.
    fare_paise: Mapped[int | None] = mapped_column(BigInteger)
    # price_changed: the fresh offer behind the [Book at ₹X] button.
    quote_offer_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    request: Mapped[Request] = relationship()
    traveler: Mapped[Traveler | None] = relationship()
    booking: Mapped[Booking | None] = relationship()
