"""Tests for the deterministic selection + confirm layer in groq_agent.

No network: Groq, MCP and everything external are stubbed.
"""
from __future__ import annotations

import groq_agent


def _offer(**kw):
    base = {
        "offer_id": "o1",
        "bookable": True,
        "stops": 0,
        "depart_time": "2026-11-25T07:30:00",
        "total_price": 5000,
        "currency": "INR",
        "carrier": "6E",
        "flight_number": "123",
        "origin": "DEL",
        "destination": "BOM",
    }
    base.update(kw)
    return base


def test_choose_picks_cheapest_nonstop_in_window():
    offers = [
        _offer(offer_id="a", total_price=6500, stops=0),
        _offer(offer_id="b", total_price=5200, stops=0),  # winner
        _offer(offer_id="c", total_price=4500, stops=1),  # cheaper but has a stop
    ]
    pick = groq_agent._choose(offers, "morning")
    assert pick["offer_id"] == "b"


def test_choose_skips_non_bookable():
    offers = [
        _offer(offer_id="a", total_price=4000, bookable=False),
        _offer(offer_id="b", total_price=6000),
    ]
    pick = groq_agent._choose(offers, "morning")
    assert pick["offer_id"] == "b"


def test_choose_enforces_max_price(monkeypatch):
    monkeypatch.setattr(groq_agent.config, "MAX_PRICE", 4000)
    offers = [
        _offer(offer_id="a", total_price=5000),
        _offer(offer_id="b", total_price=6000),
    ]
    assert groq_agent._choose(offers, "morning") is None


def test_choose_filters_time_window():
    offers = [
        _offer(offer_id="a", depart_time="2026-11-25T03:00:00", total_price=3000),
        _offer(offer_id="b", depart_time="2026-11-25T09:00:00", total_price=5000),
    ]
    pick = groq_agent._choose(offers, "morning")
    assert pick["offer_id"] == "b"


def test_pick_handles_search_error(monkeypatch):
    def boom(_text):
        raise ValueError("Groq never produced a search_id")
    monkeypatch.setattr(groq_agent, "_drive_search", boom)
    out = groq_agent.pick("garbage", {"email": "x@y"})
    assert out["status"] == "need_info"
    assert "search_id" in out["message"]


def test_confirm_dry_run_never_books(monkeypatch):
    monkeypatch.setattr(groq_agent.config, "DRY_RUN", True)

    class FakeMCP:
        def call_tool(self, *_a, **_kw):
            raise AssertionError("confirm() must not call MCP in DRY_RUN")

    monkeypatch.setattr(groq_agent, "_mcp", lambda: FakeMCP())
    out = groq_agent.confirm(
        {"status": "picked", "search_id": "s", "offer_id": "o", "summary": "x",
         "price": 100, "currency": "INR"},
        {"given_name": "A", "family_name": "B", "email": "a@b", "phone": "+1"},
    )
    assert out["status"] == "picked"


def test_pick_end_to_end_with_stubbed_search(monkeypatch):
    """pick() combines _drive_search + _choose; test it with Groq fully stubbed."""
    stub_offers = [
        _offer(offer_id="x", total_price=9999),
        _offer(offer_id="y", total_price=4500),  # should win
    ]
    monkeypatch.setattr(
        groq_agent, "_drive_search",
        lambda _r: {"search_id": "S1", "offers": stub_offers, "time_window": "morning"},
    )
    out = groq_agent.pick("DEL to BOM Tue AM",
                          {"given_name": "A", "family_name": "B", "email": "a@b"})
    assert out["status"] == "picked"
    assert out["offer_id"] == "y"
    assert out["search_id"] == "S1"


def test_tools_do_not_include_booking():
    """Safety: the Groq model must never be given access to book_flight."""
    names = {t["function"]["name"] for t in groq_agent._TOOLS}
    assert "book_flight" not in names
    assert "get_flight_booking" not in names


def test_redact_removes_secrets():
    """Logging helper must blank out API keys and tokens."""
    groq_agent.config.GROQ_API_KEY = "gsk_SECRET"
    groq_agent.config.LETSFG_TOKEN = "letsfg_SECRET"
    out = groq_agent._redact({"headers": {"Authorization": "Bearer gsk_SECRET",
                                           "LetsFG-Token": "letsfg_SECRET"}})
    assert "gsk_SECRET" not in out
    assert "letsfg_SECRET" not in out
    assert "***" in out
