"""LetsFG asks for a passport on every booking (even domestic India).

Full redacted error logging, detection (explicit message / 2+ airlines),
persistence per traveller, no dead-end alternatives, manual-booking
escalation, and document collection + one retry with no duplicate booking.

Synthetic data only. LetsFG HTTP and Slack are stubbed; nothing leaves the
machine.
"""
from __future__ import annotations

import json
import logging
import uuid
from unittest.mock import MagicMock

import pytest

import app as bot_app
import db
import letsfg_booking
import letsfg_rest
import secure_store
import traveller_profile as tp
from tests.test_need_info_flow import (  # noqa: F401 — fixtures
    ADDRESS, BASIC, PASSPORT, PASSPORT_NO, ROW24, Script, _docs_view, client, picked, texts,
)

EVERYONE = ("The travellers are missing details an airline checkout requires: "
            + ", ".join(ROW24) + ". Supply them and call again. A passport is needed for every "
            "traveller. Why a passport: the airline's checkout demands the document for all "
            "bookings made through this channel. END-OF-MESSAGE")


def refusal(fields=ROW24, message=""):
    body = {"error": "missing_fields", "missing_fields": list(fields)}
    if message:
        body["message"] = message
    return letsfg_rest.RestError(400, body)


@pytest.fixture(autouse=True)
def _isolate_need_info_log():
    """Remove need_info_log rows these tests add (the table is shared learning data)."""
    db.passport_history("QP")  # make sure the table exists
    with db._lock, db._conn() as c:
        before = c.execute("SELECT COALESCE(MAX(id), 0) FROM need_info_log").fetchone()[0]
    yield
    with db._lock, db._conn() as c:
        c.execute("DELETE FROM need_info_log WHERE id > ?", (before,))


@pytest.fixture
def user(monkeypatch):
    uid = "U" + uuid.uuid4().hex[:10].upper()
    monkeypatch.setitem(tp.config.TRAVELLERS, uid, dict(BASIC))
    secure_store.delete(uid)
    return uid


@pytest.fixture
def admins(monkeypatch):
    monkeypatch.setattr(bot_app.config, "TRAVEL_ADMINS", ["UADMIN"])


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(bot_app.config, "DRY_RUN", False)
    watched = []
    monkeypatch.setattr(bot_app, "watch_booking", lambda *a, **k: watched.append(a))
    return watched


def new_row(uid, **details):
    rid, _ = db.new_request(uid, "DEL to BOM 22 Nov morning", "claude")
    if details:
        db.update(rid, details=details)
    return rid


def restricted_docs_row(uid):
    db.record_restriction(uid, "letsfg", db.PASSPORT_REQUIRED, fields=ROW24, source="test")
    rid = new_row(uid)
    db.update(rid, status="need_documents", details=picked(
        domestic=True, missing_fields=ROW24, required_fields=sorted(ROW24), book_attempt=1,
        passport_required_everywhere=True))
    return rid


# --- 1. full error logging with redaction --------------------------------------------


def test_full_need_info_message_is_logged_and_redacted(user, monkeypatch, caplog):
    # LetsFG echoes passenger data back; none of it may reach the log.
    echoed = EVERYONE + f" (for {BASIC['given_name']} {BASIC['family_name']}, " \
                        f"{BASIC['email']}, +91 98000 00000)"
    assert len(echoed) > 300
    Script(monkeypatch, refusal(message=echoed))
    with caplog.at_level(logging.WARNING, logger="letsfg_booking"):
        letsfg_booking.book(picked(domestic=True), dict(BASIC), new_row(user))
    line = next(r.getMessage() for r in caplog.records if "LetsFG need_info" in r.getMessage())
    assert "END-OF-MESSAGE" in line            # no 300-character truncation
    assert "Why a passport" in line
    for secret in (BASIC["email"], BASIC["given_name"], BASIC["family_name"], "98000"):
        assert secret not in line
    assert "[redacted]" in line


