"""Tests for the employee self-confirmation handler.

Bypasses the Slack Bolt app and calls `_handle_decision` directly with a
synthesised action body, after stubbing the agent module.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import app as bot_app
import config
import db


def _body(user: str):
    return {
        "user": {"id": user},
        "channel": {"id": "CDM1"},
        "container": {"channel_id": "CDM1", "message_ts": "1.0"},
        "message": {"ts": "1.0", "blocks": [{"type": "actions"}]},
    }


def _action(row_id: int):
    return {"value": json.dumps({"row": row_id})}


def test_only_requesting_employee_can_confirm():
    row_id, _ = db.new_request("UEMP1", "DEL BOM", "claude")
    db.update(row_id, status="awaiting_confirm")
    client = MagicMock()
    # Different user clicks — must be rejected, row unchanged.
    bot_app._handle_decision(_body("UOTHER"), client, _action(row_id), "confirmed")
    row = db.get(row_id)
    assert row["status"] == "awaiting_confirm"
    assert row["approver"] is None
    client.chat_postEphemeral.assert_called()


def test_requesting_employee_can_confirm(monkeypatch):
    row_id, _ = db.new_request("UEMP1", "DEL BOM own", "claude")
    db.update(
        row_id,
        status="awaiting_confirm",
        details={"status": "picked", "summary": "x", "price": 100, "currency": "INR"},
    )
    # Prevent background thread.
    monkeypatch.setattr(bot_app.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: None))
    client = MagicMock()
    bot_app._handle_decision(_body("UEMP1"), client, _action(row_id), "confirmed")
    row = db.get(row_id)
    assert row["status"] == "confirmed"
    assert row["approver"] == "UEMP1"


def test_duplicate_clicks_only_count_once(monkeypatch):
    row_id, _ = db.new_request("UEMP1", "DEL CCU", "claude")
    db.update(
        row_id,
        status="awaiting_confirm",
        details={"status": "picked", "summary": "x", "price": 100, "currency": "INR"},
    )
    monkeypatch.setattr(bot_app.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: None))
    client = MagicMock()
    bot_app._handle_decision(_body("UEMP1"), client, _action(row_id), "confirmed")
    bot_app._handle_decision(_body("UEMP1"), client, _action(row_id), "confirmed")
    row = db.get(row_id)
    assert row["status"] == "confirmed"
    assert row["approver"] == "UEMP1"


def test_cancel_notifies_employee(monkeypatch):
    row_id, _ = db.new_request("UEMP1", "DEL HYD", "claude")
    db.update(row_id, status="awaiting_confirm", details={"summary": "x"})
    # the agent should not be called when cancelling
    monkeypatch.setattr(
        bot_app.agent_factory, "get_agent", lambda: (_ for _ in ()).throw(AssertionError("must not run"))
    )
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "CEMP"}}
    bot_app._handle_decision(_body("UEMP1"), client, _action(row_id), "cancelled")
    row = db.get(row_id)
    assert row["status"] == "cancelled"
    posts = [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]
    assert any("cancelled" in t.lower() for t in posts)


def test_dry_run_never_books(monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", True)

    class FakeAgent:
        @staticmethod
        def confirm(picked, traveller, **_kw):
            return {**picked, "status": "picked", "message": "DRY_RUN", "provider": "claude"}

        @staticmethod
        def booking_status(_ref, **_kw):
            raise AssertionError("should not poll in DRY_RUN path")

    monkeypatch.setattr(bot_app.agent_factory, "get_agent", lambda: FakeAgent)
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "CEMP"}}
    row_id, _ = db.new_request("UEMP1", "DRY RUN", "claude")
    db.update(
        row_id,
        status="awaiting_confirm",
        details={"status": "picked", "summary": "x", "price": 100, "currency": "INR"},
    )
    bot_app.run_booking(
        client,
        row_id,
        "UEMP1",
        {"summary": "x", "price": 100, "currency": "INR"},
        {"given_name": "A", "family_name": "B", "email": "a@b"},
    )
    row = db.get(row_id)
    assert row["status"] == "picked"
    assert row["booking_ref"] is None
