"""Supplier layer. `get_supplier()` picks the implementation from SUPPLIER (letsfg | tbo)."""

from __future__ import annotations

from traveldesk.ops.settings import get_settings
from traveldesk.ops.supplier.base import (
    BookingResult,
    CancelResult,
    Offer,
    PriceQuote,
    SearchQuery,
    Supplier,
    SupplierError,
    SupplierUnsupported,
    TravelerDetails,
    new_trace_id,
)

__all__ = [
    "BookingResult",
    "CancelResult",
    "Offer",
    "PriceQuote",
    "SearchQuery",
    "Supplier",
    "SupplierError",
    "SupplierUnsupported",
    "TravelerDetails",
    "get_supplier",
    "new_trace_id",
]


def get_supplier(name: str | None = None) -> Supplier:
    name = (name or get_settings().supplier).lower()
    if name == "letsfg":
        from traveldesk.ops.supplier.letsfg import LetsFGSupplier

        return LetsFGSupplier()
    if name == "tbo":
        from traveldesk.ops.supplier.tbo import TBOSupplier

        return TBOSupplier()
    raise ValueError(f"unknown SUPPLIER {name!r} (letsfg | tbo)")