def test_scrub_keeps_dates_and_field_names():
    out = letsfg_rest._scrub("passport_expiry must be after 2026-11-22", {"given_name": "Raj"})
    assert out == "passport_expiry must be after 2026-11-22"


# --- 2. detection + persistence ---------------------------------------------------------


def test_explicit_message_marks_passport_required_for_traveller(user, monkeypatch):
    Script(monkeypatch, refusal(message=EVERYONE))
    out = letsfg_booking.book(picked(domestic=True), dict(BASIC), new_row(user))
    assert out["status"] == "need_info" and out["passport_required_everywhere"] is True
    r = db.get_restriction(user, "letsfg", db.PASSPORT_REQUIRED)
    assert r["source"] == "letsfg_message" and r["fields"] == ROW24


def test_one_airline_without_explicit_message_is_not_a_restriction(user, monkeypatch):
    Script(monkeypatch, refusal())
    out = letsfg_booking.book(picked(domestic=True, airline="6E"), dict(BASIC), new_row(user))
    assert not out["passport_required_everywhere"]
    assert db.get_restriction(user, "letsfg", db.PASSPORT_REQUIRED) is None


def test_two_airlines_asking_for_passport_marks_restriction(user, monkeypatch):
    Script(monkeypatch, refusal(), refusal())
    letsfg_booking.book(picked(domestic=True, airline="6E"), dict(BASIC), new_row(user))
    out = letsfg_booking.book(picked(domestic=True, airline="AI"), dict(BASIC), new_row(user))
    assert out["passport_required_everywhere"] is True
    assert db.get_restriction(user, "letsfg", db.PASSPORT_REQUIRED)["source"] == "multiple_airlines"


def test_restriction_is_per_traveller(user, monkeypatch):
    Script(monkeypatch, refusal(message=EVERYONE))
    letsfg_booking.book(picked(domestic=True), dict(BASIC), new_row(user))
    assert db.get_restriction("USOMEONEELSE", "letsfg", db.PASSPORT_REQUIRED) is None


def test_known_restriction_asks_before_calling_letsfg(user, monkeypatch):
    db.record_restriction(user, "letsfg", db.PASSPORT_REQUIRED, fields=ROW24, source="test")
    s = Script(monkeypatch)  # any LetsFG call would fail: no answers scripted
    out = letsfg_booking.book(picked(domestic=True, airline="QP"), dict(BASIC), new_row(user))
    assert s.calls == []  # not rediscovered by hitting LetsFG again
    assert out["status"] == "need_info" and out["passport_required_everywhere"] is True
    assert set(out["missing_fields"]) == set(ROW24)


def test_known_restriction_sends_saved_documents_first_time(user, monkeypatch):
    db.record_restriction(user, "letsfg", db.PASSPORT_REQUIRED, fields=ROW24, source="test")
    s = Script(monkeypatch, {"booking_id": "BK1", "state": "authorised"})
    out = letsfg_booking.book(picked(domestic=True), {**BASIC, **PASSPORT, **ADDRESS}, new_row(user))
    assert out["booking_ref"] == "BK1" and len(s.calls) == 1
    sent = s.calls[0]["passengers"][0]
    assert sent["passport_number"] == PASSPORT_NO and sent["address_postal"] == "110001"


def test_international_booking_is_unchanged_by_restriction(user, monkeypatch):
    db.record_restriction(user, "letsfg", db.PASSPORT_REQUIRED, fields=ROW24, source="test")
    s = Script(monkeypatch, {"booking_id": "BK2", "state": "authorised"})
    out = letsfg_booking.book(picked(domestic=False), {**BASIC, **PASSPORT}, new_row(user))
    assert out["booking_ref"] == "BK2"
    assert "address_line1" not in s.calls[0]["passengers"][0]


# --- 3. no dead-end alternatives -------------------------------------------------------


def _buttons(blocks):
    return [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]


