"""Supplier protocol, LetsFG adapter (REST client stubbed) and TBO stub. No network."""

import logging
from datetime import UTC, date, datetime

import pytest

from tests.ops.fakes import FakeSupplier, make_offer
from traveldesk.ops.supplier import SearchQuery, Supplier, SupplierError, SupplierUnsupported, get_supplier
from traveldesk.ops.supplier.base import TravelerDetails
from traveldesk.ops.supplier.letsfg import LetsFGSupplier, offer_from_raw
from traveldesk.ops.supplier.tbo import TBOSupplier

RAW = {
    "id": "wo_1",
    "price": 10428.0,
    "outbound": {
        "segments": [
            {
                "airline": "6E",
                "flight_no": "6E2001",
                "origin": "DEL",
                "destination": "BOM",
                "departure": "2026-12-01T06:05:00",
                "arrival": "2026-12-01T08:25:00",
            }
        ]
    },
    "conditions": {"refund_before_departure": "allowed with fee"},
}


def test_all_suppliers_satisfy_protocol():
    for s in (FakeSupplier(), LetsFGSupplier(), TBOSupplier()):
        assert isinstance(s, Supplier)


def test_get_supplier_by_name():
    assert get_supplier("letsfg").name == "letsfg"
    assert get_supplier("tbo").name == "tbo"
    with pytest.raises(ValueError):
        get_supplier("amadeus")


def test_tbo_stub_raises_not_implemented():
    tbo = TBOSupplier()
    q = SearchQuery("DEL", "BOM", date(2026, 12, 1))
    for call in (
        lambda: tbo.search(q, trace_id="t"),
        lambda: tbo.price_check(make_offer(), trace_id="t"),
        lambda: tbo.book(make_offer(), None, idempotency_key="k", trace_id="t"),
        lambda: tbo.get_booking("r", trace_id="t"),
        lambda: tbo.cancel("r", trace_id="t"),
    ):
        with pytest.raises(NotImplementedError, match="TBO"):
            call()


def test_letsfg_offer_mapping():
    o = offer_from_raw(RAW, "ws_1")
    assert (o.offer_id, o.search_id, o.route, o.fare_paise) == ("wo_1", "ws_1", "DEL-BOM", 1_042_800)
    assert o.depart_at == datetime(2026, 12, 1, 0, 35, tzinfo=UTC)
    assert o.arrive_at == datetime(2026, 12, 1, 2, 55, tzinfo=UTC)
    assert (o.carrier, o.refundable) == ("6E", True)
    assert offer_from_raw({"id": "x"}, "ws") is None  # no price


@pytest.fixture
def rest(monkeypatch):
    """Stub the LetsFG REST client the adapter imports lazily."""
    from traveldesk.clients import letsfg_rest

    calls = []

    def search_flights(**kw):
        calls.append(("search", kw))
        cheaper = {**RAW, "id": "wo_2", "price": 9800.0}
        return {"status": "ok", "search_id": "ws_2", "offers": [cheaper]}

    def book_flight(search_id, offer_id, passenger, key):
        calls.append(("book", (search_id, offer_id, key)))
        return {"status": "booking_in_progress", "booking_ref": "bk_9"}

    def get_booking(ref):
        calls.append(("get", ref))
        return {"status": "completed", "pnr": "XYZ789", "charged_amount": 10428.0}

    monkeypatch.setattr(letsfg_rest, "search_flights", search_flights)
    monkeypatch.setattr(letsfg_rest, "book_flight", book_flight)
    monkeypatch.setattr(letsfg_rest, "get_booking", get_booking)
    return calls


def test_letsfg_price_check_finds_same_flight_by_signature(rest):
    q = LetsFGSupplier().price_check(offer_from_raw(RAW, "ws_1"), trace_id="t")
    assert q.available and q.fare_paise == 980_000
    assert q.offer.offer_id == "wo_2" and q.offer.search_id == "ws_2"
    assert rest[0][1]["date_from"] == "2026-12-01"


def test_letsfg_price_check_flight_gone(rest, monkeypatch):
    from traveldesk.clients import letsfg_rest

    monkeypatch.setattr(
        letsfg_rest, "search_flights", lambda **kw: {"status": "ok", "search_id": "s", "offers": []}
    )
    q = LetsFGSupplier().price_check(offer_from_raw(RAW, "ws_1"), trace_id="t")
    assert not q.available and q.fare_paise is None


def test_letsfg_book_and_get_booking(rest):
    sup = LetsFGSupplier()
    td = TravelerDetails("U1", "A", "B", "a@example.com", "+91")
    r = sup.book(offer_from_raw(RAW, "ws_1"), td, idempotency_key="k1", trace_id="t")
    assert (r.state, r.supplier_ref) == ("pending", "bk_9")
    assert rest[-1] == ("book", ("ws_1", "wo_1", "k1"))
    g = sup.get_booking("bk_9", trace_id="t")
    assert (g.state, g.pnr, g.fare_paise) == ("ticketed", "XYZ789", 1_042_800)


