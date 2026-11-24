"""The migration, the column types and the DB-level guards."""

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import IntegrityError, StatementError

from tests.ops.conftest import alembic_cfg
from traveldesk.ops.models import Booking, BookingStatus, LedgerEntry, LedgerType, Request
from traveldesk.ops.units import IST

OPS_TABLES = {
    "ops_travelers",
    "ops_requests",
    "ops_bookings",
    "ops_segments_snapshot",
    "ops_ledger",
    "ops_events",
}


def _tables(url):
    eng = sa.create_engine(url)
    try:
        return set(sa.inspect(eng).get_table_names())
    finally:
        eng.dispose()


def test_migration_leaves_legacy_tables_alone(db_url):
    eng = sa.create_engine(db_url)
    with eng.begin() as c:  # what the legacy bot already has in the same file
        c.execute(sa.text("CREATE TABLE bookings (id INTEGER PRIMARY KEY, status TEXT)"))
        c.execute(sa.text("INSERT INTO bookings (status) VALUES ('completed')"))
    eng.dispose()

    command.upgrade(alembic_cfg(db_url), "head")
    assert OPS_TABLES <= _tables(db_url)
    assert "ops_alembic_version" in _tables(db_url)

    command.downgrade(alembic_cfg(db_url), "base")
    left = _tables(db_url)
    assert not (OPS_TABLES & left)
    assert "bookings" in left  # legacy table and its row survive
    eng = sa.create_engine(db_url)
    with eng.connect() as c:
        assert c.execute(sa.text("SELECT count(*) FROM bookings")).scalar() == 1
    eng.dispose()


def test_models_match_migration(db_url):
    """Autogenerate against the migrated DB finds nothing to add: models and migration agree."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from traveldesk.ops.models import Base, include_object

    command.upgrade(alembic_cfg(db_url), "head")
    eng = sa.create_engine(db_url)
    with eng.connect() as c:
        mc = MigrationContext.configure(c, opts={"include_object": include_object, "compare_type": False})
        diff = compare_metadata(mc, Base.metadata)
    eng.dispose()
    assert diff == []


def _request(s):
    r = Request(slack_user="U1", raw_text="DEL to BOM")
    s.add(r)
    s.flush()
    return r


def test_times_round_trip_as_aware_utc(ops_session):
    with ops_session() as s:
        r = _request(s)
        b = Booking(
            request_id=r.id,
            supplier="fake",
            supplier_ref="X1",
            route="DEL-BOM",
            fare_paise=100,
            status=BookingStatus.TICKETED,
            depart_at=datetime(2026, 12, 1, 6, 5, tzinfo=IST),
        )
        s.add(b)
        s.flush()
        bid = b.id
    with ops_session() as s:
        got = s.get(Booking, bid).depart_at
    assert got.tzinfo is not None
    assert got == datetime(2026, 12, 1, 0, 35, tzinfo=UTC)


def test_naive_datetime_is_rejected(ops_session):
    with pytest.raises(StatementError, match="naive datetime"), ops_session() as s:
        r = _request(s)
        s.add(
            Booking(
                request_id=r.id,
                supplier="fake",
                supplier_ref="X1",
                route="DEL-BOM",
                fare_paise=100,
                status=BookingStatus.HELD,
                depart_at=datetime(2026, 12, 1, 6, 5),
            )
        )
        s.flush()


def test_unknown_status_is_rejected_by_db(ops_session, db_url):
    with ops_session() as s:
        _request(s)
    eng = sa.create_engine(db_url)
    with pytest.raises(IntegrityError), eng.begin() as c:
        c.execute(
            sa.text(
                "INSERT INTO ops_bookings (request_id, supplier, supplier_ref, route, fare_paise, status,"
                " created_at, updated_at) VALUES (1, 'fake', 'X', 'DEL-BOM', 1, 'teleported', '2026-01-01', '2026-01-01')"
            )
        )
    eng.dispose()


def test_negative_ledger_amount_is_rejected_by_db(ops_session):
    with pytest.raises(IntegrityError), ops_session() as s:
        r = _request(s)
        b = Booking(
            request_id=r.id,
            supplier="fake",
            supplier_ref="X1",
            route="DEL-BOM",
            fare_paise=1,
            status="ticketed",
        )
        s.add(b)
        s.flush()
        s.add(LedgerEntry(booking_id=b.id, type=LedgerType.DEBIT, amount_paise=-5))
        s.flush()


def test_duplicate_supplier_ref_is_rejected_by_db(ops_session):
    with pytest.raises(IntegrityError), ops_session() as s:
        r = _request(s)
        for _ in range(2):
            s.add(
                Booking(
                    request_id=r.id,
                    supplier="fake",
                    supplier_ref="SAME",
                    route="DEL-BOM",
                    fare_paise=1,
                    status="ticketed",
                )
            )
        s.flush()
