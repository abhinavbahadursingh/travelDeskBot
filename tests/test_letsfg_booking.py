"""Booking safeguards around the LetsFG REST booking path.

Covers:
  * Missing traveller details short-circuit with a clear message.
  * Idempotency: a second confirm() for the same booking_id does not re-book.
  * The confirmed → booking_started claim only succeeds once.

(Status mapping and "never completed without a PNR" are in test_letsfg_rest.)
No real network calls.
"""
from __future__ import annotations

import sys

import db
import letsfg_booking


def test_validate_traveller_reports_missing_fields():
    err = letsfg_booking.validate_traveller({"given_name": "Raj", "email": "raj@x"})
    assert err and "family_name" in err
    assert letsfg_booking.validate_traveller(None) == "traveller profile is missing"
    assert letsfg_booking.validate_traveller(
        {"given_name": "A", "family_name": "B", "email": "a@b"}) is None


def test_confirm_is_idempotent_for_same_booking_id(monkeypatch):
    """Second call to confirm() for a row with an existing booking_ref must
    not book again — this is the real-world duplicate-click /
    resume-after-restart protection."""
    sys.modules.pop("config", None)
    sys.modules.pop("claude_agent", None)
    import claude_agent  # fresh

    monkeypatch.setattr(claude_agent.config, "DRY_RUN", False)
    row_id, _ = db.new_request("UEMP1", "DEL BOM idem", "claude")
    db.update(row_id, status="confirmed")

    call_count = {"n": 0}

    def fake_book(picked, traveller, booking_id):
        call_count["n"] += 1
        return {"status": "awaiting_approval", "booking_ref": "BK-IDEM",
                "approve_url": "https://pay/BK-IDEM", "message": ""}

    monkeypatch.setattr(claude_agent.letsfg_booking, "book", fake_book)

    picked = {"status": "picked", "backend": "rest", "search_id": "S", "offer_id": "O",
              "summary": "x", "price": 100, "currency": "INR"}
    traveller = {"given_name": "A", "family_name": "B",
                 "email": "a@b", "phone": "+1"}

    out1 = claude_agent.confirm(picked, traveller, booking_id=row_id)
    assert out1["status"] == "awaiting_approval"
    assert call_count["n"] == 1

    # Simulate app.py writing the booking_ref back to the row after the first
    # confirm (same thing `run_booking` does on success).
    db.update(row_id, booking_ref="BK-IDEM", status="awaiting_approval")

    # Second call — must NOT book again.
    out2 = claude_agent.confirm(picked, traveller, booking_id=row_id)
    assert call_count["n"] == 1
    assert out2["booking_ref"] == "BK-IDEM"
    assert "already issued" in out2["message"]

    sys.modules.pop("config", None)
    sys.modules.pop("claude_agent", None)
    sys.modules.pop("agent", None)


def test_claim_booking_start_only_fires_once():
    row_id, _ = db.new_request("UEMP1", "DEL CCU claim", "claude")
    db.update(row_id, status="confirmed")
    assert db.claim_booking_start(row_id) is True
    # Second attempt must fail — the status is no longer 'confirmed'.
    assert db.claim_booking_start(row_id) is False
