"""Cancelled from the web dashboard: the bot stops and tells the traveller.

The dashboard only writes status='cancelled', approver='dashboard'. Slack and
LetsFG are stubbed; nothing leaves the machine.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.test_need_info_flow import admins, client, picked  # noqa: F401 — fixtures
from tests.test_payment_link import (  # noqa: F401 — fixtures
    APPROVER,
    NOW,
    TRAVELLER,
    approver,
    clock,
    pay_row,
    statuses,
    texts,
)
from traveldesk import agents
from traveldesk.slack import app as bot_app
from traveldesk.storage import db


def dash_cancel(rid):
    """What the dashboard's API writes."""
    db.update(rid, status="cancelled", approver=db.DASHBOARD, approval_ts="2026-11-20T12:00:00")


def new_row(status="searching", **fields):
    rid, _ = db.new_request(TRAVELLER, "DEL to BOM next sunday", "claude", thread_ts="100.1")
    db.update(rid, status=status, details=picked(), **fields)
    return rid


# --- the DB keeps a cancel final ----------------------------------------------------


def test_status_writes_cannot_revive_a_cancelled_row():
    rid = new_row()
    dash_cancel(rid)
    db.update(rid, status="awaiting_confirm", price=123.0)
    r = db.get(rid)
    assert r["status"] == "cancelled" and r["price"] == 123.0  # other fields still recorded
    assert db.is_cancelled(rid)


def test_status_writes_still_work_on_live_rows():
    rid = new_row()
    db.update(rid, status="awaiting_confirm")
    assert db.get(rid)["status"] == "awaiting_confirm" and not db.is_cancelled(rid)


# --- background steps stop ------------------------------------------------------------


def test_search_cancelled_mid_pick_posts_no_card(client, monkeypatch):
    def pick(text, traveller, booking_id=None):
        dash_cancel(booking_id)
        return picked()

    monkeypatch.setattr(agents, "get_agent", lambda: SimpleNamespace(pick=pick))
    bot_app.run_search(client, "DTRAV", "100.1", TRAVELLER, "DEL to BOM", {})
    rows = [r for r in db.dashboard_cancellations() if r["slack_user"] == TRAVELLER]
    assert rows and rows[-1]["status"] == "cancelled"
    client.chat_postMessage.assert_not_called()


def test_book_result_after_cancel_is_silent(client):
    rid = new_row("confirmed")
    dash_cancel(rid)
    bot_app._handle_book_result(client, rid, TRAVELLER, "DTRAV", picked(), {"status": "cancelled"})
    assert db.get(rid)["status"] == "cancelled"
    client.chat_postMessage.assert_not_called()


def test_booking_started_despite_cancel_goes_to_the_admin(client, admins):
    rid = new_row("confirmed")
    dash_cancel(rid)
    result = {"status": "awaiting_approval", "booking_ref": "BK9"}
    bot_app._handle_book_result(client, rid, TRAVELLER, "DTRAV", picked(), result)
    r = db.get(rid)
    assert r["status"] == "cancelled" and r["booking_ref"] == "BK9"
    assert any("BK9" in t for t in texts(client, "D-UADMIN"))
    assert not texts(client, "DTRAV")  # no "Booking started"


def test_no_pay_link_is_sent_for_a_cancelled_row(client, approver):
    rid = new_row("booking_started")
    dash_cancel(rid)
    bot_app._start_payment_link(
        client, rid, TRAVELLER, "DTRAV", picked(), {"pay_url": "https://pay", "booking_ref": "B1"}
    )
    assert db.get(rid)["pay_url"] is None
    client.chat_postMessage.assert_not_called()


def test_alternatives_search_stops_after_cancel(client, monkeypatch):
    rid = new_row("no_passport")

    def search(p):
        dash_cancel(rid)
        return {"status": "ok", "offers": []}

    monkeypatch.setattr(bot_app, "_search_same_route", search)
    bot_app.find_alternatives(client, rid, TRAVELLER)
    assert db.get(rid)["status"] == "cancelled"
    client.chat_postMessage.assert_not_called()


# --- a cancelled pay link is still watched until it closes --------------------------


def test_cancelled_pay_link_that_closes_ends_quietly(client, approver, statuses):
    rid = pay_row()
    dash_cancel(rid)
    statuses.answers += [{"status": "awaiting_payment"}, {"status": "failed", "message": "expired"}]
    bot_app.watch_payment(client, rid)
    assert db.get(rid)["status"] == "cancelled" and len(statuses.calls) == 2
    client.chat_postMessage.assert_not_called()


