"""Tests for the Claude Haiku/Sonnet routing and usage tracking.

Every Anthropic call is stubbed; no real API traffic.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def claude_mod(monkeypatch):
    """Reload the Claude agent against a fake Anthropic client."""
    for mod in ("traveldesk.config", "traveldesk.agents.claude_agent"):
        sys.modules.pop(mod, None)
    from traveldesk.agents import claude_agent  # noqa: WPS433 — intentional re-import

    yield claude_agent
    for mod in ("traveldesk.config", "traveldesk.agents.claude_agent", "traveldesk.agents"):
        sys.modules.pop(mod, None)


class _FakeResp:
    def __init__(self, text: str, input_tokens=10, output_tokens=2):
        self.content = [SimpleNamespace(type="text", text=text)]
        self.stop_reason = "end_turn"
        self.usage = SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )


def test_heuristic_flags_multi_city_as_complex(claude_mod):
    # Heuristic alone should match — no API call needed.
    calls = []
    claude_mod._client_singleton = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kw: calls.append(kw) or _FakeResp("simple"))
    )
    result = claude_mod._classify_complexity("multi-city DEL BOM CCU next week")
    assert result == "complex"
    assert calls == []  # heuristic bypassed the API


def test_heuristic_fast_paths_simple_request(claude_mod):
    calls = []
    claude_mod._client_singleton = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kw: calls.append(kw) or _FakeResp("complex"))
    )
    result = claude_mod._classify_complexity("DEL to BOM tomorrow morning")
    # Short structured one-way → simple without calling the API.
    assert result == "simple"
    assert calls == []


def test_classifier_uses_haiku_when_heuristic_misses(claude_mod):
    captured = {}

    def fake_create(**kw):
        captured.update(kw)
        return _FakeResp("complex", input_tokens=50, output_tokens=1)

    claude_mod._client_singleton = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
    out = claude_mod._classify_complexity(
        "I need to get from one of a few cities over to somewhere else sometime next month"
    )
    assert out == "complex"
    assert captured["model"] == claude_mod.config.CLAUDE_HAIKU_MODEL


def test_pick_model_for_haiku_tasks(claude_mod):
    for kind in ("classify", "intent", "extract", "status", "format"):
        assert claude_mod.pick_model_for(kind, "DEL to BOM") == claude_mod.config.CLAUDE_HAIKU_MODEL


def test_pick_model_for_pick_is_always_sonnet(claude_mod):
    # pick runs on Sonnet regardless of request complexity — Haiku's context
    # is fragile with long ranked offer lists.
    assert (
        claude_mod.pick_model_for("pick", "DEL to BOM tomorrow morning")
        == claude_mod.config.CLAUDE_SONNET_MODEL
    )
    assert (
        claude_mod.pick_model_for("pick", "multi-city DEL BOM CCU next week")
        == claude_mod.config.CLAUDE_SONNET_MODEL
    )


def test_pick_model_for_does_not_call_classifier(claude_mod):
    """The hot pick path must not burn a Haiku classify call per request."""
    calls = []
    claude_mod._client_singleton = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kw: calls.append(kw) or _FakeResp("complex"))
    )
    claude_mod.pick_model_for("pick", "some arbitrary request")
    assert calls == []


def test_confirm_never_calls_anthropic(claude_mod, monkeypatch):
    """Payment-affecting confirm must not touch the LLM at all — it is a
    deterministic REST call via `letsfg_booking.book`."""
    llm_calls = []

    def fail_if_called(**kw):
        llm_calls.append(kw)
        raise AssertionError("confirm() must not call Anthropic")

    claude_mod._client_singleton = SimpleNamespace(
        beta=SimpleNamespace(messages=SimpleNamespace(create=fail_if_called)),
        messages=SimpleNamespace(create=fail_if_called),
    )
    monkeypatch.setattr(claude_mod.config, "DRY_RUN", False)

    # Stub the booking helper so no LetsFG call goes out either.
    captured = {}

    def fake_book(picked, traveller, booking_id):
        captured["search_id"] = picked["search_id"]
        captured["offer_id"] = picked["offer_id"]
        captured["traveller"] = traveller
        return {
            "status": "awaiting_approval",
            "booking_ref": "BK-1",
            "approve_url": "https://pay/BK-1",
            "message": "",
        }

    monkeypatch.setattr(claude_mod.letsfg_booking, "book", fake_book)

    out = claude_mod.confirm(
        {
            "status": "picked",
            "backend": "rest",
            "search_id": "S",
            "offer_id": "O",
            "summary": "x",
            "price": 1,
            "currency": "INR",
        },
        {"given_name": "A", "family_name": "B", "email": "a@b", "phone": "+1"},
    )
    assert llm_calls == []
    assert out.get("status") == "awaiting_approval"
    assert out.get("booking_ref") == "BK-1"
    assert captured["search_id"] == "S"
    assert captured["offer_id"] == "O"


def test_record_usage_writes_to_db(claude_mod):
    from traveldesk.storage import db

    row_id, _ = db.new_request("UEMP1", "DEL BOM usage", "claude")
    resp = _FakeResp("ok", input_tokens=1234, output_tokens=56)
    claude_mod._record_usage(
        resp,
        task_kind="pick",
        model=claude_mod.config.CLAUDE_SONNET_MODEL,
        booking_id=row_id,
    )
    rows = db.usage_for_booking(row_id)
    assert len(rows) == 1
    r = rows[0]
    assert r["input_tokens"] == 1234
    assert r["output_tokens"] == 56
    assert r["provider"] == "claude"
    assert r["est_cost_usd"] > 0
