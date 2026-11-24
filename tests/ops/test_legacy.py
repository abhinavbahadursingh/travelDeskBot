"""Legacy bot rows → ops tables, and the hook in the bot's completion path."""

import json
from datetime import UTC, datetime

from sqlalchemy import func, select

from traveldesk.ops.legacy import mirror_legacy_row
from traveldesk.ops.models import Booking, LedgerEntry, Request, Traveler

PICK = {
    "origin": "DEL",
    "destination": "BOM",
    "airports": ["DEL", "BOM"],
    "domestic": True,
    "depart_at": "2026-11-22T06:05",
    "summary": "IndiGo 6E · 22 Nov 06:05 DEL → 08:25 BOM",
    "price": 10428.0,
    "currency": "INR",
    "offer_id": "wo_1",
}
# What `details` holds after completion: the poll status, not the pick.
STATUS = {"status": "completed", "pnr": "ABC123", "charged_amount": 10500.5, "currency": "INR"}


def _row(**kw):
    row = {
        "id": 28,
        "created_at": "2026-11-20T10:00:00",
        "slack_user": "UEMP1",
        "request": "Delhi to Mumbai Sunday morning",
        "status": "completed",
        "booking_ref": "bk_1",
        "pnr": "ABC123",
        "price": 10428.0,
        "currency": "INR",
        "charged_amount": 10500.5,
        "details": json.dumps(STATUS),
    }
    row.update(kw)
    return row


def test_mirror_uses_pick_for_route_and_charged_amount_for_fare(ops_session):
    profile = {
        "given_name": "Test",
        "family_name": "User",
        "email": "t@example.com",
        "passport_number": "P9876543",
    }
    bid = mirror_legacy_row(_row(), profile, pick=PICK)
    with ops_session() as s:
        b = s.get(Booking, bid)
        assert (b.supplier, b.supplier_ref, b.pnr, b.route) == ("letsfg", "bk_1", "ABC123", "DEL-BOM")
        assert b.fare_paise == 1_050_050  # charged, not the quoted price
        assert b.depart_at == datetime(2026, 11, 22, 0, 35, tzinfo=UTC)  # 06:05 IST
        req = s.get(Request, b.request_id)
        assert req.legacy_booking_id == 28 and req.parsed_json["summary"].startswith("IndiGo")
        t = s.get(Traveler, b.traveler_id)
        assert (t.slack_id, t.name, t.passport_no) == ("UEMP1", "Test User", "••••6543")
        assert [e.amount_paise for e in s.scalars(select(LedgerEntry))] == [1_050_050]


def test_mirror_is_idempotent(ops_session):
    a = mirror_legacy_row(_row(), pick=PICK)
    b = mirror_legacy_row(_row(), pick=PICK)
    assert a == b
    with ops_session() as s:
        for model in (Request, Booking, LedgerEntry):
            assert s.scalar(select(func.count()).select_from(model)) == 1


def test_not_mirrored_until_completed_with_pnr(ops_session):
    assert mirror_legacy_row(_row(status="booking_in_progress"), pick=PICK) is None
    assert mirror_legacy_row(_row(pnr=None), pick=PICK) is None
    assert mirror_legacy_row(_row(booking_ref=None), pick=PICK) is None
    with ops_session() as s:
        assert s.scalar(select(func.count()).select_from(Request)) == 0


def test_non_inr_is_skipped(ops_session):
    assert mirror_legacy_row(_row(currency="EUR"), pick={**PICK, "currency": "EUR"}) is None


def test_international_foreign_departure_time_left_unknown(ops_session):
    pick = {**PICK, "origin": "DXB", "destination": "DEL", "airports": ["DXB", "DEL"], "domestic": False}
    bid = mirror_legacy_row(_row(), pick=pick)
    with ops_session() as s:
        assert s.get(Booking, bid).depart_at is None  # no DXB offset known; not stored wrong


def test_bot_hook_never_raises(monkeypatch, caplog):
    from traveldesk.ops import legacy
    from traveldesk.slack import app

    def boom(row_id, *, pick=None):
        raise RuntimeError("ops db locked")

    monkeypatch.setattr(legacy, "mirror_legacy_id", boom)
    app._mirror_to_ops(5, PICK)  # must not raise
    assert "ops mirror failed for booking 5" in caplog.text


def test_bot_hook_passes_the_pick(monkeypatch):
    from traveldesk.ops import legacy
    from traveldesk.slack import app

    seen = {}
    monkeypatch.setattr(
        legacy, "mirror_legacy_id", lambda row_id, *, pick=None: seen.update(id=row_id, pick=pick)
    )
    app._mirror_to_ops(7, PICK)
    assert seen == {"id": 7, "pick": PICK}


def test_mirror_stores_recheckable_offer_from_signature(ops_session):
    from traveldesk.ops.supplier.base import offer_from_json

    pick = {**PICK, "signature": [["6E", "", "DEL", "BOM", "2026-11-22T06:05"]]}
    bid = mirror_legacy_row(_row(), pick=pick)
    with ops_session() as s:
        offer = offer_from_json(s.get(Booking, bid).offer_json)
    assert offer.raw["segments"][0]["departure"] == "2026-11-22T06:05"
    assert (offer.route, offer.carrier, offer.fare_paise) == ("DEL-BOM", "6E", 1_050_050)


def test_mirror_without_signature_has_no_offer(ops_session):
    bid = mirror_legacy_row(_row(), pick=PICK)
    with ops_session() as s:
        assert s.get(Booking, bid).offer_json is None
