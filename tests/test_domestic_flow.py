"""Domestic-India vs international passenger details.

Synthetic fixtures only. No Slack, LetsFG or payment call is made: Slack is a
MagicMock, LetsFG HTTP is stubbed and booking threads are captured.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from traveldesk.booking import flight_scope, letsfg_booking
from traveldesk.booking import traveller_profile as tp
from traveldesk.clients import letsfg_rest
from traveldesk.slack import app as bot_app
from traveldesk.storage import db, secure_store

TODAY = date(2026, 11, 18)
PASSPORT_NO = "Z9081726"
DOB = "1990-01-02"
BASIC = {
    "given_name": "Raj",
    "family_name": "Gupta",
    "email": "raj@example.com",
    "phone": "+919800000000",
    "date_of_birth": DOB,
    "gender": "m",
}
PASSPORT = {
    "nationality": "IN",
    "passport_number": PASSPORT_NO,
    "passport_country": "IN",
    "passport_expiry": "2032-05-01",
}


def seg(o, d, dep="2026-12-01T07:00:00", arr="2026-12-01T09:00:00", **extra):
    return {"origin": o, "destination": d, "departure": dep, "arrival": arr, **extra}


def offer(*segs, inbound=None):
    o = {"id": "OFF", "outbound": {"segments": list(segs)}, "inbound": None}
    if inbound:
        o["inbound"] = {"segments": list(inbound)}
    return o


# --- is_domestic_india ------------------------------------------------------


def test_del_bom_is_domestic():
    assert flight_scope.is_domestic_india(offer(seg("DEL", "BOM"))) is True


def test_del_dxb_is_international():
    assert flight_scope.is_domestic_india(offer(seg("DEL", "DXB"))) is False


def test_multi_segment_ending_abroad_is_international():
    assert flight_scope.is_domestic_india(offer(seg("BOM", "DEL"), seg("DEL", "SIN"))) is False


def test_unknown_airport_country_is_international():
    assert flight_scope.is_domestic_india(offer(seg("DEL", "ZZZ"))) is False


def test_return_leg_abroad_is_international():
    assert (
        flight_scope.is_domestic_india(
            offer(seg("DEL", "BOM"), inbound=[seg("BOM", "DXB"), seg("DXB", "DEL")])
        )
        is False
    )


def test_explicit_segment_country_wins():
    assert flight_scope.is_domestic_india(offer(seg("QQQ", "BOM", origin_country="IN"))) is True
    assert flight_scope.is_domestic_india(offer(seg("DEL", "BOM", destination_country="AE"))) is False


def test_no_segments_is_international():
    assert flight_scope.is_domestic_india({"id": "OFF"}) is False
    assert flight_scope.is_domestic_india(None) is False


def test_last_flight_date_uses_return_leg():
    o = offer(seg("DEL", "DXB"), inbound=[seg("DXB", "DEL", "2027-01-10T23:00:00", "2027-01-11T04:00:00")])
    assert flight_scope.last_flight_date(o) == "2027-01-11"


def test_claude_rest_pick_records_domestic_flag(monkeypatch):
    from traveldesk.agents import claude_agent

    monkeypatch.setattr(claude_agent.config, "MAX_PRICE", None)
    offers = [{"id": "OFF-1", "price_total": 5000, "outbound": {"segments": [seg("DEL", "BOM")]}}]
    monkeypatch.setattr(
        claude_agent.letsfg_rest,
        "search_flights",
        lambda *a, **k: {"status": "ok", "search_id": "S", "offers": offers, "currency": "INR"},
    )
    replies = iter(
        [
            {
                "status": "ok",
                "origin": "DEL",
                "destination": "BOM",
                "date_from": "2026-12-01",
                "return_from": None,
            },
            {"status": "picked", "offer_id": "OFF-1", "summary": "6E"},
        ]
    )
    monkeypatch.setattr(claude_agent, "_plain_json", lambda *a, **k: next(replies))
    out = claude_agent.pick("DEL to BOM 1 Dec", BASIC)
    assert out["domestic"] is True
    assert out["airports"] == ["DEL", "BOM"]
    assert out["last_flight_date"] == "2026-12-01"


# --- validation -----------------------------------------------------------------


def test_domestic_needs_only_basic_profile():
    res = tp.validate(BASIC, for_booking=True, domestic=True, today=TODAY)
    assert res.ok, res.summary()


def test_domestic_never_returns_passport_fields():
    res = tp.validate({**BASIC, **PASSPORT}, for_booking=True, domestic=True, today=TODAY)
    assert not set(res.cleaned) & set(tp.PASSPORT_FIELDS)


def test_domestic_requires_mobile():
    res = tp.validate({**BASIC, "phone": None}, for_booking=True, domestic=True, today=TODAY)
    assert res.missing == ["phone"]


def test_id_type_only_accepts_listed_types():
    ok = tp.validate({**BASIC, "id_type": "aadhaar"}, for_booking=True, domestic=True, today=TODAY)
    assert ok.ok and ok.cleaned["id_type"] == "aadhaar"
    bad = tp.validate({**BASIC, "id_type": "1234 5678 9012"}, for_booking=True, domestic=True, today=TODAY)
    assert "id_type" in bad.invalid


def test_international_still_needs_passport():
    res = tp.validate(BASIC, for_booking=True, today=TODAY)
    assert set(res.missing) == set(tp.PASSPORT_FIELDS) - {"passport_issue"}  # issue date optional


@pytest.mark.parametrize(
    "expiry,ok",
    [
        ("2027-07-09", False),  # under 6 months after the 2027-01-11 return
        ("2027-07-11", True),  # exactly 6 months
        ("2027-09-01", True),
    ],
)
def test_passport_must_cover_six_months_after_last_flight(expiry, ok):
    res = tp.validate(
        {**BASIC, **PASSPORT, "passport_expiry": expiry},
        for_booking=True,
        travel_date="2026-12-01",
        last_flight_date="2027-01-11",
        today=TODAY,
    )
    assert res.ok is ok
    if not ok:
        assert "2027-07-11" in res.invalid["passport_expiry"]


def test_mask_passport_shows_last_three_only():
    assert tp.mask_passport(PASSPORT_NO) == "•••726"
    assert PASSPORT_NO not in tp.mask_passport(PASSPORT_NO)


# --- Slack modal ------------------------------------------------------------------


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
def user(monkeypatch):
    def make(uid, profile, secure=None):
        monkeypatch.setitem(tp.config.TRAVELLERS, uid, dict(profile))
        secure_store.delete(uid)
        if secure:
            secure_store.save(uid, secure)
        return uid

    return make


def _row(user, domestic, last="2026-12-01"):
    row_id, _ = db.new_request(user, f"trip {user}", "claude")
    db.update(
        row_id,
        status="awaiting_confirm",
        manager_channel="CDM",
        manager_message_ts="1.0",
        details={
            "status": "picked",
            "backend": "rest",
            "search_id": "S",
            "offer_id": "O",
            "summary": "6E · DEL → BOM",
            "price": 5000,
            "currency": "INR",
            "depart_date": "2026-12-01",
            "last_flight_date": last,
            "domestic": domestic,
        },
    )
    return row_id


def _confirm(uid, row_id):
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "CDM"}}
    body = {
        "user": {"id": uid},
        "trigger_id": "T1",
        "channel": {"id": "CDM"},
        "container": {"channel_id": "CDM", "message_ts": "1.0"},
        "message": {"ts": "1.0", "blocks": [{"type": "actions"}]},
    }
    bot_app._handle_decision(body, client, {"value": json.dumps({"row": row_id})}, "confirmed")
    return client


def _inputs(client):
    view = client.views_open.call_args.kwargs["view"]
    return view, {b["block_id"]: b for b in view["blocks"] if b["type"] == "input"}


def test_domestic_modal_shows_basic_fields_and_id_type_only(user, threads):
    uid = user("UDOM1", {"given_name": "Raj", "family_name": "Gupta", "email": "raj@example.com"})
    row_id = _row(uid, domestic=True)
    view, inputs = _inputs(_confirm(uid, row_id))
    assert list(inputs) == [
        "given_name",
        "middle_name",
        "family_name",
        "date_of_birth",
        "gender",
        "phone",
        "id_type",
    ]
    assert inputs["middle_name"]["optional"] is True
    assert not any("passport" in f or "nationality" in f for f in inputs)
    assert inputs["given_name"]["element"]["initial_value"] == "Raj"
    assert inputs["id_type"]["optional"] is True
    assert [o["text"]["text"] for o in inputs["id_type"]["element"]["options"]] == [
        "Aadhaar",
        "PAN",
        "Driving Licence",
        "Voter ID",
        "Passport",
    ]
    # No ID-number input of any kind.
    assert all(
        b["element"]["type"] != "plain_text_input"
        or f in ("given_name", "middle_name", "family_name", "phone")
        for f, b in inputs.items()
    )
    intro = view["blocks"][0]["elements"][0]["text"]
    assert (
        "Enter your name exactly as on the ID you'll carry. No ID number is needed "
        "for flights within India." in intro
    )
    assert db.get(row_id)["status"] == "awaiting_confirm"


def test_domestic_with_basic_profile_skips_modal(user, threads):
    uid = user("UDOM2", BASIC)  # no passport at all
    row_id = _row(uid, domestic=True)
    client = _confirm(uid, row_id)
    client.views_open.assert_not_called()
    assert db.get(row_id)["status"] == "confirmed"
    assert threads and threads[0][0] is bot_app.run_booking


def test_international_with_passport_expiring_too_soon_shows_passport_fields_prefilled(user, threads):
    uid = user("UINT1", BASIC, secure={**PASSPORT, "passport_expiry": "2027-03-01"})
    row_id = _row(uid, domestic=False, last="2027-01-11")
    view, inputs = _inputs(_confirm(uid, row_id))
    assert list(inputs) == ["middle_name", *tp.PASSPORT_FIELDS]
    assert inputs["nationality"]["element"]["initial_value"] == "IN"
    assert inputs["passport_expiry"]["element"]["initial_date"] == "2027-03-01"
    assert "6 months" in inputs["passport_expiry"]["hint"]["text"]
    pn = inputs["passport_number"]
    assert pn["optional"] is True and "•••726" in pn["hint"]["text"]
    assert PASSPORT_NO not in json.dumps(view)


def test_international_complete_profile_skips_modal(user, threads):
    uid = user("UINT2", BASIC, secure=PASSPORT)
    row_id = _row(uid, domestic=False)
    _confirm(uid, row_id).views_open.assert_not_called()
    assert db.get(row_id)["status"] == "confirmed"


def _view(row_id, fields, **values):
    state = {}
    for f, v in values.items():
        if f in ("date_of_birth", "passport_expiry"):
            state[f] = {"value": {"type": "datepicker", "selected_date": v}}
        elif f in ("gender", "id_type"):
            state[f] = {"value": {"type": "static_select", "selected_option": {"value": v} if v else None}}
        else:
            state[f] = {"value": {"type": "plain_text_input", "value": v}}
    return {"private_metadata": json.dumps({"row": row_id, "fields": fields}), "state": {"values": state}}


def test_international_expiry_too_soon_is_inline_error(user, threads):
    uid = user("UINT3", BASIC)
    row_id = _row(uid, domestic=False, last="2027-01-11")
    ack = MagicMock()
    bot_app._handle_passenger_details(
        ack,
        {"user": {"id": uid}},
        MagicMock(),
        _view(row_id, list(tp.PASSPORT_FIELDS), **{**PASSPORT, "passport_expiry": "2027-05-01"}),
    )
    kw = ack.call_args.kwargs
    assert kw["response_action"] == "errors"
    assert list(kw["errors"]) == ["passport_expiry"]
    assert "6 months" in kw["errors"]["passport_expiry"]
    assert secure_store.load(uid) == {}
    assert threads == []


def test_international_blank_passport_number_keeps_saved_one(user, threads, monkeypatch):
    monkeypatch.setattr(bot_app.config, "DRY_RUN", True)
    uid = user("UINT4", BASIC, secure={**PASSPORT, "passport_expiry": "2027-03-01"})
    row_id = _row(uid, domestic=False, last="2027-01-11")
    ack, client = MagicMock(), MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "CDM"}}
    bot_app._handle_passenger_details(
        ack,
        {"user": {"id": uid}},
        client,
        _view(
            row_id,
            list(tp.PASSPORT_FIELDS),
            nationality="IN",
            passport_number=None,
            passport_country="IN",
            passport_expiry="2031-01-01",
        ),
    )
    ack.assert_called_once_with()
    saved = secure_store.load(uid)
    assert saved["passport_number"] == PASSPORT_NO and saved["passport_expiry"] == "2031-01-01"
    texts = [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]
    assert any("•••726" in t for t in texts)
    assert all(PASSPORT_NO not in t for t in texts)
    assert db.get(row_id)["status"] == "confirmed"


def test_domestic_submission_stores_basic_and_id_type_only(user, threads, monkeypatch):
    monkeypatch.setattr(bot_app.config, "DRY_RUN", True)
    uid = user("UDOM3", {"given_name": "R", "family_name": "G", "email": "raj@example.com"})
    row_id = _row(uid, domestic=True)
    ack, client = MagicMock(), MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "CDM"}}
    fields = ["given_name", "family_name", "date_of_birth", "gender", "phone", "id_type"]
    bot_app._handle_passenger_details(
        ack,
        {"user": {"id": uid}},
        client,
        _view(
            row_id,
            fields,
            given_name="Raj",
            family_name="Gupta",
            date_of_birth=DOB,
            gender="m",
            phone="+919800000000",
            id_type="aadhaar",
        ),
    )
    ack.assert_called_once_with()
    saved = secure_store.load(uid)
    assert saved == {
        "given_name": "Raj",
        "family_name": "Gupta",
        "date_of_birth": DOB,
        "gender": "m",
        "phone": "+919800000000",
        "id_type": "aadhaar",
    }
    assert tp.load(uid)["given_name"] == "Raj"  # modal value overrides travellers.json
    target, args = threads[0]
    assert target is bot_app.run_booking


def test_domestic_submission_without_id_type_is_fine(user, threads):
    uid = user("UDOM4", {"given_name": "Raj", "family_name": "Gupta", "email": "raj@example.com"})
    row_id = _row(uid, domestic=True)
    ack = MagicMock()
    fields = ["given_name", "family_name", "date_of_birth", "gender", "phone", "id_type"]
    bot_app._handle_passenger_details(
        ack,
        {"user": {"id": uid}},
        MagicMock(),
        _view(
            row_id,
            fields,
            given_name="Raj",
            family_name="Gupta",
            date_of_birth=DOB,
            gender="f",
            phone="+919800000000",
            id_type=None,
        ),
    )
    ack.assert_called_once_with()
    assert "id_type" not in secure_store.load(uid)


def test_secure_store_drops_unknown_fields_like_id_numbers():
    secure_store.save("UIDNUM", {"id_type": "aadhaar", "aadhaar_number": "123412341234"})
    assert secure_store.load("UIDNUM") == {"id_type": "aadhaar"}


# --- booking call ---------------------------------------------------------------------


def _capture_book(monkeypatch):
    sent = {}
    monkeypatch.setattr(
        letsfg_booking.letsfg_rest,
        "book_flight",
        lambda s, o, t, k: sent.update(t) or {"status": "booking_in_progress", "booking_ref": "B"},
    )
    return sent


def test_domestic_booking_sends_no_passport_fields(monkeypatch):
    sent = _capture_book(monkeypatch)
    out = letsfg_booking.book(
        {"backend": "rest", "search_id": "S", "offer_id": "O", "domestic": True, "depart_date": "2026-12-01"},
        {**BASIC, **PASSPORT, "id_type": "pan"},
        None,
    )
    assert out["status"] == "booking_in_progress" and out["domestic"] is True
    assert sent["given_name"] == "Raj" and sent["date_of_birth"] == DOB
    assert not any("passport" in k or k == "nationality" for k in sent)
    body = letsfg_rest.map_passenger(sent)
    assert not any("passport" in k or k == "nationality" for k in body)
    assert "id_type" not in body


def test_international_booking_includes_passport(monkeypatch):
    sent = _capture_book(monkeypatch)
    letsfg_booking.book(
        {
            "backend": "rest",
            "search_id": "S",
            "offer_id": "O",
            "domestic": False,
            "depart_date": "2026-12-01",
        },
        {**BASIC, **PASSPORT},
        None,
    )
    assert sent["passport_number"] == PASSPORT_NO and sent["nationality"] == "IN"


def test_domestic_rejected_for_documents_returns_fields_to_ask_for(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(
        letsfg_rest,
        "_request",
        lambda *a, **k: (_ for _ in ()).throw(
            letsfg_rest.RestError(
                400,
                {
                    "error": "missing_fields",
                    "missing_fields": ["passport_number", "nationality"],
                    "message": f"passport required for {DOB}",
                },
            )
        ),
    )
    out = letsfg_booking.book(
        {"backend": "rest", "search_id": "S", "offer_id": "O", "domestic": True, "depart_date": "2026-12-01"},
        BASIC,
        None,
    )
    assert out["status"] == "need_info"
    assert out["missing_fields"] == ["passport_number", "nationality"]
    assert not out["escalate"]
    assert "travel admin" not in out["message"]
    assert "error_detail" not in out
    assert "passport_number" in caplog.text and "need_info" in caplog.text
    assert DOB not in caplog.text  # server echo of passenger data is scrubbed


# --- confirmation messages -----------------------------------------------------------------


def _run_booking(monkeypatch, domestic):
    monkeypatch.setattr(
        bot_app.agent_factory,
        "get_agent",
        lambda: SimpleNamespace(
            confirm=lambda picked, traveller, booking_id=None: {
                "status": "picked",
                "summary": "6E · DEL → BOM",
                "price": 5000,
                "currency": "INR",
            }
        ),
    )
    uid = "UMSG"
    row_id, _ = db.new_request(uid, "trip", "claude")
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "CDM"}}
    bot_app.run_booking(client, row_id, uid, {"domestic": domestic}, BASIC)
    return " ".join(c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list)


def test_domestic_confirmation_mentions_id_and_digiyatra(monkeypatch):
    text = _run_booking(monkeypatch, True)
    assert "Carry the ID you selected (or use DigiYatra) at the airport." in text


def test_international_confirmation_has_no_id_line(monkeypatch):
    assert "DigiYatra" not in _run_booking(monkeypatch, False)


def test_ticket_issued_message_for_domestic_mentions_id(monkeypatch):
    monkeypatch.setattr(bot_app.config, "POLL_SECONDS", 0)
    monkeypatch.setattr(bot_app.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        bot_app.agent_factory,
        "get_agent",
        lambda: SimpleNamespace(
            booking_status=lambda ref, booking_id=None: {
                "status": "completed",
                "pnr": "ABC123",
                "charged_amount": 5000,
                "currency": "INR",
            }
        ),
    )
    row_id, _ = db.new_request("UTKT", "trip", "claude")
    client = MagicMock()
    bot_app.watch_booking(client, row_id, "CDM", "B1", domestic=True)
    text = client.chat_postMessage.call_args.kwargs["text"]
    assert "ABC123" in text and "DigiYatra" in text
