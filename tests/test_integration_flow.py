"""End-to-end-ish integration test with every external dependency mocked.

Exercises the full happy path:
  /traveldesk → pick → employee confirmation (Block Kit) → confirm → poll → PNR.

Nothing here hits Slack, Claude, or LetsFG. DRY_RUN is forced off so
confirm() is actually invoked, but the agent module itself is a stub, so no
real booking tool is ever called.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import app as bot_app
import config
import db


class StubAgent:
    """Captures the 'did we actually book' signal without talking to LetsFG."""
    book_calls: list = []
    status_calls: list = []

    @classmethod
    def pick(cls, request, traveller, **_kw):
        return {
            "status": "picked",
            "summary": "6E 123 · 07:30 DEL → 09:50 BOM · nonstop",
            "price": 4200,
            "currency": "INR",
            "search_id": "S42",
            "offer_id": "O7",
            "offer_link": "https://letsfg.co/offer/O7",
            "provider": "claude",
        }

    @classmethod
    def confirm(cls, picked, traveller, **_kw):
        cls.book_calls.append({"picked": picked, "traveller": traveller})
        return {
            **picked,
            "status": "awaiting_approval",
            "booking_ref": "BK-123",
            "approve_url": "https://letsfg.co/pay/BK-123",
            "provider": "claude",
        }

    @classmethod
    def booking_status(cls, booking_ref, **_kw):
        cls.status_calls.append(booking_ref)
        if len(cls.status_calls) == 1:
            return {"status": "booking_in_progress", "provider": "claude"}
        return {
            "status": "completed",
            "pnr": "ABC123",
            "total": 4200,
            "currency": "INR",
            "ticket_url": "https://letsfg.co/ticket/ABC123",
            "provider": "claude",
        }


def _slack_body(user: str):
    return {
        "user": {"id": user},
        "channel": {"id": "CDM"},
        "container": {"channel_id": "CDM", "message_ts": "9.0"},
        "message": {"ts": "9.0", "blocks": [{"type": "actions"}]},
    }


def _action(row_id: int):
    return {"value": json.dumps({"row": row_id})}


def test_full_happy_path(monkeypatch):
    StubAgent.book_calls.clear()
    StubAgent.status_calls.clear()

    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "POLL_SECONDS", 0)
    monkeypatch.setattr(config, "POLL_MAX_MINUTES", 1)
    monkeypatch.setattr(bot_app.agent_factory, "get_agent", lambda: StubAgent)

    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "CDM"}}
    client.chat_postMessage.return_value = {"ts": "1.0", "channel": "CDM"}

    # --- Phase 1: search + pick, post confirmation card to the employee
    bot_app.run_search(client, "CDM", "1.0", "UEMP1",
                       "DEL to BOM Tue morning",
                       {"given_name": "A", "family_name": "B",
                        "email": "a@b", "phone": "+1"})

    with db._conn() as c:
        c.row_factory = __import__("sqlite3").Row
        row = dict(c.execute(
            "SELECT * FROM bookings WHERE slack_user=? AND status='awaiting_confirm' "
            "ORDER BY id DESC LIMIT 1", ("UEMP1",),
        ).fetchone())
    row_id = row["id"]
    assert row["price"] == 4200
    assert row["currency"] == "INR"

    # The employee must have seen exactly one confirmation card.
    card_posts = [
        c for c in client.chat_postMessage.call_args_list
        if c.kwargs.get("blocks")
    ]
    assert len(card_posts) == 1

    # Still nothing booked yet
    assert StubAgent.book_calls == []

    # --- Phase 2: employee confirms
    captured_thread_args = {}

    def fake_thread(target=None, args=(), daemon=None, **_kw):
        captured_thread_args["target"] = target
        captured_thread_args["args"] = args
        return SimpleNamespace(start=lambda: None)

    monkeypatch.setattr(bot_app.threading, "Thread", fake_thread)
    bot_app._handle_decision(_slack_body("UEMP1"), client, _action(row_id), "confirmed")

    row = db.get(row_id)
    assert row["status"] == "confirmed"
    assert row["approver"] == "UEMP1"
    assert row["approval_ts"]

    # --- Phase 3: run the booking job that was scheduled
    assert captured_thread_args["target"] is bot_app.run_booking
    bot_app.run_booking(*captured_thread_args["args"])

    assert len(StubAgent.book_calls) == 1

    row = db.get(row_id)
    assert row["booking_ref"] == "BK-123"
    assert row["status"] == "completed"
    assert row["pnr"] == "ABC123"
    assert row["payment_status"] == "paid"

    assert StubAgent.status_calls == ["BK-123", "BK-123"]

    sent_texts = [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]
    assert any("ABC123" in t for t in sent_texts)


def test_approve_url_sets_awaiting_payment(monkeypatch):
    StubAgent.book_calls.clear()
    StubAgent.status_calls.clear()

    class AwaitingAgent(StubAgent):
        @classmethod
        def booking_status(cls, booking_ref, **_kw):
            return {"status": "awaiting_approval", "provider": "claude"}

    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "POLL_SECONDS", 0)
    monkeypatch.setattr(config, "POLL_MAX_MINUTES", 0)
    monkeypatch.setattr(bot_app.agent_factory, "get_agent", lambda: AwaitingAgent)

    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "CDM"}}
    client.chat_postMessage.return_value = {"ts": "1.0", "channel": "CDM"}

    row_id, _ = db.new_request("UEMP1", "DEL CCU awaiting", "claude")
    db.update(row_id, status="awaiting_confirm",
              details={"status": "picked", "search_id": "S", "offer_id": "O",
                       "summary": "x", "price": 100, "currency": "INR"})
    bot_app.run_booking(client, row_id, "UEMP1",
                        {"status": "picked", "search_id": "S", "offer_id": "O",
                         "summary": "x", "price": 100, "currency": "INR"},
                        {"given_name": "A", "family_name": "B",
                         "email": "a@b", "phone": "+1"})
    row = db.get(row_id)
    assert row["payment_status"] == "awaiting_payment"
    assert row["pnr"] is None
    assert row["status"] == "timeout"


def test_startup_resume_picks_up_booking_ref(monkeypatch):
    row_id, _ = db.new_request("UEMP1", "DEL HYD resume", "claude")
    db.update(row_id, status="awaiting_approval", booking_ref="BK-RESUME")

    spawned = []

    def fake_thread(target=None, args=(), daemon=None, **_kw):
        spawned.append({"target": target, "args": args})
        return SimpleNamespace(start=lambda: None)

    monkeypatch.setattr(bot_app.threading, "Thread", fake_thread)
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "CEMP"}}
    bot_app._resume_unfinished_bookings(client)
    targets = [s["target"] for s in spawned]
    assert bot_app.watch_booking in targets
    for s in spawned:
        if s["target"] is bot_app.watch_booking and "BK-RESUME" in s["args"]:
            break
    else:
        raise AssertionError("resumed watcher for BK-RESUME not found")
