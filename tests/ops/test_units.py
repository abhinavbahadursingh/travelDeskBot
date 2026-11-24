from datetime import UTC, datetime

import pytest

from traveldesk.ops.units import fmt_ist, format_inr, parse_local, to_ist, to_paise


@pytest.mark.parametrize(
    "rupees,paise",
    [
        (10428.0, 1_042_800),
        ("10428", 1_042_800),
        (0.1 + 0.2, 30),  # float noise does not leak into paise
        ("1234.565", 123_457),  # half-up at the paisa
        (0, 0),
    ],
)
def test_to_paise_exact(rupees, paise):
    assert to_paise(rupees) == paise


@pytest.mark.parametrize("junk", ["abc", "", None, float("nan"), float("inf")])
def test_to_paise_rejects_junk(junk):
    with pytest.raises(ValueError):
        to_paise(junk)


@pytest.mark.parametrize(
    "paise,text",
    [
        (50_000, "₹500"),
        (1_042_800, "₹10,428"),
        (12_345_678, "₹1,23,456.78"),
        (1_000_000_000, "₹1,00,00,000"),
        (-50_000, "-₹500"),
    ],
)
def test_format_inr_indian_grouping(paise, text):
    assert format_inr(paise) == text


def test_supplier_local_time_is_ist_and_stored_utc():
    dt = parse_local("2026-11-22T06:05")
    assert dt == datetime(2026, 11, 22, 0, 35, tzinfo=UTC)
    assert to_ist(dt).strftime("%H:%M") == "06:05"
    assert fmt_ist(dt) == "22 Nov 06:05"


def test_parse_local_junk_is_none():
    assert parse_local("not a date") is None
    assert parse_local(None) is None
