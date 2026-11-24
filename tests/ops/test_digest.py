"""Phase 3: end-of-day leakage digest."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.ops.fakes import FakeNotifier
from traveldesk.ops import gst
from traveldesk.ops.digest import OPEN_ACTION, RESOLVE_ACTION, build_message, find_leaks, post_digest
from traveldesk.ops.models import Booking, BookingStatus, Event, LedgerEntry
from traveldesk.ops.records import attach_invoice, create_request, record_booking, upsert_traveler
from traveldesk.ops.settings import OpsSettings
from traveldesk.ops.supplier.base import BookingResult

NOW = datetime(2026, 11, 21, 13, 30, tzinfo=UTC)  # 19:00 IST
COMPANY = "27AAPFU0939F1ZV"
OTHER_VALID = "29AAGCB7383J1Z4"


def cfg(**kw):
    return OpsSettings(**{"ops_slack_channel": "#travel-ops", "company_gstin": COMPANY, **kw})


_n = iter(range(1, 10_000))


def ticket(ops_session, *, fare=1_000_000, legacy_id=None, invoice=None, gstin=None, inv_amount=None):
    """A ticketed booking with its debit; optional invoice details on the debit."""
    i = next(_n)
    with ops_session() as s:
        t = upsert_traveler(s, slack_id="UT", name="Asha Rao")
        r = create_request(s, slack_user="UT", raw_text="x", legacy_booking_id=legacy_id)
        b = record_booking(
            s, r, supplier="fake", traveler=t, route="DEL-BOM",
            depart_at=datetime(2026, 12, 1, 0, 35, tzinfo=UTC),
            result=BookingResult("ticketed", f"REF{i}", pnr=f"PNR{i}", fare_paise=fare),
        )  # fmt: skip
        if invoice:
            attach_invoice(s, b, invoice_no=invoice, gst_no=gstin, invoice_amount_paise=inv_amount)
        return b.id


def clean(ops_session, **kw):
    return ticket(ops_session, invoice="INV-1", gstin=COMPANY, inv_amount=1_000_000, **kw)


def ids(found, bucket):
    return [i.booking_id for i in found[bucket]]


# --- GSTIN --------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,ok",
    [
        (COMPANY, True),
        (OTHER_VALID, True),
        (" 27aapfu0939f1zv ", True),  # normalized
        ("27AAPFU0939F1ZX", False),  # wrong check digit
        ("27AAPFU0939F1Z", False),  # too short
        ("", False),
        (None, False),
    ],
)
def test_gstin_validation(value, ok):
    assert gst.is_valid(value) is ok


# --- buckets ------------------------------------------------------------------


def test_clean_booking_is_not_listed(ops_session):
    clean(ops_session)
    assert all(not v for v in find_leaks(NOW, cfg()).values())


def test_ticketed_without_debit(ops_session):
    bid = clean(ops_session)
    with ops_session() as s:
        s.query(LedgerEntry).delete()
    found = find_leaks(NOW, cfg())
    assert ids(found, "unbilled") == [bid]
    assert found["unbilled"][0].summary.endswith("— no ledger debit")


def test_ticketed_without_invoice_number(ops_session):
    bid = ticket(ops_session)
    found = find_leaks(NOW, cfg())
    assert ids(found, "unbilled") == [bid]
    s = found["unbilled"][0].summary
    assert (
        s
        == "DEL-BOM 01 Dec · PNR PNR"
        + s.split("PNR PNR")[1].split(" ")[0]
        + " · Asha Rao · ₹10,000 — no invoice number"
    )


@pytest.mark.parametrize(
    "gstin,amount,problem",
    [
        (None, 1_000_000, "GSTIN missing"),
        ("27AAPFU0939F1ZX", 1_000_000, "GSTIN 27AAPFU0939F1ZX invalid"),
        (OTHER_VALID, 1_000_000, f"billed to {OTHER_VALID}, not company GSTIN"),
        (COMPANY, 1_050_000, "invoice ₹10,500 ≠ debit ₹10,000"),
    ],
)
def test_gst_and_amount_mismatches(ops_session, gstin, amount, problem):
    bid = ticket(ops_session, invoice="INV-9", gstin=gstin, inv_amount=amount)
    found = find_leaks(NOW, cfg())
    assert ids(found, "gst") == [bid]
    assert problem in found["gst"][0].summary
    assert found["unbilled"] == []  # one bucket per booking


def test_no_company_gstin_configured_only_checks_validity(ops_session):
    ticket(ops_session, invoice="INV-9", gstin=OTHER_VALID, inv_amount=1_000_000)
    assert find_leaks(NOW, cfg(company_gstin=""))["gst"] == []


def test_missing_invoice_amount_is_not_a_mismatch(ops_session):
    ticket(ops_session, invoice="INV-9", gstin=COMPANY, inv_amount=None)
    assert find_leaks(NOW, cfg())["gst"] == []


def _refund_pending(ops_session, days_ago):
    bid = clean(ops_session)
    with ops_session() as s:
        b = s.get(Booking, bid)
        b.status = BookingStatus.REFUND_PENDING
        s.flush()
        assert b.status_changed_at is not None  # stamped automatically
        b.status_changed_at = NOW - timedelta(days=days_ago)
    return bid


def test_refund_pending_older_than_7_days(ops_session):
    old = _refund_pending(ops_session, 8)
    _refund_pending(ops_session, 6)
    found = find_leaks(NOW, cfg())
    assert ids(found, "refund_stale") == [old]
    assert "refund pending 8 days" in found["refund_stale"][0].summary


def test_status_change_is_stamped_only_on_change(ops_session):
    bid = clean(ops_session)
    with ops_session() as s:
        b = s.get(Booking, bid)
        first = b.status_changed_at
        b.status = BookingStatus.TICKETED  # same status
        s.flush()
        assert b.status_changed_at == first


def test_resolved_items_stay_hidden(ops_session):
    bid = ticket(ops_session)
    from traveldesk.ops.slack_actions import handle_resolve

    handle_resolve({"actions": [{"value": f"unbilled:{bid}"}], "user": {"id": "UOPS"}}, client=None)
    assert find_leaks(NOW, cfg())["unbilled"] == []


# --- message ------------------------------------------------------------------


def test_all_clear_message(ops_session):
    text, blocks = build_message(find_leaks(NOW, cfg()), NOW, cfg())
    assert text == "Travel desk leakage — Sat 21 Nov: all clear"
    assert "All clear" in blocks[-1]["text"]["text"]


def test_message_skips_empty_buckets_and_has_buttons(ops_session):
    bid = ticket(ops_session, legacy_id=28)
    text, blocks = build_message(find_leaks(NOW, cfg(dashboard_url="http://mini:5173/")), NOW,
                                 cfg(dashboard_url="http://mini:5173/"))  # fmt: skip
    assert text == "Travel desk leakage — Sat 21 Nov: 1 item(s) (1 unbilled)"
    headers = [
        b["text"]["text"] for b in blocks if b["type"] == "section" and b["text"]["text"].startswith("*")
    ]
    assert headers == ["*1. Ticketed, no ledger debit or invoice* (1)"]  # buckets 2 and 3 skipped
    actions = [b for b in blocks if b["type"] == "actions"]
    [resolve, open_] = actions[0]["elements"]
    assert (resolve["action_id"], resolve["value"]) == (RESOLVE_ACTION, f"unbilled:{bid}")
    assert (open_["action_id"], open_["url"]) == (OPEN_ACTION, "http://mini:5173/?booking=28")


def test_no_open_button_without_dashboard_or_legacy_row(ops_session):
    ticket(ops_session)  # no legacy id
    _, blocks = build_message(
        find_leaks(NOW, cfg(dashboard_url="http://x")), NOW, cfg(dashboard_url="http://x")
    )
    [actions] = [b for b in blocks if b["type"] == "actions"]
    assert [e["action_id"] for e in actions["elements"]] == [RESOLVE_ACTION]


def test_long_buckets_are_capped_under_slack_block_limit(ops_session):
    for _ in range(20):
        ticket(ops_session)  # unbilled
    for _ in range(20):
        ticket(ops_session, invoice="I", gstin=None, inv_amount=1_000_000)  # gst
    for _ in range(20):
        _refund_pending(ops_session, 30)
    _, blocks = build_message(find_leaks(NOW, cfg()), NOW, cfg())
    assert len(blocks) <= 50
    overflow = [b for b in blocks if b["type"] == "context"]
    assert len(overflow) == 3 and "+13 more" in overflow[0]["elements"][0]["text"]


# --- posting --------------------------------------------------------------------


def test_posts_once_per_ist_day(ops_session):
    ticket(ops_session)
    n = FakeNotifier()
    assert post_digest(n, now=NOW, cfg=cfg()) == "ts1"
    assert post_digest(n, now=NOW + timedelta(hours=1), cfg=cfg()) is None  # same IST day
    assert len(n.posts) == 1
    post_digest(n, now=NOW + timedelta(days=1), cfg=cfg())
    assert len(n.posts) == 2
    with ops_session() as s:
        days = sorted(e.payload_json["day"] for e in s.scalars(select(Event).where(Event.kind == "digest")))
    assert days == ["2026-11-21", "2026-11-22"]


def test_failed_post_is_retried(ops_session):
    n = FakeNotifier(fail_targets={"#travel-ops"})
    with pytest.raises(RuntimeError):
        post_digest(n, now=NOW, cfg=cfg())
    n.fail_targets.clear()
    assert post_digest(n, now=NOW + timedelta(minutes=10), cfg=cfg()) == "ts1"


def test_no_channel_does_not_post(ops_session):
    n = FakeNotifier()
    assert post_digest(n, now=NOW, cfg=cfg(ops_slack_channel="")) is None
    assert n.posts == []


# --- [Mark resolved] --------------------------------------------------------------


class FakeClient:
    def __init__(self):
        self.updates = []

    def chat_update(self, **kw):
        self.updates.append(kw)


def test_mark_resolved_updates_only_that_item(ops_session):
    from traveldesk.ops.slack_actions import handle_resolve

    a, b = ticket(ops_session), ticket(ops_session)
    n = FakeNotifier()
    post_digest(n, now=NOW, cfg=cfg())
    _, text, blocks = n.posts[0]
    body = {
        "actions": [{"value": f"unbilled:{a}"}],
        "user": {"id": "UOPS"},
        "channel": {"id": "C1"},
        "message": {"ts": "ts1", "text": text, "blocks": blocks},
    }
    client = FakeClient()
    assert handle_resolve(body, client) is True
    assert handle_resolve(body, client) is False  # second click: already resolved

    new = client.updates[0]["blocks"]
    by_id = {blk.get("block_id"): blk for blk in new if blk.get("block_id")}
    assert by_id[f"leak:unbilled:{a}"]["type"] == "context"
    assert "Resolved by <@UOPS>" in by_id[f"leak:unbilled:{a}"]["elements"][0]["text"]
    assert by_id[f"leak:unbilled:{b}"]["type"] == "actions"
    assert ids(find_leaks(NOW, cfg()), "unbilled") == [b]


def test_register_wires_digest_actions():
    from traveldesk.ops.slack_actions import register

    class FakeApp:
        def __init__(self):
            self.actions = {}

        def action(self, action_id):
            return lambda fn: self.actions.setdefault(action_id, fn)

    app = FakeApp()
    register(app)
    assert {RESOLVE_ACTION, OPEN_ACTION} <= set(app.actions)
    acked = []
    app.actions[OPEN_ACTION](ack=lambda: acked.append(True))
    assert acked == [True]


# --- invoice CLI ---------------------------------------------------------------------


def test_invoice_cli_clears_unbilled(ops_session, capsys):
    from traveldesk.ops.invoices import main

    bid = ticket(ops_session)
    main(["attach", str(bid), "--invoice-no", "INV-77", "--gstin", COMPANY, "--amount", "10000"])
    assert "invoice INV-77 saved" in capsys.readouterr().out
    assert all(not v for v in find_leaks(NOW, cfg()).values())


def test_invoice_cli_rejects_bad_gstin(ops_session):
    from traveldesk.ops.invoices import main

    bid = ticket(ops_session)
    with pytest.raises(SystemExit, match="not valid"):
        main(["attach", str(bid), "--invoice-no", "X", "--gstin", "27AAPFU0939F1ZX"])