def test_letsfg_transient_status_is_retryable_error(monkeypatch):
    from traveldesk.clients import letsfg_rest

    monkeypatch.setattr(
        letsfg_rest, "get_booking", lambda ref: {"status": "booking_in_progress", "transient": True}
    )
    with pytest.raises(SupplierError) as e:
        LetsFGSupplier().get_booking("bk", trace_id="t")
    assert e.value.retryable


def test_letsfg_has_no_hold_or_cancel():
    sup = LetsFGSupplier()
    with pytest.raises(SupplierUnsupported):
        sup.hold(make_offer(), None, idempotency_key="k", trace_id="t")
    with pytest.raises(SupplierUnsupported):
        sup.cancel("bk", trace_id="t")


def test_every_supplier_call_is_logged_with_trace_id(rest, caplog):
    caplog.set_level(logging.INFO, logger="traveldesk.ops.supplier")
    LetsFGSupplier().get_booking("bk_9", trace_id="trace-abc")
    lines = [r.getMessage() for r in caplog.records if r.name == "traveldesk.ops.supplier"]
    assert any("op=get_booking" in m and "trace_id=trace-abc" in m and "outcome=ok" in m for m in lines)


def _seg(dep, arr, flight_no="6E2001", origin="DEL", dest="BOM"):
    return {"airline": "6E", "flight_no": flight_no, "origin": origin, "destination": dest,
            "departure": dep, "arrival": arr}  # fmt: skip


def _stub_search(monkeypatch, offers):
    from traveldesk.clients import letsfg_rest

    seen = {}

    def search_flights(**kw):
        seen.update(kw)
        return {"status": "ok", "search_id": "ws_n", "offers": offers}

    monkeypatch.setattr(letsfg_rest, "search_flights", search_flights)
    return seen


def test_price_check_detects_retimed_flight_by_flight_number(monkeypatch):
    """LetsFG has no schedule in booking status: a re-timed flight shows up as same number, new time."""
    moved = {"id": "wo_m", "price": 10428.0,
             "outbound": {"segments": [_seg("2026-12-01T05:30:00", "2026-12-01T07:50:00")]}}  # fmt: skip
    _stub_search(monkeypatch, [moved])
    q = LetsFGSupplier().price_check(offer_from_raw(RAW, "ws_1"), trace_id="t")
    assert q.available
    assert q.depart_at == datetime(2026, 12, 1, 0, 0, tzinfo=UTC)  # 05:30 IST


def test_price_check_blank_stored_flight_number_still_matches(monkeypatch):
    from traveldesk.ops.supplier.letsfg import raw_from_signature

    # The bot's stored signature often has an empty flight number.
    stored = raw_from_signature([["6E", "", "DEL", "BOM", "2026-12-01T06:05"]])
    offer = offer_from_raw({**stored, "id": "wo_old", "price": 10428.0}, "ws_1")
    cheaper = {"id": "wo_c", "price": 9000.0,
               "outbound": {"segments": [_seg("2026-12-01T06:05:00", "2026-12-01T08:25:00")]}}  # fmt: skip
    seen = _stub_search(monkeypatch, [cheaper])
    q = LetsFGSupplier().price_check(offer, trace_id="t")
    assert q.fare_paise == 900_000
    assert (seen["origin"], seen["destination"], seen["date_from"]) == ("DEL", "BOM", "2026-12-01")


def test_price_check_one_stop_and_round_trip_search_params(monkeypatch):
    raw = {
        "id": "wo_rt",
        "price": 50000.0,
        "outbound": {"segments": [_seg("2026-12-01T02:00:00", "2026-12-01T04:30:00", "EK1", "DEL", "DXB"),
                                  _seg("2026-12-01T08:00:00", "2026-12-01T12:00:00", "EK2", "DXB", "LHR")]},
        "inbound": {"segments": [_seg("2026-12-10T21:00:00", "2026-12-11T10:00:00", "EK3", "LHR", "DEL")]},
    }  # fmt: skip
    seen = _stub_search(monkeypatch, [])
    q = LetsFGSupplier().price_check(offer_from_raw(raw, "ws"), trace_id="t")
    assert not q.available
    assert (seen["origin"], seen["destination"]) == ("DEL", "LHR")  # not the DXB connection
    assert (seen["date_from"], seen["return_from"]) == ("2026-12-01", "2026-12-10")


def test_letsfg_requote_hold_unsupported():
    with pytest.raises(SupplierUnsupported):
        LetsFGSupplier().requote_hold("bk", make_offer(), trace_id="t")
