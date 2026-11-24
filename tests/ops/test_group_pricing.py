"""Group booking: a fresh offer per traveller (GROUP_OFFER_MODE) and re-pricing on retry / [Book at ₹X]."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.ops.test_group import ADMIN, NAMES, PROFILES, Picky, Slack, deps, person, policy, roster, start
from traveldesk.ops import group
from traveldesk.ops.group import APPROVE_ACTION, price_change_text, price_ok
from traveldesk.ops.models import Booking, RosterMember, RosterStatus
from traveldesk.ops.supplier.base import PriceQuote

CARD = 2_500_000  # ₹25,000 per person on the card
TWO = "DEL Dubai for <@UA> <@UB>"
CLEAN = {**PROFILES, "UB": person("Ravi", "Kumar")}  # both travellers bookable


def quote(fare, available=True):
    return PriceQuote(available, fare if available else None)


def fares(ops_session):
    with ops_session() as s:
        return sorted(b.fare_paise for b in s.scalars(select(Booking)))


# --- price rules --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "new,cap,ok",
    [
        (2_500_000, None, True),  # unchanged
        (2_400_000, None, True),  # dropped
        (2_625_000, None, True),  # +5.0% exactly
        (2_625_100, None, False),  # just over 5%
        (2_550_000, 2_520_000, False),  # +2% but over MAX_PRICE
    ],
)
def test_price_ok(new, cap, ok):
    assert price_ok(CARD, new, cap=cap, tolerance_pct=5.0) is ok


def test_price_change_text():
    assert price_change_text(CARD, 2_750_000, None) == "fare ₹25,000 → ₹27,500 (+10%)"
    assert (
        price_change_text(CARD, 2_600_000, 2_550_000) == "fare ₹25,000 → ₹26,000 (+4%, over the cap ₹25,500)"
    )


# --- 2. fresh offer per traveller -------------------------------------------------------------


def test_single_use_offer_books_everyone_in_per_traveller_mode(ops_session):
    sup = Picky(single_use=True)
    d = deps(sup, profiles=CLEAN)
    slack = Slack(NAMES)
    rid = start(slack, d, text=TWO)
    group.confirm(slack, rid, ADMIN, deps=d, policy=policy(offer_mode="per_traveller"))
    r = roster(ops_session, rid)
    assert r["UA"][0] is RosterStatus.BOOKED and r["UB"][0] is RosterStatus.BOOKED
    booked_offers = [t.slack_id for t, _ in sup.sent]
    assert booked_offers == ["UA", "UB"]
    assert [c[0] for c in sup.calls] == ["price_check", "book", "price_check", "book"]


def test_single_use_offer_fails_second_traveller_in_shared_mode(ops_session):
    sup = Picky(single_use=True)
    d = deps(sup, profiles=CLEAN)
    slack = Slack(NAMES)
    rid = start(slack, d, text=TWO)
    group.confirm(slack, rid, ADMIN, deps=d, policy=policy(offer_mode="shared"))
    r = roster(ops_session, rid)
    assert r["UA"][0] is RosterStatus.BOOKED
    assert r["UB"] == (RosterStatus.NEEDS_ACTION, "offer already used")
    assert "price_check" not in [c[0] for c in sup.calls]  # shared: today's behaviour


def test_flight_gone_on_re_search_needs_action(ops_session):
    sup = Picky(quotes={"wo_1": quote(None, available=False)})
    d = deps(sup, profiles=CLEAN)
    slack = Slack(NAMES)
    rid = start(slack, d, text=TWO)
    group.confirm(slack, rid, ADMIN, deps=d, policy=policy())
    assert roster(ops_session, rid)["UA"] == (RosterStatus.NEEDS_ACTION, "flight no longer available")
    assert "book" not in [c[0] for c in sup.calls]


def test_retimed_flight_is_not_booked(ops_session):
    """exact=True: the same flight number at another time is a different flight for booking."""
    from tests.ops.test_group import intl_pick

    pick = intl_pick()
    sup = Picky()
    d = deps(sup, profiles=CLEAN, pick=pick)
    slack = Slack(NAMES)
    rid = start(slack, d, text="DEL Dubai for <@UA>")
    with ops_session() as s:
        from traveldesk.ops.models import Request
        from traveldesk.ops.supplier.base import offer_from_json

        dep = offer_from_json(s.get(Request, rid).parsed_json["offer"]).depart_at
    sup.quotes["wo_1"] = PriceQuote(True, CARD, dep + timedelta(minutes=40))
    group.confirm(slack, rid, ADMIN, deps=d, policy=policy())
    assert roster(ops_session, rid)["UA"] == (RosterStatus.NEEDS_ACTION, "flight no longer available")


def test_fresh_fare_small_rise_books_and_card_says_so(ops_session):
    sup = Picky(quotes={"wo_1": quote(2_575_000)})  # +3%
    d = deps(sup, profiles=CLEAN)
    slack = Slack(NAMES)
    rid = start(slack, d, text="DEL Dubai for <@UA>")
    group.confirm(slack, rid, ADMIN, deps=d, policy=policy())
    assert fares(ops_session) == [2_575_000]
    assert "PNR *PNR001* · ₹25,750 (was ₹25,000)" in str(slack.card["blocks"])


def test_fresh_fare_big_rise_asks_admin(ops_session):
    sup = Picky(quotes={"wo_1": quote(2_750_000)})  # +10%
    d = deps(sup, profiles=CLEAN)
    slack = Slack(NAMES)
    rid = start(slack, d, text="DEL Dubai for <@UA>")
    group.confirm(slack, rid, ADMIN, deps=d, policy=policy())
    assert fares(ops_session) == []  # nothing booked
    status, reason = roster(ops_session, rid)["UA"]
    assert status is RosterStatus.PRICE_CHANGED and reason == "fare ₹25,000 → ₹27,500 (+10%)"
    [btn] = [b["accessory"] for b in slack.card["blocks"] if b.get("accessory")]
    assert (btn["action_id"], btn["value"], btn["text"]["text"]) == (
        APPROVE_ACTION,
        f"{rid}:UA:2750000",
        "Book at ₹27,500",
    )
    assert slack.card["text"] == "Group booking: 0 booked / 1 need action"


# --- 3. [Fix & retry] re-prices first -------------------------------------------------------------


def _retry_setup(ops_session):
    """UA booked at the card fare; UB failed the passport check, then gets fixed."""
    profiles = dict(PROFILES)
    sup = Picky()
    d = deps(sup, profiles=profiles)
    slack = Slack(NAMES)
    rid = start(slack, d, text=TWO)
    group.confirm(slack, rid, ADMIN, deps=d, policy=policy())
    assert roster(ops_session, rid)["UB"][0] is RosterStatus.NEEDS_ACTION
    profiles["UB"] = person("Ravi", "Kumar")
    return sup, d, slack, rid


def _retry(slack, rid, d, **pol):
    return group.retry(slack, rid, "UB", ADMIN, deps=d, policy=policy(**pol))


def _ub_fare(ops_session, rid):
    with ops_session() as s:
        m = s.scalar(
            select(RosterMember).where(RosterMember.request_id == rid, RosterMember.slack_id == "UB")
        )
        return m.booking.fare_paise if m.booking else None


@pytest.mark.parametrize("fare", [CARD, 2_300_000])  # unchanged, dropped
def test_retry_same_or_lower_fare_books(ops_session, fare):
    sup, d, slack, rid = _retry_setup(ops_session)
    sup.quotes["wo_1"] = quote(fare)
    assert _retry(slack, rid, d)
    assert roster(ops_session, rid)["UB"][0] is RosterStatus.BOOKED
    assert _ub_fare(ops_session, rid) == fare
    assert sup.calls[-2][0] == "price_check" and sup.calls[-1][0] == "book"


def test_retry_small_rise_books_and_shows_new_fare(ops_session):
    sup, d, slack, rid = _retry_setup(ops_session)
    sup.quotes["wo_1"] = quote(2_600_000)  # +4%
    _retry(slack, rid, d)
    assert _ub_fare(ops_session, rid) == 2_600_000
    assert "₹26,000 (was ₹25,000)" in str(slack.card["blocks"])


def test_retry_big_rise_does_not_book(ops_session):
    sup, d, slack, rid = _retry_setup(ops_session)
    sup.quotes["wo_1"] = quote(2_750_000)  # +10%
    _retry(slack, rid, d)
    assert _ub_fare(ops_session, rid) is None
    assert roster(ops_session, rid)["UB"] == (RosterStatus.PRICE_CHANGED, "fare ₹25,000 → ₹27,500 (+10%)")
    assert "Book at ₹27,500" in str(slack.card["blocks"])
    assert [c[0] for c in sup.calls].count("book") == 1  # only UA's original booking


def test_retry_over_cap_does_not_book(ops_session):
    sup, d, slack, rid = _retry_setup(ops_session)
    sup.quotes["wo_1"] = quote(2_550_000)  # +2%, but the cap is ₹25,200
    _retry(slack, rid, d, cap_paise=2_520_000)
    status, reason = roster(ops_session, rid)["UB"]
    assert status is RosterStatus.PRICE_CHANGED and "over the cap ₹25,200" in reason


def test_retry_expired_fare_needs_new_request(ops_session):
    sup, d, slack, rid = _retry_setup(ops_session)
    sup.quotes["wo_1"] = quote(None, available=False)
    _retry(slack, rid, d)
    assert roster(ops_session, rid)["UB"] == (RosterStatus.NEEDS_ACTION, "fare expired — send a new request")


def test_retry_books_the_fresh_offer_not_the_stored_one(ops_session):
    sup, d, slack, rid = _retry_setup(ops_session)
    _retry(slack, rid, d)
    with ops_session() as s:
        m = s.scalar(
            select(RosterMember).where(RosterMember.request_id == rid, RosterMember.slack_id == "UB")
        )
        assert m.booking.offer_json["offer_id"].startswith("wo_1~")  # re-searched offer


# --- [Book at ₹X] -----------------------------------------------------------------------------------


def _price_changed(ops_session):
    sup, d, slack, rid = _retry_setup(ops_session)
    sup.quotes["wo_1"] = quote(2_750_000)
    _retry(slack, rid, d)
    return sup, d, slack, rid


def test_book_at_x_books_at_x(ops_session):
    sup, d, slack, rid = _price_changed(ops_session)
    assert group.approve(slack, rid, "UB", ADMIN, 2_750_000, deps=d, policy=policy())
    assert roster(ops_session, rid)["UB"][0] is RosterStatus.BOOKED
    assert _ub_fare(ops_session, rid) == 2_750_000
    assert "₹27,500 (was ₹25,000)" in str(slack.card["blocks"])  # the booked price is on the card


def test_book_at_x_allowed_over_cap_once_admin_confirms(ops_session):
    sup, d, slack, rid = _retry_setup(ops_session)
    sup.quotes["wo_1"] = quote(2_550_000)
    _retry(slack, rid, d, cap_paise=2_520_000)
    assert group.approve(slack, rid, "UB", ADMIN, 2_550_000, deps=d, policy=policy(cap_paise=2_520_000))
    assert _ub_fare(ops_session, rid) == 2_550_000


def test_book_at_x_refuses_when_price_moved_again(ops_session):
    sup, d, slack, rid = _price_changed(ops_session)
    sup.quotes["wo_1"] = quote(2_760_000)  # +₹100 since the button was shown: no tolerance here
    group.approve(slack, rid, "UB", ADMIN, 2_750_000, deps=d, policy=policy())
    assert _ub_fare(ops_session, rid) is None
    status, reason = roster(ops_session, rid)["UB"]
    assert status is RosterStatus.PRICE_CHANGED and reason == "fare ₹27,500 → ₹27,600 (+0.4%)"
    assert "Book at ₹27,600" in str(slack.card["blocks"])


def test_stale_book_at_button_is_ignored(ops_session):
    sup, d, slack, rid = _price_changed(ops_session)
    assert group.approve(slack, rid, "UB", ADMIN, 2_600_000, deps=d, policy=policy()) is False
    assert group.approve(slack, rid, "UB", "UA", 2_750_000, deps=d, policy=policy()) is False  # not allowed
    assert [c[0] for c in sup.calls].count("book") == 1


def test_never_books_a_price_not_shown(ops_session):
    """Whatever path booked it, the booked fare is on the card."""
    sup, d, slack, rid = _retry_setup(ops_session)
    sup.quotes["wo_1"] = quote(2_610_000)
    _retry(slack, rid, d)
    fare = _ub_fare(ops_session, rid)
    assert f"₹{fare // 100:,}" in str(slack.card["blocks"])


def test_register_wires_book_at():
    class FakeApp:
        def __init__(self):
            self.actions = {}

        def action(self, action_id):
            return lambda fn: self.actions.setdefault(action_id, fn)

    app = FakeApp()
    group.register(app)
    assert APPROVE_ACTION in app.actions
