"""Verify provider switching gives the right module without touching the net."""
from __future__ import annotations

import importlib
import os
import sys


def _reload_with(provider: str):
    os.environ["AI_PROVIDER"] = provider
    for mod in ("agent", "config"):
        sys.modules.pop(mod, None)
    return importlib.import_module("agent")


def test_groq_is_default_when_unset():
    os.environ.pop("AI_PROVIDER", None)
    for mod in ("agent", "config"):
        sys.modules.pop(mod, None)
    a = importlib.import_module("agent")
    mod = a.get_agent()
    assert mod.__name__ == "groq_agent"
    # interface contract: every provider exposes these
    assert all(hasattr(mod, fn) for fn in ("pick", "confirm", "booking_status", "book"))


def test_groq_selectable():
    a = _reload_with("groq")
    mod = a.get_agent()
    assert mod.__name__ == "groq_agent"
    assert all(hasattr(mod, fn) for fn in ("pick", "confirm", "booking_status", "book"))


def test_claude_selectable():
    a = _reload_with("claude")
    mod = a.get_agent()
    assert mod.__name__ == "claude_agent"
    assert all(hasattr(mod, fn) for fn in ("pick", "confirm", "booking_status", "book"))
