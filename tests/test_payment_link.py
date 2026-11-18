"""Pay by link: card declined (or none saved) → LetsFG secure payment page.

Every state transition, restart-resume and "never a second /flights/book".
LetsFG HTTP and Slack are stubbed; nothing leaves the machine.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.test_need_info_flow import Script, client, picked  # noqa: F401 — fixtures
from traveldesk import agents, config
from traveldesk.booking import letsfg_booking
from traveldesk.clients import letsfg_rest
from traveldesk.slack import app as bot_app
from traveldesk.storage import db

TRAVELLER = "UPAYTRAV"
APPROVER = "UPAYADMIN"
PAY_URL = "https://checkout.revolut.com/payment-link/abc"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
LONG_DECLINE = "Your payment was declined (law violation). " + "Details. " * 80 + "END-OF-DECLINE"


def declined_body(valid_until=None):
    retry = {"booking_id": "dev_1_3ds", "pay_url": PAY_URL, "amount": 116.0, "currency": "USD"}
    if valid_until is not None:
        retry["valid_until"] = valid_until
    return {
        "error": "payment_declined",
        "booking_id": "dev_1",
        "decline_reason": "law_violation",
        "decline_message": "Your payment was declined.",
        "decline_advice": "Pay another way on the secure page.",
        "pay_url": PAY_URL,
        "retry_booking_id": "dev_1_3ds",
        "retry": retry,
        "message": LONG_DECLINE,
    }


def link_result(**extra):
    out = letsfg_rest.payment_link(declined_body(), kind="payment_declined")
    out.update(extra)
    return out


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    now = {"t": NOW}
    monkeypatch.setattr(bot_app, "_utcnow", lambda: now["t"])
    monkeypatch.setattr(bot_app.time, "sleep", lambda s: None)
    return now


@pytest.fixture
def approver(monkeypatch):
    monkeypatch.setattr(config, "PAYMENT_APPROVERS", [APPROVER])


@pytest.fixture
def no_approver(monkeypatch):
    monkeypatch.setattr(config, "PAYMENT_APPROVERS", [])


@pytest.fixture
def statuses(monkeypatch):
    """Script the LetsFG status answers the payment watcher sees."""
    answers, calls = [], []

    def booking_status(ref, booking_id=None):
        calls.append(ref)
        if not answers:
            # pytest.fail is a BaseException: the watchers' "keep polling"
            # except-clauses can't swallow it, so a bug can't loop forever.
            pytest.fail("unexpected status call")
        nxt = answers.pop(0)
        return nxt() if callable(nxt) else nxt

    monkeypatch.setattr(agents, "get_agent", lambda: SimpleNamespace(booking_status=booking_status))
    return SimpleNamespace(answers=answers, calls=calls)


def new_row(**fields):
    rid, _ = db.new_request(TRAVELLER, "DEL to MAA 23 Nov afternoon", "claude")
    db.update(rid, status="booking_started", details=picked(domestic=True), **fields)
    return rid


def pay_row(*, started=NOW, expires=NOW + timedelta(minutes=60), approvers=APPROVER, **fields):
    rid = new_row()
    db.update(
        rid,
        status="awaiting_payment",
        booking_ref="dev_1_3ds",
        payment_status="awaiting_payment",
        pay_url=PAY_URL,
        declined_booking_ref="dev_1",
        pay_started_at=started.isoformat(),
        pay_expires_at=expires.isoformat(),
        pay_approvers=approvers,
        **fields,
    )
    return rid


def posts(client, channel=None):
    return [
        c.kwargs
        for c in client.chat_postMessage.call_args_list
        if channel is None or c.kwargs.get("channel") == channel
    ]


def texts(client, channel=None):
    return [p.get("text", "") for p in posts(client, channel)]


def buttons(post):
    return [e for b in post.get("blocks") or [] if b["type"] == "actions" for e in b["elements"]]


# --- REST parsing ----------------------------------------------------------------------


def test_402_decline_with_retry_is_a_pay_link():
    out = letsfg_rest._error_result(letsfg_rest.RestError(402, declined_body(1795000000)), phase="book")
    assert out["status"] == "payment_link" and out["payment_kind"] == "payment_declined"
    assert out["booking_ref"] == "dev_1_3ds" and out["declined_booking_ref"] == "dev_1"
    assert out["pay_url"] == PAY_URL and out["pay_valid_until"] == 1795000000.0
    assert out["pay_amount"] == 116.0 and out["pay_currency"] == "USD"
    assert out["message"].endswith("END-OF-DECLINE")  # full text, no 500-char cut


def test_402_payment_method_required_with_pay_url_is_an_upfront_link():
    body = {
        "error": "payment_method_required",
        "booking_id": "dev_2",
        "pay_url": PAY_URL,
        "poll_url": "https://letsfg.co/developers/api/v1/flights/bookings/dev_2",
    }
    out = letsfg_rest._error_result(letsfg_rest.RestError(402, body), phase="book")
    assert out["status"] == "payment_link" and out["payment_kind"] == "payment_required"
    assert out["booking_ref"] == "dev_2" and out["declined_booking_ref"] is None


def test_402_decline_without_link_is_failed():
    body = {"error": "payment_declined", "decline_message": "Insufficient funds."}
    out = letsfg_rest._error_result(letsfg_rest.RestError(402, body), phase="book")
    assert out["status"] == "failed" and out["payment_declined"] and "Insufficient" in out["message"]


def test_409_too_many_attempts_is_payment_blocked_not_price_change():
    e = letsfg_rest.RestError(409, {"error": "too_many_attempts", "message": "declined 5 times"})
    assert letsfg_rest._error_result(e, phase="book") == {
        "status": "payment_blocked",
        "message": "declined 5 times",
    }
    other = letsfg_rest._error_result(letsfg_rest.RestError(409, {"error": "x"}), phase="book")
    assert other.get("price_changed") is True  # unchanged existing mapping


def test_202_awaiting_payment_with_pay_url_is_a_pay_link(monkeypatch):
    monkeypatch.setattr(
        letsfg_rest,
        "_request",
        lambda *a, **k: {"booking_id": "dev_3", "state": "awaiting_payment", "pay_url": PAY_URL},
    )
    out = letsfg_rest.book_flight("S", "O", {"given_name": "A", "family_name": "B", "email": "a@b"}, "k")
    assert out["status"] == "payment_link" and out["booking_ref"] == "dev_3"


@pytest.mark.parametrize(
    "body,status",
    [
        ({"state": "failed", "retry": {"booking_id": "dev_1_3ds", "pay_url": PAY_URL}}, "payment_link"),
        (
            {"state": "awaiting_payment", "pay_url": PAY_URL, "awaiting_link_payment": True},
            "awaiting_payment",
        ),
        ({"state": "failed", "failure_reason": "expired"}, "failed"),
        ({"state": "authorised"}, "booking_in_progress"),
    ],
)
def test_status_mapping_for_pay_links(monkeypatch, body, status):
    monkeypatch.setattr(letsfg_rest, "_request", lambda *a, **k: body)
    out = letsfg_rest.get_booking("dev_1")
    assert out["status"] == status
    if status == "payment_link":
        assert out["booking_ref"] == "dev_1_3ds" and out["pay_url"] == PAY_URL


def test_status_decline_message_is_not_cut(monkeypatch):
    monkeypatch.setattr(letsfg_rest, "_request", lambda *a, **k: {"state": "failed", "message": LONG_DECLINE})
    assert letsfg_rest.get_booking("x")["message"].endswith("END-OF-DECLINE")


def test_transient_status_error_is_flagged(monkeypatch):
    def boom(*a, **k):
        raise letsfg_rest.RestError(503, {"error": "busy"})

    monkeypatch.setattr(letsfg_rest, "_request", boom)
    assert letsfg_rest.get_booking("x")["transient"] is True


# --- decline → "payment needs approval" ---------------------------------------------


def test_pay_link_goes_to_approver_and_traveller_waits(client, approver, monkeypatch):
    watched = []
    monkeypatch.setattr(bot_app, "watch_payment", lambda c, r: watched.append(r))
    rid = new_row()
    result = link_result(pay_valid_until=(NOW + timedelta(minutes=30)).timestamp())
    bot_app._handle_book_result(client, rid, TRAVELLER, "DTRAV", picked(domestic=True), result)

    r = db.get(rid)
    assert r["status"] == "awaiting_payment" and r["payment_status"] == "awaiting_payment"
    assert r["booking_ref"] == "dev_1_3ds" and r["declined_booking_ref"] == "dev_1"
    assert r["pay_url"] == PAY_URL and r["pay_approvers"] == APPROVER
    assert r["pay_expires_at"] == (NOW + timedelta(minutes=30)).isoformat()  # LetsFG valid_until
    admin = posts(client, f"D-{APPROVER}")
    assert len(admin) == 1 and "Payment needs approval" in admin[0]["text"]
    pay = buttons(admin[0])
    assert pay[0]["url"] == PAY_URL and pay[0]["text"]["text"] == "Pay now"
    trav = texts(client, "DTRAV")
    assert len(trav) == 1 and "Waiting for payment approval" in trav[0]
    assert not any("Booking started" in t for t in texts(client))  # skipped on a 402
    assert watched == [rid]


def test_without_approver_the_traveller_gets_the_link(client, no_approver, monkeypatch):
    monkeypatch.setattr(bot_app, "watch_payment", lambda c, r: None)
    rid = new_row()
    bot_app._handle_book_result(client, rid, TRAVELLER, "DTRAV", picked(domestic=True), link_result())
    mine = posts(client, f"D-{TRAVELLER}")
    assert len(mine) == 1 and buttons(mine[0])[0]["url"] == PAY_URL
    assert texts(client, "DTRAV") == []  # no separate "waiting" message
    # no valid_until from LetsFG → fallback timeout
    assert (
        db.get(rid)["pay_expires_at"]
        == (NOW + timedelta(minutes=config.PAY_LINK_TIMEOUT_MINUTES)).isoformat()
    )


def test_full_decline_message_is_logged(client, approver, monkeypatch, caplog):
    monkeypatch.setattr(bot_app, "watch_payment", lambda c, r: None)
    with caplog.at_level(logging.WARNING, logger="traveldesk"):
        bot_app._handle_book_result(client, new_row(), TRAVELLER, "D", picked(domestic=True), link_result())
    line = next(r.getMessage() for r in caplog.records if "payment needs approval" in r.getMessage())
    assert "END-OF-DECLINE" in line and "law_violation" in line


def test_link_without_url_is_failed_not_awaiting(client, approver, monkeypatch):
    monkeypatch.setattr(bot_app, "watch_payment", lambda c, r: pytest.fail("no link to watch"))
    rid = new_row()
    bot_app._handle_book_result(client, rid, TRAVELLER, "D", picked(), link_result(pay_url=None))
    assert db.get(rid)["status"] == "failed"


def test_decline_while_polling_switches_to_the_pay_link(client, approver, monkeypatch, statuses):
    """The real case: /flights/book said 202, the hold was declined later."""
    started = []
    monkeypatch.setattr(bot_app, "watch_payment", lambda c, r: started.append(r))
    monkeypatch.setattr(config, "POLL_MAX_MINUTES", 1)
    rid = new_row(booking_ref="dev_1")
    statuses.answers.append({**link_result(), "state": "failed"})
    bot_app.watch_booking(client, rid, "DTRAV", "dev_1")
    r = db.get(rid)
    assert r["status"] == "awaiting_payment" and r["booking_ref"] == "dev_1_3ds"
    assert started == [rid] and statuses.calls == ["dev_1"]


# --- never a second /flights/book -------------------------------------------------------


def test_no_second_book_call_while_a_pay_link_is_open(monkeypatch):
    s = Script(monkeypatch)  # any LetsFG call fails the test
    rid = pay_row()
    out = letsfg_booking.book(picked(domestic=True), {"given_name": "A"}, rid)
    assert s.calls == [] and out["status"] == "awaiting_payment"


def test_confirm_cannot_restart_a_pay_link_row(monkeypatch):
    from traveldesk.agents import claude_agent

    monkeypatch.setattr(claude_agent.config, "DRY_RUN", False)
    monkeypatch.setattr(claude_agent.letsfg_booking, "book", lambda *a: pytest.fail("booked again"))
    rid = pay_row()
    out = claude_agent.confirm(picked(), {"given_name": "A", "family_name": "B", "email": "a@b"}, rid)
    assert out["booking_ref"] == "dev_1_3ds"


def test_book_402_end_to_end_makes_exactly_one_book_call(client, approver, monkeypatch):
    s = Script(monkeypatch, letsfg_rest.RestError(402, declined_body()))
    monkeypatch.setattr(bot_app, "watch_payment", lambda c, r: None)
    rid = new_row()
    traveller = {
        "given_name": "Raj",
        "family_name": "Gupta",
        "email": "r@example.com",
        "phone": "+919800000000",
        "date_of_birth": "1990-01-02",
        "gender": "m",
    }
    result = letsfg_booking.book(picked(domestic=True), traveller, rid)
    bot_app._handle_book_result(client, rid, TRAVELLER, "D", picked(domestic=True), result)
    assert len(s.calls) == 1 and db.get(rid)["status"] == "awaiting_payment"
    letsfg_booking.book(picked(domestic=True), traveller, rid)  # e.g. a retry thread
    assert len(s.calls) == 1


# --- following the link --------------------------------------------------------------


def test_paid_link_hands_over_to_booking_watch(client, approver, monkeypatch, statuses):
    watched = []
    monkeypatch.setattr(bot_app, "watch_booking", lambda *a, **k: watched.append(a[3]))
    rid = pay_row()
    statuses.answers += [
        {"status": "awaiting_payment"},
        {"status": "booking_in_progress", "transient": True},  # a failed check, not a state
        {"status": "booking_in_progress"},
    ]
    bot_app.watch_payment(client, rid)
    r = db.get(rid)
    assert r["status"] == "booking_in_progress" and r["payment_status"] == "held"
    assert watched == ["dev_1_3ds"] and len(statuses.calls) == 3
    assert any("Payment received" in t for t in texts(client, f"D-{APPROVER}"))
    assert any("Payment received" in t for t in texts(client, f"D-{TRAVELLER}"))


@pytest.mark.parametrize(
    "answer,final",
    [
        ({"status": "failed", "message": "The payment link expired."}, "expired"),
        ({"status": "failed", "message": "Payment refused by the bank."}, "failed"),
        ({"status": "approval_expired"}, "expired"),
    ],
)
def test_letsfg_closing_the_link_stops_at_once(client, approver, statuses, answer, final):
    rid = pay_row()
    statuses.answers.append(answer)
    bot_app.watch_payment(client, rid)
    assert db.get(rid)["status"] == final and len(statuses.calls) == 1
    mine = posts(client, f"D-{TRAVELLER}")
    assert buttons(mine[-1])[0]["action_id"] == "search_again"
    assert "Nothing was booked or charged" in mine[-1]["text"]
    assert texts(client, f"D-{APPROVER}")  # approver told too


def test_timeout_sends_one_reminder_then_expires(client, approver, statuses, clock):
    """Halfway → one reminder; LetsFG still waiting at the expiry → expired, no extra call."""
    rid = pay_row(started=NOW - timedelta(minutes=40), expires=NOW + timedelta(minutes=20))

    def waiting_then_time_passes():
        clock["t"] = NOW + timedelta(minutes=21)
        return {"status": "awaiting_payment"}

    statuses.answers += [{"status": "awaiting_payment"}, waiting_then_time_passes]
    bot_app.watch_payment(client, rid)
    r = db.get(rid)
    assert r["status"] == "expired" and r["pay_reminded_at"]
    assert len(statuses.calls) == 2  # stopped at the expiry, no 3rd check
    assert len([t for t in texts(client, f"D-{APPROVER}") if "Reminder" in t]) == 1
    mine = posts(client, f"D-{TRAVELLER}")
    assert "expired" in mine[-1]["text"] and buttons(mine[-1])[0]["action_id"] == "search_again"


def test_letsfg_needs_attention_stops_and_tells_both(client, approver, statuses):
    rid = pay_row()
    statuses.answers.append({"status": "needs_attention", "message": "checking"})
    bot_app.watch_payment(client, rid)
    assert db.get(rid)["status"] == "needs_attention"
    assert texts(client, f"D-{APPROVER}") and texts(client, f"D-{TRAVELLER}")


def test_completed_after_payment_posts_pnr_in_the_original_thread(client, approver, monkeypatch, statuses):
    monkeypatch.setattr(config, "POLL_MAX_MINUTES", 1)
    rid = pay_row(manager_channel="DCARD", manager_message_ts="111.222")
    db.update(rid, status="booking_in_progress")
    statuses.answers.append(
        {"status": "completed", "pnr": "ABC123", "charged_amount": 116.0, "currency": "USD"}
    )
    bot_app.watch_booking(client, rid, "DTRAV", "dev_1_3ds", domestic=True)
    r = db.get(rid)
    assert r["status"] == "completed" and r["pnr"] == "ABC123"
    thread = [p for p in posts(client, "DCARD") if p.get("thread_ts") == "111.222"]
    assert len(thread) == 1 and "ABC123" in thread[0]["text"]
    assert picked()["summary"] in thread[0]["text"]
    assert any("ABC123" in t for t in texts(client, f"D-{APPROVER}"))


# --- restart-resume ------------------------------------------------------------------


def test_restart_resumes_pay_link_rows_with_the_payment_watcher(client, monkeypatch):
    started = []
    monkeypatch.setattr(
        bot_app.threading,
        "Thread",
        lambda target=None, args=(), kwargs=None, **k: SimpleNamespace(
            start=lambda: started.append((target, args))
        ),
    )
    pay = pay_row()
    plain = new_row(booking_ref="dev_plain")
    db.update(plain, status="booking_in_progress")
    bot_app._resume_unfinished_bookings(client)
    by_row = {a[1]: t for t, a in started}
    assert by_row[pay] is bot_app.watch_payment
    assert by_row[plain] is bot_app.watch_booking


def test_resumed_watch_uses_the_stored_expiry_and_reminder(client, approver, statuses, clock):
    rid = pay_row(
        started=NOW - timedelta(minutes=50),
        expires=NOW + timedelta(minutes=10),
        pay_reminded_at=(NOW - timedelta(minutes=20)).isoformat(),
    )
    statuses.answers.append({"status": "awaiting_payment"})

    def later():
        clock["t"] = NOW + timedelta(minutes=11)
        return {"status": "awaiting_payment"}

    statuses.answers[:] = [later]
    bot_app.watch_payment(client, rid)
    assert db.get(rid)["status"] == "expired"
    assert not [t for t in texts(client, f"D-{APPROVER}") if "Reminder" in t]  # already sent before restart


def test_expired_rows_are_not_resumed():
    rid = pay_row()
    db.update(rid, status="expired")
    assert rid not in [r["id"] for r in db.unfinished_bookings()]


# --- 409 too_many_attempts ---------------------------------------------------------------


def test_payment_blocked_tells_the_approver_clearly(client, approver):
    rid = new_row()
    bot_app._handle_book_result(
        client,
        rid,
        TRAVELLER,
        "DTRAV",
        picked(),
        {"status": "payment_blocked", "message": "declined 5 times; connect another method"},
    )
    assert db.get(rid)["status"] == "payment_blocked"
    admin = texts(client, f"D-{APPROVER}")
    assert len(admin) == 1 and "Payment blocked by LetsFG" in admin[0] and "5 times" in admin[0]
    assert "connect another payment method" in admin[0]
    trav = posts(client, "DTRAV")
    assert "Nothing was booked or charged" in trav[0]["text"]
    assert buttons(trav[0])[0]["action_id"] == "search_again"


# --- search again ---------------------------------------------------------------------


def test_search_again_runs_once_for_the_owner(client, monkeypatch):
    calls = []
    monkeypatch.setattr(bot_app, "start_request", lambda c, u, t, d: calls.append((u, t, d)))
    rid = pay_row()
    db.update(rid, status="expired")
    action = {"value": json.dumps({"row": rid})}
    bot_app._handle_search_again({"user": {"id": "UOTHER"}}, client, action)
    assert calls == []
    body = {"user": {"id": TRAVELLER}, "channel": {"id": "DTRAV"}}
    bot_app._handle_search_again(body, client, action)
    bot_app._handle_search_again(body, client, action)  # double click
    assert calls == [(TRAVELLER, "DEL to MAA 23 Nov afternoon", "DTRAV")]
    assert db.get(rid)["status"] == "replaced"


def test_pay_now_click_is_acknowledged():
    ack = MagicMock()
    bot_app.on_pay_now(ack)
    ack.assert_called_once_with()
