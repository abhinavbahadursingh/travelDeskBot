"""DB tests: duplicate-request protection, atomic decision claim, AI usage."""
from __future__ import annotations

import db


def test_new_request_and_update():
    row_id, key = db.new_request("UEMP1", "DEL to BOM", "claude")
    assert row_id > 0
    assert isinstance(key, str) and len(key) > 5
    row = db.get(row_id)
    assert row["status"] == "searching"
    assert row["payment_status"] == "unpaid"
    assert row["provider"] == "claude"


def test_claim_decision_is_single_winner():
    row_id, _ = db.new_request("UEMP1", "DEL to HYD", "groq")
    db.update(row_id, status="awaiting_confirm")
    first = db.claim_decision(row_id, "UEMP1", "confirmed")
    second = db.claim_decision(row_id, "UEMP1", "confirmed")
    assert first is True
    assert second is False
    row = db.get(row_id)
    assert row["status"] == "confirmed"
    assert row["approver"] == "UEMP1"
    assert row["approval_ts"]


def test_claim_rejects_when_not_awaiting():
    row_id, _ = db.new_request("UEMP1", "DEL to GOI", "claude")
    # row is still "searching" → claim must fail
    assert db.claim_decision(row_id, "UEMP1", "confirmed") is False


def test_recent_pending_blocks_duplicate():
    row_id, _ = db.new_request("UEMP1", "DEL to CCU today", "claude")
    db.update(row_id, status="awaiting_confirm")
    dup = db.recent_pending_for("UEMP1", "DEL to CCU today")
    assert dup is not None and dup["id"] == row_id
    assert db.recent_pending_for("UEMP1", "something else") is None


def test_ai_usage_records_and_estimates_cost():
    row_id, _ = db.new_request("UEMP1", "DEL to BOM cost test", "claude")
    usage_id = db.record_ai_usage(
        booking_id=row_id,
        provider="claude",
        model="claude-sonnet-5-5",
        task_kind="pick",
        input_tokens=1000,
        output_tokens=500,
    )
    assert usage_id > 0
    rows = db.usage_for_booking(row_id)
    assert len(rows) == 1
    r = rows[0]
    assert r["model"] == "claude-sonnet-5-5"
    assert r["task_kind"] == "pick"
    assert r["input_tokens"] == 1000
    assert r["output_tokens"] == 500
    # Sonnet pricing: 1k * $3/M + 500 * $15/M = $0.003 + $0.0075 = $0.0105
    assert abs(r["est_cost_usd"] - 0.0105) < 1e-6


def test_ai_usage_haiku_is_cheaper_than_sonnet():
    haiku_cost = db.estimate_cost_usd("claude-haiku-5-5", 1000, 500)
    sonnet_cost = db.estimate_cost_usd("claude-sonnet-5-5", 1000, 500)
    assert 0 < haiku_cost < sonnet_cost


def test_usage_totals_aggregate():
    row_id, _ = db.new_request("UEMP1", "DEL to BOM totals", "claude")
    db.record_ai_usage(booking_id=row_id, provider="claude",
                       model="claude-haiku-5-5", task_kind="classify",
                       input_tokens=200, output_tokens=5)
    db.record_ai_usage(booking_id=row_id, provider="claude",
                       model="claude-sonnet-5-5", task_kind="pick",
                       input_tokens=1500, output_tokens=800)
    totals = db.usage_totals()
    assert totals["calls"] >= 2
    assert totals["input_tokens"] >= 1700
    assert totals["output_tokens"] >= 805
    assert totals["est_cost_usd"] > 0
