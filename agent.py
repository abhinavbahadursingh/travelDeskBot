"""Returns the AI agent module (Claude).

The agent exposes:
    pick(request, traveller, booking_id=None) -> dict
    confirm(picked, traveller, booking_id=None) -> dict
    booking_status(booking_ref, booking_id=None) -> dict
    book(request, traveller, booking_id=None) -> dict   # pick + confirm
"""
from __future__ import annotations

import importlib
from types import ModuleType

_cache: ModuleType | None = None


def get_agent() -> ModuleType:
    global _cache
    if _cache is None:
        _cache = importlib.import_module("claude_agent")
    return _cache
