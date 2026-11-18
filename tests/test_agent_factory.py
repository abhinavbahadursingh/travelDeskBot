"""The agent factory returns the Claude agent without touching the net."""
from __future__ import annotations

import importlib
import sys


def test_claude_is_the_agent():
    for mod in ("agent", "config"):
        sys.modules.pop(mod, None)
    a = importlib.import_module("agent")
    mod = a.get_agent()
    assert mod.__name__ == "claude_agent"
    # interface contract used by app.py
    assert all(hasattr(mod, fn) for fn in ("pick", "confirm", "booking_status", "book"))


def test_agent_is_cached():
    a = importlib.import_module("agent")
    assert a.get_agent() is a.get_agent()
