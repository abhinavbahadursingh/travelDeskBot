"""LetsFG need_info: parse fields, auto-retry with saved data, ask the
traveller for exactly what's missing, re-price, alternatives, escalation.

Synthetic data only. LetsFG HTTP and Slack are stubbed; no booking or
payment request leaves the machine.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app as bot_app
import db
import letsfg_booking
import letsfg_rest
import secure_store
import traveller_profile as tp

PASSPORT_NO = "Z9081726"
BASIC = {
    "given_name": "Raj",
    "family_name": "Gupta",
    "email": "raj@example.com",
    "phone": "+919800000000",
    "date_of_birth": "1990-01-02",
    "gender": "m",
}
PASSPORT = {
    "nationality": "IN",
    "passport_number": PASSPORT_NO,
    "passport_country": "IN",
    "passport_expiry": "2032-05-01",
    "passport_issue": "2022-05-02",
}
DOC_FIELDS = ["nationality", "passport_number", "passport_country", "passport_issue", "passport_expiry"]


def seg(o, d, dep, airline="QP", no="1101"):
    return {
        "airline": airline,
        "airline_name": {"QP": "Akasa Air", "6E": "IndiGo", "AI": "Air India"}[airline],
        "flight_no": no,
        "origin": o,
        "destination": d,
        "departure": dep,
        "arrival": dep,
    }


def offer(oid, dep, price, airline="QP", no="1101", fare="base_fare"):
    return {
        "id": oid,
        "price_total": price,
        "currency": "INR",
        "owner_airline": airline,
        "conditions": {"fare_type": fare},
        "outbound": {"segments": [seg("DEL", "BOM", dep, airline, no)]},
    }


ORIGINAL = offer("OFF-1", "2026-12-05T07:10:00", 5200)


def picked(**extra):
    import flight_scope

    return {
        "status": "picked",
        "backend": "rest",
        "search_id": "S1",
        "offer_id": "OFF-1",
        "summary": "Akasa Air · 05 Dec 07:10 DEL → BOM · nonstop",
        "price": 5200.0,
        "currency": "INR",
        "depart_date": "2026-12-05",
        "origin": "DEL",
        "destination": "BOM",
        "return_from": None,
        **flight_scope.offer_details(ORIGINAL),
        **extra,
    }


# --- parsing ------------------------------------------------------------------


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"missing_fields": ["nationality", "passport_number"]}, ["nationality", "passport_number"]),
        (
            {
                "status": "need_info",
                "fields": [{"field": "passengers[0].passport_issue_date"}, {"name": "passengers.0.born_on"}],
            },
            ["passport_issue", "date_of_birth"],
        ),
        (
            {"error": "need_info", "detail": {"required_fields": {"passport_expiry": "required"}}},
            ["passport_expiry"],
        ),
        ({"missing_fields": ["address_line1"]}, ["address_line1"]),  # unknown names pass through
        ({"message": "nope"}, []),
    ],
)
def test_parse_missing_fields(body, expected):
    assert letsfg_rest.parse_missing_fields(body) == expected


def test_book_flight_200_need_info_is_need_info(monkeypatch):
    monkeypatch.setattr(
        letsfg_rest,
        "_request",
        lambda *a, **k: {"status": "need_info", "missing_fields": DOC_FIELDS, "message": "need docs"},
    )
    out = letsfg_rest.book_flight("S", "O", BASIC, "k")
    assert out["status"] == "need_info" and out["missing_fields"] == DOC_FIELDS


# --- book(): auto-retry, ask, escalate, log -------------------------------------------


class Script:
    """Stub for letsfg_rest._request on /flights/book: list of bodies or RestErrors."""

    def __init__(self, monkeypatch, *answers):
        self.answers, self.calls = list(answers), []
        monkeypatch.setattr(letsfg_rest, "_request", self)

    def __call__(self, method, path, json_body=None, retries=0):
        self.calls.append(json_body)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def need_info(*fields):
    return letsfg_rest.RestError(400, {"error": "need_info", "missing_fields": list(fields)})


def row():
    rid, _ = db.new_request("UBOOK", "DEL BOM", "claude")
    return rid


def test_saved_passport_is_added_and_retried_once(monkeypatch):
    s = Script(monkeypatch, need_info(*DOC_FIELDS), {"booking_id": "BK1", "state": "authorised"})
    rid = row()
    out = letsfg_booking.book(picked(domestic=True), {**BASIC, **PASSPORT}, rid)
    assert out["status"] == "booking_in_progress" and out["booking_ref"] == "BK1"
    first, second = (c["passengers"][0] for c in s.calls)
    assert "passport_number" not in first and "nationality" not in first
    assert second["passport_number"] == PASSPORT_NO and second["passport_issue"] == "2022-05-02"
    keys = [c["idempotency_key"] for c in s.calls]
    assert keys[0] != keys[1] and keys[1].endswith("-r1")


def test_only_fields_not_saved_are_asked_for(monkeypatch):
    s = Script(monkeypatch, need_info(*DOC_FIELDS))
    profile = {**BASIC, "nationality": "IN", "passport_country": "IN"}
    out = letsfg_booking.book(picked(domestic=True), profile, row())
    assert len(s.calls) == 1  # no retry without every field
    assert out["status"] == "need_info" and not out["escalate"]
    assert out["missing_fields"] == ["passport_number", "passport_issue", "passport_expiry"]
    assert out["required_fields"] == sorted(DOC_FIELDS)


def test_placeholder_document_values_are_never_sent(monkeypatch):
    s = Script(monkeypatch, need_info("passport_number"))
    out = letsfg_booking.book(picked(domestic=True), {**BASIC, "passport_number": "TEST1234"}, row())
    assert len(s.calls) == 1
    assert "passport_number" not in s.calls[0]["passengers"][0]
    assert out["missing_fields"] == ["passport_number"]


def test_retry_that_also_fails_escalates(monkeypatch):
    Script(monkeypatch, need_info(*DOC_FIELDS), need_info("passport_issue"))
    out = letsfg_booking.book(picked(domestic=True), {**BASIC, **PASSPORT}, row())
    assert out["status"] == "need_info" and out["escalate"] is True


def test_required_fields_are_sent_on_first_call_next_time(monkeypatch):
    s = Script(monkeypatch, {"booking_id": "BK2", "state": "authorised"})
    letsfg_booking.book(
        picked(domestic=True, required_fields=DOC_FIELDS, book_attempt=1), {**BASIC, **PASSPORT}, row()
    )
    assert s.calls[0]["passengers"][0]["passport_number"] == PASSPORT_NO
    assert s.calls[0]["idempotency_key"].endswith("-r1")


def test_every_need_info_is_logged_with_airline_fare_and_fields(monkeypatch):
    Script(monkeypatch, need_info("nationality", "passport_number"))
    rid = row()
    letsfg_booking.book(picked(domestic=True), BASIC, rid)
    with sqlite3.connect(db.config.DATABASE_PATH) as c:
        r = c.execute(
            "SELECT airline, airline_name, fare_type, route, domestic, fields, http_status "
            "FROM need_info_log WHERE booking_id=?",
            (rid,),
        ).fetchone()
    assert r == (
        "QP",
        "Akasa Air",
        "base_fare",
        "DEL-BOM",
        1,
        json.dumps(["nationality", "passport_number"]),
        400,
    )
    assert db.passport_history("QP", "base_fare") == "fare"
    assert db.passport_history("QP", "flex") == "airline"
    assert db.passport_history("6E", "base_fare") is None


# --- Slack flow -------------------------------------------------------------------------


@pytest.fixture
def threads(monkeypatch):
    started = []
    monkeypatch.setattr(
        bot_app.threading,
        "Thread",
        lambda target=None, args=(), kwargs=None, **_k: SimpleNamespace(
            start=lambda: started.append((target, args))
        ),
    )
    return started


@pytest.fixture
def client():
    c = MagicMock()
    c.conversations_open.side_effect = lambda users: {"channel": {"id": f"D-{users}"}}
    c.chat_postMessage.return_value = {"ts": "5.0"}
    return c


@pytest.fixture
def traveller(monkeypatch):
    uid = "UNEED"
    monkeypatch.setitem(tp.config.TRAVELLERS, uid, dict(BASIC))
    secure_store.delete(uid)
    return uid


@pytest.fixture
def admins(monkeypatch):
    monkeypatch.setattr(bot_app.config, "TRAVEL_ADMINS", ["UADMIN"])


def texts(client, channel=None):
    return [
        c.kwargs.get("text", "")
        for c in client.chat_postMessage.call_args_list
        if channel is None or c.kwargs.get("channel") == channel
    ]


def docs_row(uid, **extra):
    rid, _ = db.new_request(uid, "DEL to BOM 5 Dec morning", "claude")
    db.update(
        rid,
        status="need_documents",
        details=picked(
            **{
                "domestic": True,
                "missing_fields": DOC_FIELDS,
                "required_fields": sorted(DOC_FIELDS),
                "book_attempt": 1,
                **extra,
            }
        ),
    )
    return rid


def test_need_info_from_booking_asks_traveller_with_buttons(traveller, client, threads, admins):
    rid, _ = db.new_request(traveller, "DEL to BOM", "claude")
    bot_app._handle_book_result(
        client,
        rid,
        traveller,
        "DCH",
        picked(domestic=True),
        {
            "status": "need_info",
            "missing_fields": DOC_FIELDS,
            "book_attempt": 0,
            "required_fields": sorted(DOC_FIELDS),
            "escalate": False,
            "message": "x",
        },
    )
    r = db.get(rid)
    assert r["status"] == "need_documents"
    d = json.loads(r["details"])
    assert d["missing_fields"] == DOC_FIELDS and d["book_attempt"] == 1
    call = client.chat_postMessage.call_args.kwargs
    assert (
        "This fare requires passport details even for a domestic flight. "
        "Saved securely for future bookings." in call["text"]
    )
    actions = [e["action_id"] for b in call["blocks"] if b["type"] == "actions" for e in b["elements"]]
    assert actions == ["provide_documents", "no_passport"]
    assert "travel admin" not in call["text"]
    assert texts(client, "D-UADMIN") == []  # no escalation on the first refusal


def test_documents_modal_shows_only_missing_fields(traveller, client):
    rid = docs_row(traveller, missing_fields=["passport_number", "passport_issue"])
    bot_app._open_documents_modal(
        {"user": {"id": traveller}, "trigger_id": "T"}, client, {"value": json.dumps({"row": rid})}
    )
    view = client.views_open.call_args.kwargs["view"]
    inputs = [b for b in view["blocks"] if b["type"] == "input"]
    assert [b["block_id"] for b in inputs] == ["passport_number", "passport_issue"]
    assert not any(b.get("optional") for b in inputs)
    assert view["callback_id"] == "passenger_documents"
    assert view["blocks"][0]["elements"][0]["text"].endswith(
        "This fare requires passport details even for a domestic flight. Saved securely for future bookings."
    )
    buttons = [e["action_id"] for b in view["blocks"] if b["type"] == "actions" for e in b["elements"]]
    assert buttons == ["no_passport"]


def test_documents_modal_ignores_other_users(traveller, client):
    rid = docs_row(traveller)
    bot_app._open_documents_modal(
        {"user": {"id": "UOTHER"}, "trigger_id": "T"}, client, {"value": json.dumps({"row": rid})}
    )
    client.views_open.assert_not_called()


def _docs_view(rid, fields, **values):
    state = {}
    for f in fields:
        v = values.get(f)
        if f in ("passport_expiry", "passport_issue"):
            state[f] = {"value": {"type": "datepicker", "selected_date": v}}
        else:
            state[f] = {"value": {"type": "plain_text_input", "value": v}}
    return {"private_metadata": json.dumps({"row": rid, "fields": fields}), "state": {"values": state}}


def test_documents_submission_saves_and_retries(traveller, client, threads):
    rid = docs_row(traveller)
    ack = MagicMock()
    bot_app._handle_documents_submission(
        ack, {"user": {"id": traveller}}, client, _docs_view(rid, DOC_FIELDS, **PASSPORT)
    )
    ack.assert_called_once_with()
    assert secure_store.load(traveller)["passport_issue"] == "2022-05-02"
    assert threads == [(bot_app.run_document_retry, (client, rid, traveller))]
    assert all(PASSPORT_NO not in t for t in texts(client))
    assert any("•••726" in t for t in texts(client))


def test_documents_submission_rejects_placeholders_and_blanks(traveller, client, threads):
    rid = docs_row(traveller)
    ack = MagicMock()
    bot_app._handle_documents_submission(
        ack,
        {"user": {"id": traveller}},
        client,
        _docs_view(rid, DOC_FIELDS, **{**PASSPORT, "passport_number": "XXXXXXXX", "passport_issue": None}),
    )
    errs = ack.call_args.kwargs["errors"]
    assert set(errs) == {"passport_number", "passport_issue"}
    assert secure_store.load(traveller) == {} and threads == []


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(bot_app.config, "DRY_RUN", False)
    watched = []
    monkeypatch.setattr(bot_app, "watch_booking", lambda *a, **k: watched.append(a))
    return watched


def test_document_retry_books_same_offer(traveller, client, live, monkeypatch):
    secure_store.save(traveller, PASSPORT)
    rid = docs_row(traveller)
    seen = {}
    monkeypatch.setattr(
        bot_app.letsfg_booking,
        "book",
        lambda p, t, b: seen.update(p=p, t=t) or {"status": "booking_in_progress", "booking_ref": "BK9"},
    )
    bot_app.run_document_retry(client, rid, traveller)
    assert seen["p"]["offer_id"] == "OFF-1" and seen["p"]["book_attempt"] == 1
    assert seen["t"]["passport_number"] == PASSPORT_NO
    assert db.get(rid)["booking_ref"] == "BK9"
    assert live and live[0][3] == "BK9"


def test_document_retry_runs_once(traveller, client, live, monkeypatch):
    rid = docs_row(traveller)
    calls = []
    monkeypatch.setattr(
        bot_app.letsfg_booking,
        "book",
        lambda *a: calls.append(1) or {"status": "booking_in_progress", "booking_ref": "B"},
    )
    bot_app.run_document_retry(client, rid, traveller)
    bot_app.run_document_retry(client, rid, traveller)
    assert calls == [1]


def test_document_retry_respects_dry_run(traveller, client, monkeypatch):
    monkeypatch.setattr(bot_app.config, "DRY_RUN", True)
    rid = docs_row(traveller)
    monkeypatch.setattr(
        bot_app.letsfg_booking, "book", lambda *a: (_ for _ in ()).throw(AssertionError("booked in DRY_RUN"))
    )
    bot_app.run_document_retry(client, rid, traveller)
    assert "Dry run" in texts(client)[0]


def test_failed_retry_escalates_to_admin(traveller, client, live, admins, monkeypatch):
    rid = docs_row(traveller)
    monkeypatch.setattr(
        bot_app.letsfg_booking,
        "book",
        lambda *a: {
            "status": "need_info",
            "missing_fields": ["passport_issue"],
            "escalate": True,
            "book_attempt": 1,
        },
    )
    bot_app.run_document_retry(client, rid, traveller)
    assert db.get(rid)["status"] == "needs_attention"
    admin = texts(client, "D-UADMIN")
    assert admin and f"#{rid}" in admin[0] and "passport_issue" in admin[0]
    assert PASSPORT_NO not in admin[0]
    assert any("travel admin has been told" in t for t in texts(client, f"D-{traveller}"))


def test_escalation_without_admin_configured_says_so(traveller, client, live, monkeypatch):
    monkeypatch.setattr(bot_app.config, "TRAVEL_ADMINS", [])
    rid = docs_row(traveller)
    monkeypatch.setattr(bot_app.letsfg_booking, "book", lambda *a: {"status": "failed", "message": "boom"})
    bot_app.run_document_retry(client, rid, traveller)
    assert any("TRAVEL_ADMIN_SLACK_IDS" in t for t in texts(client))


def test_expired_offer_on_retry_triggers_reprice(traveller, client, live, threads, monkeypatch):
    rid = docs_row(traveller)
    monkeypatch.setattr(
        bot_app.letsfg_booking,
        "book",
        lambda *a: {"status": "failed", "offer_expired": True, "message": "expired"},
    )
    bot_app.run_document_retry(client, rid, traveller)
    assert db.get(rid)["status"] == "expired"
    assert threads[0][0] is bot_app.reprice_and_reconfirm


def _search(monkeypatch, *offers):
    monkeypatch.setattr(
        bot_app.letsfg_rest,
        "search_flights",
        lambda *a, **k: {"status": "ok", "search_id": "S2", "offers": list(offers), "currency": "INR"},
    )


def _cards(client):
    return [
        c.kwargs
        for c in client.chat_postMessage.call_args_list
        if any(
            b.get("type") == "actions" and str(b.get("block_id", "")).startswith("traveldesk_decision_")
            for b in c.kwargs.get("blocks") or []
        )
    ]


def test_reprice_with_new_price_asks_for_approval_again(traveller, client, monkeypatch):
    rid = docs_row(traveller)
    _search(monkeypatch, offer("OFF-NEW", "2026-12-05T07:10:00", 5600))
    p = json.loads(db.get(rid)["details"])
    bot_app.reprice_and_reconfirm(client, rid, traveller, "DCH", p, {})
    card = _cards(client)[0]
    assert "price changed from 5200.0 to 5600.0" in json.dumps(card["blocks"])
    with sqlite3.connect(db.config.DATABASE_PATH) as c:
        new_id, status, details = c.execute(
            "SELECT id, status, details FROM bookings ORDER BY id DESC LIMIT 1"
        ).fetchone()
    d = json.loads(details)
    assert status == "awaiting_confirm" and new_id != rid
    assert d["offer_id"] == "OFF-NEW" and d["search_id"] == "S2" and d["price"] == 5600
    assert d["required_fields"] == sorted(DOC_FIELDS) and d["book_attempt"] == 0


def test_reprice_when_flight_gone_says_so(traveller, client, monkeypatch):
    rid = docs_row(traveller)
    _search(monkeypatch, offer("OFF-X", "2026-12-05T11:00:00", 4000, "6E", "201"))
    bot_app.reprice_and_reconfirm(client, rid, traveller, "DCH", json.loads(db.get(rid)["details"]), {})
    assert _cards(client) == []
    assert "no longer available" in texts(client)[0]


def test_no_passport_shows_labelled_alternatives(traveller, client, threads, monkeypatch):
    rid = docs_row(traveller)
    bot_app._handle_no_passport(
        {"user": {"id": traveller}, "view": {"id": "V1"}}, client, {"value": json.dumps({"row": rid})}
    )
    assert db.get(rid)["status"] == "no_passport"
    client.views_update.assert_called_once()
    assert threads[0][0] is bot_app.find_alternatives

    db.log_need_info(
        booking_id=None,
        airline="AI",
        airline_name="Air India",
        fare_type="saver",
        route="DEL-BOM",
        domestic=True,
        price=1,
        currency="INR",
        fields=["passport_number"],
    )
    _search(
        monkeypatch,
        ORIGINAL,  # same flight: excluded
        offer("A1", "2026-12-05T08:30:00", 4800, "6E", "201"),  # in window
        offer("A2", "2026-12-05T18:00:00", 3000, "6E", "555"),  # outside ±3h
        offer("A3", "2026-12-05T09:00:00", 4500, "AI", "887", "saver"),
    )  # known passport fare
    bot_app.find_alternatives(client, rid, traveller)
    post = client.chat_postMessage.call_args.kwargs
    body = json.dumps(post["blocks"])
    assert "Needs confirmation" in body and "not yet confirmed bookable without a passport" in body
    alts = json.loads(db.get(rid)["details"])["alternatives"]
    assert [a["offer_id"] for a in alts] == ["A1"]
    assert "1 other flight(s) hidden" in body


def test_no_alternatives_tells_traveller_and_admin(traveller, client, admins, monkeypatch):
    rid = docs_row(traveller)
    db.update(rid, status="no_passport")
    db.log_need_info(
        booking_id=None,
        airline="6E",
        airline_name="IndiGo",
        fare_type="base_fare",
        route="DEL-BOM",
        domestic=True,
        price=1,
        currency="INR",
        fields=["nationality"],
    )
    _search(monkeypatch, ORIGINAL, offer("A1", "2026-12-05T08:30:00", 4800, "6E", "201"))
    bot_app.find_alternatives(client, rid, traveller)
    assert any(
        "couldn't find a flight you can book without a passport" in t for t in texts(client, f"D-{traveller}")
    )
    assert any("no passport" in t for t in texts(client, "D-UADMIN"))
    assert db.get(rid)["status"] == "needs_attention"


def test_choosing_alternative_asks_for_approval(traveller, client, monkeypatch):
    rid = docs_row(traveller)
    db.update(rid, status="no_passport")
    # A fare type never logged as needing a passport (other tests log 6E base_fare).
    _search(monkeypatch, offer("A1", "2026-12-05T08:30:00", 4800, "6E", "201", fare="flexi_promo"))
    bot_app.find_alternatives(client, rid, traveller)
    body = {"user": {"id": traveller}, "channel": {"id": "DCH"}, "message": {"ts": "5.0", "blocks": []}}
    bot_app._handle_choose_alternative(body, client, {"value": json.dumps({"row": rid, "alt": 0})})
    bot_app._handle_choose_alternative(body, client, {"value": json.dumps({"row": rid, "alt": 0})})
    assert db.get(rid)["status"] == "replaced"
    cards = _cards(client)
    assert len(cards) == 1  # second click ignored
    assert "Needs confirmation" in json.dumps(cards[0]["blocks"])


def test_confirmation_card_warns_about_known_passport_fares():
    db.log_need_info(
        booking_id=None,
        airline="QP",
        airline_name="Akasa Air",
        fare_type="base_fare",
        route="DEL-BOM",
        domestic=True,
        price=1,
        currency="INR",
        fields=["passport_number"],
    )
    blocks = bot_app._confirm_blocks(1, "U", "req", picked(domestic=True), BASIC)
    assert "has asked for passport details on this fare type before" in json.dumps(blocks)


# --- address + phone country (IndiGo base_fare, row 24 shape) ---------------------------

ROW24 = [
    "address_city",
    "address_country",
    "address_line1",
    "address_postal",
    "nationality",
    "phone_country",
    "passport_number",
    "passport_country",
    "passport_issue",
    "passport_expiry",
]
ADDRESS = {
    "address_line1": "12 Synthetic Lane",
    "address_city": "New Delhi",
    "address_postal": "110001",
    "address_country": "IN",
    "phone_country": "IN",
}


def test_row24_refusal_asks_traveller_for_all_ten_fields(traveller, client, threads, admins):
    rid, _ = db.new_request(traveller, "DEL to BOM", "claude")
    bot_app._handle_book_result(
        client,
        rid,
        traveller,
        "DCH",
        picked(domestic=True),
        {
            "status": "need_info",
            "missing_fields": ROW24,
            "book_attempt": 0,
            "required_fields": sorted(ROW24),
            "escalate": False,
            "message": "x",
        },
    )
    assert db.get(rid)["status"] == "need_documents"
    assert texts(client, "D-UADMIN") == []  # no escalation: every field is collectable
    bot_app._open_documents_modal(
        {"user": {"id": traveller}, "trigger_id": "T"}, client, {"value": json.dumps({"row": rid})}
    )
    inputs = [b for b in client.views_open.call_args.kwargs["view"]["blocks"] if b["type"] == "input"]
    assert [b["block_id"] for b in inputs] == ROW24
    assert not any(b.get("optional") for b in inputs)


def test_row24_submission_saves_encrypted_and_retry_sends_them(traveller, client, threads, monkeypatch):
    rid = docs_row(traveller, missing_fields=ROW24, required_fields=sorted(ROW24))
    ack = MagicMock()
    bot_app._handle_documents_submission(
        ack, {"user": {"id": traveller}}, client, _docs_view(rid, ROW24, **PASSPORT, **ADDRESS)
    )
    ack.assert_called_once_with()
    saved = secure_store.load(traveller)
    assert {k: saved[k] for k in ADDRESS} == ADDRESS
    assert all("Synthetic" not in t and "110001" not in t for t in texts(client))
    # retry sends exactly the saved values
    s = Script(monkeypatch, {"booking_id": "BK9", "state": "authorised"})
    row_ = db.get(rid)
    out = letsfg_booking.book(json.loads(row_["details"]), tp.load(traveller), rid)
    assert out["booking_ref"] == "BK9"
    sent = s.calls[0]["passengers"][0]
    assert {k: sent[k] for k in ADDRESS} == ADDRESS and sent["passport_number"] == PASSPORT_NO


def test_address_not_sent_unless_letsfg_asked(traveller, monkeypatch):
    secure_store.save(traveller, ADDRESS)
    s = Script(monkeypatch, {"booking_id": "BK1", "state": "authorised"})
    letsfg_booking.book(picked(domestic=True), tp.load(traveller), row())
    assert not any(k in s.calls[0]["passengers"][0] for k in ADDRESS)


@pytest.mark.parametrize(
    "field,value",
    [
        ("address_line1", "Test address"),
        ("address_line1", "dummy"),
        ("address_city", "XXXX"),
        ("address_postal", "!!"),
        ("address_country", "India"),
        ("phone_country", "+91"),
    ],
)
def test_address_placeholders_and_bad_values_rejected(traveller, client, threads, field, value):
    rid = docs_row(traveller, missing_fields=ROW24, required_fields=sorted(ROW24))
    ack = MagicMock()
    bot_app._handle_documents_submission(
        ack,
        {"user": {"id": traveller}},
        client,
        _docs_view(rid, ROW24, **PASSPORT, **{**ADDRESS, field: value}),
    )
    assert set(ack.call_args.kwargs["errors"]) == {field}
    assert secure_store.load(traveller) == {} and threads == []


def test_saved_address_is_used_for_auto_retry(monkeypatch):
    s = Script(monkeypatch, need_info(*ROW24), {"booking_id": "BK2", "state": "authorised"})
    out = letsfg_booking.book(picked(domestic=True), {**BASIC, **PASSPORT, **ADDRESS}, row())
    assert out["booking_ref"] == "BK2" and len(s.calls) == 2
    assert s.calls[1]["passengers"][0]["address_postal"] == "110001"
