"""Traveller profile: validation, encrypted storage, Slack collection, log hygiene.

All passport data here is a synthetic fixture. No Slack, LetsFG or payment
call is made; Slack's client is a MagicMock and booking threads are captured.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app as bot_app
import db
import letsfg_booking
import secure_store
import traveller_profile as tp

TODAY = date(2026, 11, 17)
PASSPORT_NO = "Z9081726"
DOB = "1990-01-02"
VALID = {
    "given_name": "Raj", "family_name": "Gupta", "email": "raj@example.com",
    "phone": "+919800000000", "date_of_birth": DOB, "gender": "Male",
    "nationality": "in", "passport_number": "z 908-1726", "passport_country": "IN",
    "passport_expiry": "2032-05-01", "passport_issue": "2022-05-02",
}
SEARCH_ONLY = {"given_name": "Raj", "family_name": "Gupta", "email": "raj@example.com"}


# --- validation: valid ----------------------------------------------------


def test_valid_profile_normalizes_values():
    res = tp.validate(VALID, for_booking=True, today=TODAY)
    assert res.ok, res.summary()
    assert res.cleaned["gender"] == "m"
    assert res.cleaned["passport_number"] == PASSPORT_NO
    assert res.cleaned["nationality"] == "IN"


def test_search_only_profile_is_valid_for_search():
    assert tp.validate(SEARCH_ONLY, for_booking=False).ok


def test_existing_placeholder_entry_still_searches():
    # Search checks presence only, so legacy entries keep working.
    assert tp.validate({"given_name": "a", "family_name": "b", "email": "a@b"},
                       for_booking=False).ok


# --- validation: missing --------------------------------------------------


def test_search_only_profile_lists_missing_booking_fields():
    res = tp.validate(SEARCH_ONLY, for_booking=True, today=TODAY)
    assert set(res.missing) == set(tp.BOOKING_ONLY_REQUIRED)
    assert not res.ok


def test_null_and_blank_values_count_as_missing():
    profile = {**VALID, "passport_number": None, "date_of_birth": "  "}
    res = tp.validate(profile, for_booking=True, today=TODAY)
    assert set(res.missing) == {"passport_number", "date_of_birth"}


def test_missing_search_fields():
    res = tp.validate({"given_name": "Raj"}, for_booking=False)
    assert set(res.missing) == {"family_name", "email"}


# --- validation: invalid --------------------------------------------------


@pytest.mark.parametrize("field,value,reason", [
    ("date_of_birth", "02/01/1990", "YYYY-MM-DD"),
    ("date_of_birth", "2030-01-01", "past"),
    ("date_of_birth", "2020-01-01", "at least 12"),
    ("date_of_birth", "1890-01-01", "plausible"),
    ("gender", "x", "male or female"),
    ("nationality", "IND", "2-letter"),
    ("passport_country", "India", "2-letter"),
    ("passport_number", "AB1", "5-20"),
    ("passport_number", "AAAAAAAA", "placeholder"),
    ("passport_number", "TEST1234", "placeholder"),
    ("passport_number", "123456789", "placeholder"),
    ("passport_expiry", "2025-01-01", "expired"),
    ("passport_expiry", "not-a-date", "YYYY-MM-DD"),
    ("passport_issue", "2027-01-01", "future"),
    ("email", "a@b", "email"),
    ("given_name", "R4j", "letters"),
])
def test_invalid_values_are_rejected(field, value, reason):
    res = tp.validate({**VALID, field: value}, for_booking=True, today=TODAY)
    assert field in res.invalid
    assert reason in res.invalid[field]
    assert value not in res.summary()  # reasons never echo the value


def test_passport_expiring_before_travel_is_rejected():
    res = tp.validate({**VALID, "passport_expiry": "2026-12-01"}, for_booking=True,
                      travel_date="2026-12-15", today=TODAY)
    assert "6 months after your last flight" in res.invalid["passport_expiry"]


# --- profile helpers --------------------------------------------------------


def test_public_view_strips_sensitive_fields():
    view = tp.public_view(VALID)
    for f in tp.SENSITIVE_FIELDS:
        assert f not in view
    assert view["given_name"] == "Raj"


def test_redact_reports_presence_only():
    r = tp.redact(VALID)
    assert r["passport_number"] == "set"
    assert PASSPORT_NO not in json.dumps(r) and DOB not in json.dumps(r)


def test_load_merges_json_aliases_and_secure_store(monkeypatch):
    monkeypatch.setitem(tp.config.TRAVELLERS, "UMERGE",
                        {**SEARCH_ONLY, "born_on": DOB, "passport_number": None})
    secure_store.delete("UMERGE")
    secure_store.save("UMERGE", {"passport_number": PASSPORT_NO, "gender": "m"})
    p = tp.load("UMERGE")
    assert p["date_of_birth"] == DOB and "born_on" not in p
    assert p["passport_number"] == PASSPORT_NO and p["gender"] == "m"


# --- secure store -----------------------------------------------------------


def test_secure_store_encrypts_at_rest():
    secure_store.save("UENC", {"passport_number": PASSPORT_NO, "date_of_birth": DOB,
                               "email": "ignored-not-allowed@example.com"})
    with sqlite3.connect(db.config.DATABASE_PATH) as c:
        blob = c.execute("SELECT ciphertext FROM traveller_secure WHERE slack_user='UENC'").fetchone()[0]
    raw = bytes(blob) if not isinstance(blob, bytes) else blob
    assert PASSPORT_NO.encode() not in raw and DOB.encode() not in raw
    assert secure_store.load("UENC") == {"passport_number": PASSPORT_NO, "date_of_birth": DOB}


def test_secure_store_wrong_key_returns_nothing(monkeypatch):
    secure_store.save("UKEY", {"passport_number": PASSPORT_NO})
    monkeypatch.setenv("TRAVELDESK_DATA_KEY", "not-a-fernet-key")
    assert secure_store.is_configured() is False
    monkeypatch.setenv("TRAVELDESK_DATA_KEY", "aGVsbG8taGVsbG8taGVsbG8taGVsbG8taGVsbG8tMTI=")
    assert secure_store.load("UKEY") == {}


def test_secure_store_fails_closed_without_key(monkeypatch):
    monkeypatch.delenv("TRAVELDESK_DATA_KEY")
    with pytest.raises(secure_store.SecureStoreUnavailable):
        secure_store.save("UNOKEY", {"passport_number": PASSPORT_NO})


def test_secure_store_restricts_db_permissions():
    import os
    import stat
    secure_store.save("UPERM", {"gender": "f"})
    mode = os.stat(db.config.DATABASE_PATH).st_mode
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO)


# --- REST booking gate ------------------------------------------------------


def test_rest_booking_without_passport_never_calls_api(monkeypatch):
    monkeypatch.setattr(letsfg_booking.letsfg_rest, "book_flight",
                        lambda *a: (_ for _ in ()).throw(AssertionError("API called")))
    out = letsfg_booking.book({"backend": "rest", "search_id": "S", "offer_id": "O"},
                              SEARCH_ONLY, None)
    assert out["status"] == "need_info"
    assert "Passport number" in out["message"]


def test_rest_booking_sends_cleaned_values(monkeypatch):
    sent = {}
    monkeypatch.setattr(letsfg_booking.letsfg_rest, "book_flight",
                        lambda s, o, t, k: sent.update(t) or {"status": "booking_in_progress",
                                                               "booking_ref": "B"})
    letsfg_booking.book({"backend": "rest", "search_id": "S", "offer_id": "O"}, VALID, None)
    assert sent["passport_number"] == PASSPORT_NO and sent["gender"] == "m"


# --- Slack flow ---------------------------------------------------------------


def _row(user, backend="rest"):
    row_id, _ = db.new_request(user, f"DEL BOM {user}", "claude")
    db.update(row_id, status="awaiting_confirm", manager_channel="CDM", manager_message_ts="1.0",
              details={"status": "picked", "backend": backend, "search_id": "S", "offer_id": "O",
                       "summary": "6E · DEL → BOM", "price": 5000, "currency": "INR",
                       "depart_date": "2026-12-01"})
    return row_id


def _body(user, trigger="T1"):
    return {"user": {"id": user}, "trigger_id": trigger, "channel": {"id": "CDM"},
            "container": {"channel_id": "CDM", "message_ts": "1.0"},
            "message": {"ts": "1.0", "blocks": [{"type": "actions"}]}}


@pytest.fixture
def threads(monkeypatch):
    started = []
    monkeypatch.setattr(bot_app.threading, "Thread",
                        lambda target=None, args=(), **_k: SimpleNamespace(
                            start=lambda: started.append((target, args))))
    return started


@pytest.fixture
def user(monkeypatch):
    def make(uid, profile=SEARCH_ONLY):
        monkeypatch.setitem(tp.config.TRAVELLERS, uid, dict(profile))
        secure_store.delete(uid)
        return uid
    return make


def test_confirm_opens_modal_for_missing_fields_only(user, threads):
    uid = user("UMODAL", {**SEARCH_ONLY, "gender": "female", "nationality": "IN"})
    row_id = _row(uid)
    client = MagicMock()
    bot_app._handle_decision(_body(uid), client, {"value": json.dumps({"row": row_id})}, "confirmed")

    view = client.views_open.call_args.kwargs["view"]
    inputs = {b["block_id"]: b for b in view["blocks"] if b["type"] == "input"}
    # Missing basic field + the whole passport section (pre-filled where known).
    assert set(inputs) == {"date_of_birth", "nationality", "passport_number",
                           "passport_country", "passport_expiry", "passport_issue"}
    assert inputs["passport_issue"]["optional"] is True
    assert inputs["nationality"]["element"]["initial_value"] == "IN"
    assert "initial_value" not in inputs["passport_number"]["element"]
    assert db.get(row_id)["status"] == "awaiting_confirm"  # not claimed yet
    assert threads == []


def test_complete_profile_books_without_modal(user, threads):
    uid = user("UFULL", VALID)
    row_id = _row(uid)
    client = MagicMock()
    bot_app._handle_decision(_body(uid), client, {"value": json.dumps({"row": row_id})}, "confirmed")
    client.views_open.assert_not_called()
    assert db.get(row_id)["status"] == "confirmed"
    assert threads and threads[0][0] is bot_app.run_booking


def test_mcp_backend_does_not_require_passport(user, threads):
    uid = user("UMCP")
    row_id = _row(uid, backend="mcp")
    client = MagicMock()
    bot_app._handle_decision(_body(uid), client, {"value": json.dumps({"row": row_id})}, "confirmed")
    client.views_open.assert_not_called()
    assert db.get(row_id)["status"] == "confirmed"


def test_invalid_search_fields_send_admin_message(user, threads):
    uid = user("UADMIN", {**SEARCH_ONLY, "email": "a@b"})
    row_id = _row(uid)
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "CDM"}}
    bot_app._handle_decision(_body(uid), client, {"value": json.dumps({"row": row_id})}, "confirmed")
    client.views_open.assert_not_called()
    texts = [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]
    assert any("admin" in t and "Email" in t for t in texts)
    assert db.get(row_id)["status"] == "awaiting_confirm"


def _view(row_id, **values):
    state = {}
    for f, v in values.items():
        if f in ("date_of_birth", "passport_expiry", "passport_issue"):
            state[f] = {"value": {"type": "datepicker", "selected_date": v}}
        elif f == "gender":
            state[f] = {"value": {"type": "static_select", "selected_option": {"value": v}}}
        else:
            state[f] = {"value": {"type": "plain_text_input", "value": v}}
    return {"private_metadata": json.dumps({"row": row_id}), "state": {"values": state}}


FULL_SUBMISSION = dict(date_of_birth=DOB, gender="m", nationality="IN",
                       passport_number=PASSPORT_NO, passport_country="IN",
                       passport_expiry="2032-05-01")


def test_modal_invalid_submission_returns_field_errors(user, threads):
    uid = user("USUBBAD")
    row_id = _row(uid)
    ack = MagicMock()
    bot_app._handle_passenger_details(
        ack, {"user": {"id": uid}}, MagicMock(),
        _view(row_id, **{**FULL_SUBMISSION, "passport_expiry": "2025-01-01",
                         "passport_number": "XXXXXXXX"}))
    kw = ack.call_args.kwargs
    assert kw["response_action"] == "errors"
    assert set(kw["errors"]) == {"passport_expiry", "passport_number"}
    assert secure_store.load(uid) == {}
    assert db.get(row_id)["status"] == "awaiting_confirm"
    assert threads == []


def test_modal_valid_submission_saves_and_starts_booking(user, threads, monkeypatch):
    monkeypatch.setattr(bot_app.config, "DRY_RUN", True)
    uid = user("USUBOK")
    row_id = _row(uid)
    ack, client = MagicMock(), MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "CDM"}}
    bot_app._handle_passenger_details(ack, {"user": {"id": uid}}, client,
                                      _view(row_id, **FULL_SUBMISSION))
    ack.assert_called_once_with()
    assert secure_store.load(uid)["passport_number"] == PASSPORT_NO
    assert db.get(row_id)["status"] == "confirmed"
    target, args = threads[0]
    assert target is bot_app.run_booking
    assert args[4]["passport_number"] == PASSPORT_NO  # full profile goes to booking only
    texts = [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]
    assert texts and all(PASSPORT_NO not in t and DOB not in t for t in texts)


def test_modal_submission_from_other_user_is_ignored(user, threads):
    uid = user("UOWNER")
    row_id = _row(uid)
    ack = MagicMock()
    bot_app._handle_passenger_details(ack, {"user": {"id": "UINTRUDER"}}, MagicMock(),
                                      _view(row_id, **FULL_SUBMISSION))
    assert secure_store.load("UINTRUDER") == {} and secure_store.load(uid) == {}
    assert db.get(row_id)["status"] == "awaiting_confirm"


def test_modal_without_key_fails_closed(user, threads, monkeypatch):
    uid = user("UNOKEY2")
    row_id = _row(uid)
    monkeypatch.delenv("TRAVELDESK_DATA_KEY")
    ack = MagicMock()
    bot_app._handle_passenger_details(ack, {"user": {"id": uid}}, MagicMock(),
                                      _view(row_id, **FULL_SUBMISSION))
    assert ack.call_args.kwargs["response_action"] == "errors"
    assert db.get(row_id)["status"] == "awaiting_confirm"


def test_search_works_without_passport_and_hides_sensitive_fields(user, threads):
    uid = user("USEARCH", {**SEARCH_ONLY, "passport_number": PASSPORT_NO, "date_of_birth": DOB})
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "CDM"}}
    client.chat_postMessage.return_value = {"ts": "1.0"}
    bot_app.traveldesk(lambda: None, {"user_id": uid, "text": "DEL to BOM 1 Dec"}, client)
    target, args = threads[0]
    assert target is bot_app.run_search
    sent_to_agent = args[-1]
    assert sent_to_agent["given_name"] == "Raj"
    assert "passport_number" not in sent_to_agent and "date_of_birth" not in sent_to_agent


# --- log hygiene ----------------------------------------------------------------


def test_sensitive_values_never_logged(user, threads, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    uid = user("ULOG")
    row_id = _row(uid)
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "CDM"}}
    bot_app._handle_passenger_details(MagicMock(), {"user": {"id": uid}}, client,
                                      _view(row_id, **FULL_SUBMISSION))
    tp.validate({**VALID, "passport_expiry": "2020-01-01"}, for_booking=True)
    secure_store.load(uid)

    import letsfg_rest
    monkeypatch.setattr(letsfg_rest, "_request", lambda *a, **k: (_ for _ in ()).throw(
        letsfg_rest.RestError(400, {"error": "invalid_fields",
                                    "invalid_fields": ["passport_number"],
                                    "message": f"bad passport {PASSPORT_NO}"})))
    out = letsfg_rest.book_flight("S", "O", tp.validate(VALID, for_booking=True).cleaned, "k")
    assert PASSPORT_NO not in out["message"]

    assert PASSPORT_NO not in caplog.text
    assert DOB not in caplog.text