def test_cancelled_pay_link_paid_anyway_alerts_admin_and_approver(client, approver, admins, statuses):
    rid = pay_row()
    dash_cancel(rid)
    statuses.answers.append({"status": "booking_in_progress"})
    bot_app.watch_payment(client, rid)
    r = db.get(rid)
    assert r["status"] == "cancelled" and r["payment_status"] == "held"
    assert any("paid afterwards" in t for t in texts(client, "D-UADMIN"))
    assert any("paid afterwards" in t for t in texts(client, f"D-{APPROVER}"))
    assert not any("Payment received" in t for t in texts(client))


def test_cancelled_pay_link_stops_watching_at_expiry_without_a_reminder(client, approver, statuses, clock):
    rid = pay_row(started=NOW - timedelta(minutes=50), expires=NOW + timedelta(minutes=10))
    dash_cancel(rid)

    def waiting_then_time_passes():
        clock["t"] = NOW + timedelta(minutes=11)
        return {"status": "awaiting_payment"}

    statuses.answers.append(waiting_then_time_passes)
    bot_app.watch_payment(client, rid)
    assert db.get(rid)["pay_reminded_at"] is None
    client.chat_postMessage.assert_not_called()


def test_payment_received_mid_poll_after_cancel_is_escalated(client, approver, admins, statuses):
    rid = pay_row()

    def cancelled_then_paid():
        dash_cancel(rid)
        return {"status": "booking_in_progress"}

    statuses.answers.append(cancelled_then_paid)
    bot_app.watch_payment(client, rid)
    assert db.get(rid)["status"] == "cancelled"
    assert any("paid afterwards" in t for t in texts(client, "D-UADMIN"))


# --- the traveller is told once ----------------------------------------------------------


@pytest.fixture
def drained():
    """Report rows cancelled by earlier tests (shared DB) so only this test's rows count."""
    bot_app.notify_dashboard_cancellations(MagicMock())


def test_traveller_is_told_in_the_request_thread_once(client, drained):
    rid = new_row("awaiting_confirm", manager_channel="DCARD", manager_message_ts="200.2")
    dash_cancel(rid)
    assert bot_app.notify_dashboard_cancellations(client) == 1
    mine = [
        c.kwargs for c in client.chat_postMessage.call_args_list if c.kwargs["channel"] == f"D-{TRAVELLER}"
    ]
    assert len(mine) == 1 and mine[0]["thread_ts"] == "100.1"
    assert (
        mine[0]["text"].startswith(f":no_entry: #{rid} ")
        and "cancelled from the travel desk dashboard" in mine[0]["text"]
    )
    assert "Nothing was booked or charged" in mine[0]["text"]
    card = client.chat_update.call_args.kwargs
    assert card["ts"] == "200.2" and not any(b["type"] == "actions" for b in card["blocks"])
    assert db.get(rid)["cancel_notified_at"]
    client.chat_postMessage.reset_mock()
    bot_app.notify_dashboard_cancellations(client)
    client.chat_postMessage.assert_not_called()


def test_pay_link_approvers_are_told_not_to_pay(client, approver, drained):
    rid = pay_row()
    dash_cancel(rid)
    bot_app.notify_dashboard_cancellations(client)
    assert any("Don't pay" in t for t in texts(client, f"D-{APPROVER}"))
    assert any("Don't use its payment link" in t for t in texts(client, f"D-{TRAVELLER}"))
    assert db.get(rid)["payment_status"] == "unpaid"


def test_slack_cancels_are_not_reported_again(client, drained):
    rid = new_row("awaiting_confirm")
    assert db.claim_decision(rid, TRAVELLER, "cancelled")
    bot_app.notify_dashboard_cancellations(client)
    assert db.get(rid)["cancel_notified_at"] is None
    client.chat_postMessage.assert_not_called()


def test_change_flight_on_the_web_says_so(client, drained):
    rid = new_row("awaiting_confirm", manager_channel="DCARD", manager_message_ts="300.3")
    db.update(rid, status="cancelled", approver=db.WEB_CHANGE, approval_ts="2026-11-20T12:00:00")
    assert bot_app.notify_dashboard_cancellations(client) == 1
    (msg,) = texts(client, f"D-{TRAVELLER}")
    assert "Flight changed on the web page" in msg and f"#{rid}" in msg and "dashboard" not in msg
    assert "Replaced" in client.chat_update.call_args.kwargs["text"]
    assert len(msg) < 200  # short
