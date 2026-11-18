"""Provider selector. Returns whichever agent module AI_PROVIDER names.

Every provider module exposes the same four functions:
    pick(request, traveller) -> dict
    confirm(picked, traveller) -> dict
    booking_status(booking_ref) -> dict
    book(request, traveller) -> dict         # legacy convenience wrapper
"""
from __future__ import annotations

import importlib
from types import ModuleType

import config

_cache: ModuleType | None = None


def get_agent() -> ModuleType:
    global _cache
    if _cache is not None:
        return _cache
    name = "groq_agent" if config.AI_PROVIDER == "groq" else "claude_agent"
    _cache = importlib.import_module(name)
    return _cache