def test_restricted_need_info_hides_alternatives(user, client, admins):
    rid = new_row(user)
    bot_app._handle_book_result(client, rid, user, "DCH", picked(domestic=True), {
        "status": "need_info", "missing_fields": ROW24, "required_fields": sorted(ROW24),
        "book_attempt": 0, "escalate": False, "passport_required_everywhere": True})
    call = client.chat_postMessage.call_args.kwargs
    assert _buttons(call["blocks"]) == ["provide_documents", "request_manual_booking"]
    assert "requires passport details even for domestic flights" in call["text"]
    assert db.get(rid)["status"] == "need_documents"
    assert texts(client, "D-UADMIN") == []  # not escalated while they may have a passport


def test_unrestricted_need_info_still_offers_alternatives(user, client):
    rid = new_row(user)
    bot_app._handle_book_result(client, rid, user, "DCH", picked(domestic=True), {
        "status": "need_info", "missing_fields": ROW24, "required_fields": sorted(ROW24),
        "book_attempt": 0, "escalate": False})
    assert _buttons(client.chat_postMessage.call_args.kwargs["blocks"]) == [
        "provide_documents", "no_passport"]


def test_restricted_documents_modal_offers_admin_not_alternatives(user, client):
    rid = restricted_docs_row(user)
    bot_app._open_documents_modal({"user": {"id": user}, "trigger_id": "T"}, client,
                                  {"value": json.dumps({"row": rid})})
    view = client.views_open.call_args.kwargs["view"]
    assert [b["block_id"] for b in view["blocks"] if b["type"] == "input"] == ROW24
    assert _buttons(view["blocks"]) == ["request_manual_booking"]


def test_old_no_passport_button_does_not_search_when_restricted(user, client, admins, monkeypatch):
    rid = restricted_docs_row(user)
    monkeypatch.setattr(bot_app, "_search_same_route",
                        lambda *a: (_ for _ in ()).throw(AssertionError("searched alternatives")))
    bot_app._handle_no_passport({"user": {"id": user}}, client, {"value": json.dumps({"row": rid})})
    assert db.get(rid)["status"] == "needs_attention"
    assert texts(client, "D-UADMIN")


def test_find_alternatives_stops_when_restricted(user, client, admins, monkeypatch):
    rid = restricted_docs_row(user)
    db.update(rid, status="no_passport")
    monkeypatch.setattr(bot_app, "_search_same_route",
                        lambda *a: (_ for _ in ()).throw(AssertionError("searched alternatives")))
    bot_app.find_alternatives(client, rid, user)
    assert db.get(rid)["status"] == "needs_attention"


def test_confirmation_card_warns_restricted_traveller(user):
    db.record_restriction(user, "letsfg", db.PASSPORT_REQUIRED, fields=ROW24, source="test")
    assert "requires passport details" in bot_app._passport_warning(picked(domestic=True), user)
    assert bot_app._passport_warning(picked(domestic=False), user) is None


# --- 4. admin escalation / manual booking -----------------------------------------------


def test_manual_booking_request_escalates_once(user, client, admins):
    rid = restricted_docs_row(user)
    action = {"value": json.dumps({"row": rid})}
    bot_app._request_manual_booking({"user": {"id": user}}, client, rid, db.get(rid))
    bot_app._request_manual_booking({"user": {"id": user}}, client, rid, db.get(rid))
    r = db.get(rid)
    assert r["status"] == "needs_attention" and r["booking_ref"] is None
    admin = texts(client, "D-UADMIN")
    assert len(admin) == 1 and "book this flight manually" in admin[0] and f"#{rid}" in admin[0]
    assert PASSPORT_NO not in admin[0]
    mine = texts(client, f"D-{user}")
    assert any("Nothing was booked or charged" in t and "travel admin has been told" in t
               for t in mine)
    assert action  # button payload carries only the row id


def test_manual_booking_ignores_other_users(user, client, admins):
    rid = restricted_docs_row(user)
    found = bot_app._row_for_actor({"user": {"id": "UOTHER"}},
                                   {"value": json.dumps({"row": rid}),
                                    "action_id": "request_manual_booking"}, client,
                                   expect=("need_documents",))
    assert found is None and db.get(rid)["status"] == "need_documents"


def test_manual_booking_without_admin_configured_says_so(user, client, monkeypatch):
    monkeypatch.setattr(bot_app.config, "TRAVEL_ADMINS", [])
    rid = restricted_docs_row(user)
    bot_app._request_manual_booking({"user": {"id": user}}, client, rid, db.get(rid))
    assert any("TRAVEL_ADMIN_SLACK_IDS" in t for t in texts(client))


# --- 5. document collection → one retry, no duplicates ----------------------------------


def test_documents_saved_then_single_retry_books_once(user, client, live, monkeypatch):
    rid = restricted_docs_row(user)
    started = []
    monkeypatch.setattr(bot_app.threading, "Thread", lambda target=None, args=(), **_k:
                        MagicMock(start=lambda: started.append((target, args))))
    ack = MagicMock()
    bot_app._handle_documents_submission(ack, {"user": {"id": user}}, client,
                                         _docs_view(rid, ROW24, **PASSPORT, **ADDRESS))
    ack.assert_called_once_with()
    saved = secure_store.load(user)
    assert saved["passport_number"] == PASSPORT_NO and saved["address_city"] == "New Delhi"
    assert all(PASSPORT_NO not in t and "110001" not in t for t in texts(client))
    assert started == [(bot_app.run_document_retry, (client, rid, user))]

    s = Script(monkeypatch, {"booking_id": "BK7", "state": "authorised", "pay_url": "https://pay"})
    bot_app.run_document_retry(client, rid, user)
    bot_app.run_document_retry(client, rid, user)  # double click / duplicate thread
    assert len(s.calls) == 1  # one booking request only
    sent = s.calls[0]
    assert sent["idempotency_key"].endswith("-r1")
    assert sent["passengers"][0]["passport_number"] == PASSPORT_NO
    r = db.get(rid)
    assert r["booking_ref"] == "BK7" and r["payment_status"] == "awaiting_payment"
    assert len(live) == 1


def test_need_info_is_never_marked_booked(user, client, live, monkeypatch):
    rid = restricted_docs_row(user)
    Script(monkeypatch, refusal(message=EVERYONE))
    secure_store.save(user, {**PASSPORT, **ADDRESS})
    bot_app.run_document_retry(client, rid, user)
    r = db.get(rid)
    assert r["booking_ref"] is None and r["status"] == "needs_attention"
    assert live == []


def test_dry_run_never_calls_letsfg_during_document_retry(user, client, monkeypatch):
    monkeypatch.setattr(bot_app.config, "DRY_RUN", True)
    rid = restricted_docs_row(user)
    s = Script(monkeypatch)
    bot_app.run_document_retry(client, rid, user)
    assert s.calls == [] and db.get(rid)["status"] == "need_documents"


def test_restriction_is_derived_from_existing_history(user, client, admins, monkeypatch):
    """Travellers already refused by 2+ airlines (before this rule existed)."""
    for airline in ("6E", "AI"):
        db.log_need_info(booking_id=new_row(user), airline=airline, airline_name=airline,
                         fare_type="base_fare", route="DEL-BOM", domestic=True, price=1,
                         currency="INR", fields=ROW24)
    rid = new_row(user)
    db.update(rid, status="need_documents", details=picked(
        domestic=True, missing_fields=ROW24, required_fields=sorted(ROW24), book_attempt=1))
    monkeypatch.setattr(bot_app, "_search_same_route",
                        lambda *a: (_ for _ in ()).throw(AssertionError("searched alternatives")))
    bot_app._handle_no_passport({"user": {"id": user}}, client, {"value": json.dumps({"row": rid})})
    assert db.get(rid)["status"] == "needs_attention"
    r = db.get_restriction(user, "letsfg", db.PASSPORT_REQUIRED)
    assert r["source"] == "multiple_airlines" and set(r["fields"]) == set(ROW24)
