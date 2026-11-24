"""Phase 4: split-roster group booking."""

from dataclasses import replace
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from tests.ops.fakes import FakeSupplier
from traveldesk.booking import traveller_profile
from traveldesk.ops import db as ops_db
from traveldesk.ops import group
from traveldesk.ops.group import CANCEL_ACTION, CONFIRM_ACTION, RETRY_ACTION, Deps, Policy
from traveldesk.ops.models import (
    Booking,
    BookingStatus,
    LedgerEntry,
    LedgerStatus,
    Request,
    RequestStatus,
    RosterMember,
    RosterStatus,
)
from traveldesk.ops.supplier.base import BookingResult
from traveldesk.ops.units import utcnow

TODAY = date.today()
TRIP = TODAY + timedelta(days=30)
ADMIN = "UADMIN"


def intl_pick(**kw):
    return {
        "status": "picked",
        "summary": "Emirates · DEL → DXB · nonstop",
        "price": 25000.0,
        "currency": "INR",
        "search_id": "ws_1",
        "offer_id": "wo_1",
        "depart_date": TRIP.isoformat(),
        "last_flight_date": TRIP.isoformat(),
        "depart_at": f"{TRIP.isoformat()}T09:00",
        "domestic": False,
        "airports": ["DEL", "DXB"],
        "origin": "DEL",
        "destination": "DXB",
        "signature": [["EK", "EK511", "DEL", "DXB", f"{TRIP.isoformat()}T09:00"]],
        **kw,
    }


def domestic_pick():
    return intl_pick(summary="IndiGo · DEL → BOM", price=6000.0, domestic=True, airports=["DEL", "BOM"],
                     destination="BOM", signature=[["6E", "", "DEL", "BOM", f"{TRIP.isoformat()}T06:05"]])  # fmt: skip


def person(given, family, *, passport_years=3.0, **kw):
    return {
        "given_name": given,
        "family_name": family,
        "email": f"{given.lower()}@example.com",
        "phone": "+919800000001",
        "date_of_birth": "1990-01-01",
        "gender": "f",
        "nationality": "IN",
        "passport_number": "Z1234567",
        "passport_country": "IN",
        "passport_expiry": (TODAY + timedelta(days=int(365 * passport_years))).isoformat(),
        **kw,
    }


class Slack:
    def __init__(self, names=None):
        self.names = names or {}
        self.posts, self.updates, self.dms = [], [], []

    def chat_postMessage(self, channel, text, blocks=None, thread_ts=None, **kw):
        if channel.startswith("DM-"):
            self.dms.append((channel[3:], text))
        else:
            self.posts.append({"channel": channel, "text": text, "blocks": blocks, "thread_ts": thread_ts})
        return {"ts": f"ts{len(self.posts)}"}

    def chat_update(self, **kw):
        self.updates.append(kw)

    def users_info(self, user):
        if user not in self.names:
            raise RuntimeError("user_not_found")
        return {"user": {"real_name": self.names[user], "profile": {}}}

    def conversations_open(self, users):
        return {"channel": {"id": f"DM-{users}"}}

    @property
    def card(self):
        return self.updates[-1] if self.updates else [p for p in self.posts if p["blocks"]][-1]


class Picky(FakeSupplier):
    """Refuses the booking for some travellers; records what each booking sent."""

    def __init__(self, refuse=(), **kw):
        super().__init__(**kw)
        self.refuse, self.sent = set(refuse), []

    def book(self, offer, traveler, *, idempotency_key, trace_id):
        self.sent.append((traveler, idempotency_key))
        if traveler.slack_id in self.refuse:
            self.calls.append(("book", trace_id))
            return BookingResult("failed", None, message="fare sold out")
        return super().book(offer, traveler, idempotency_key=idempotency_key, trace_id=trace_id)


PROFILES = {
    ADMIN: person("Admin", "User"),
    "UA": person("Asha", "Rao"),
    "UB": person("Ravi", "Kumar", passport_years=0.3),  # expires before trip + 6 months
    "UD": person("Meera", "Shah"),
}
NAMES = {"UA": "Asha Rao", "UB": "Ravi K", "UD": "John Smith"}  # UC: not in Slack lookup / profiles


def deps(supplier=None, pick=None, profiles=None):
    profiles = PROFILES if profiles is None else profiles
    return Deps(
        supplier=supplier or Picky(),
        pick=lambda text, me: pick or intl_pick(),
        load_profile=lambda sid: profiles.get(sid),
        validate=traveller_profile.validate,
        saved_fields=traveller_profile.saved_fields,
        public_view=traveller_profile.public_view,
        spawn=lambda fn: fn(),
    )


def policy(**kw):
    return Policy(**{"admins": frozenset({ADMIN}), "cap_paise": 5_000_000, "dry_run": False, **kw})


TEXT = "DEL to Dubai next month for <@UA|asha> <@UB> <@UC> <@UD|meera>"


def start(slack, d, p=None, text=TEXT, who=ADMIN):
    group.start(slack, who, text, "DMADMIN", deps=d, policy=p or policy())
    with ops_db.session_scope() as s:
        return s.scalar(select(func.max(Request.id)))


def roster(ops_session, rid):
    with ops_session() as s:
        return {
            m.slack_id: (m.status, m.reason)
            for m in s.scalars(select(RosterMember).where(RosterMember.request_id == rid))
        }


# --- parsing -----------------------------------------------------------------------


def test_parse_roster():
    r = group.parse_roster("DEL to BOM 1 Dec for <@UA|asha>, <@UB> and <@UA>")
    assert r.slack_ids == ["UA", "UB"]  # de-duplicated, in order
    assert "<@" not in r.text and r.text.startswith("DEL to BOM 1 Dec for")


@pytest.mark.parametrize(
    "text,is_group",
    [
        ("DEL to BOM tomorrow", False),
        ("DEL to BOM tomorrow for <@ME>", False),  # only the requester
        ("DEL to BOM for <@UA>", True),
        ("DEL to BOM for <@ME> <@UA>", True),
        ("DEL to BOM for @asha @ravi", True),  # unescaped names → group (gets a setup hint)
        ("email me at a@b.com", False),
    ],
)
def test_is_group_request(text, is_group):
    assert group.is_group_request(text, "ME") is is_group


# --- refusals ------------------------------------------------------------------------


def test_non_admin_is_refused(ops_session):
    slack = Slack()
    assert group.start(slack, "UA", TEXT, "D1", deps=deps(), policy=policy()) is False
    assert "Only travel admins" in slack.posts[0]["text"] and "<@UADMIN>" in slack.posts[0]["text"]


def test_plain_at_names_get_setup_hint(ops_session):
    slack = Slack()
    assert (
        group.start(slack, ADMIN, "DEL to BOM for @asha @ravi", "D1", deps=deps(), policy=policy()) is False
    )
    assert "Escape channels, users, and links" in slack.posts[0]["text"]


def test_too_many_travellers(ops_session):
    text = "DEL BOM " + " ".join(f"<@U{i:03d}>" for i in range(21))
    slack = Slack()
    assert group.start(slack, ADMIN, text, "D1", deps=deps(), policy=policy()) is False


def test_pick_failure_is_reported(ops_session):
    slack = Slack(NAMES)
    rid = start(slack, deps(pick={"status": "no_match", "message": "nothing under the cap"}))
    assert "Not booked (no_match)" in slack.posts[-1]["text"]
    with ops_session() as s:
        assert s.get(Request, rid).status is RequestStatus.FAILED


# --- review card -------------------------------------------------------------------------


def test_review_card_lists_ready_and_reasons(ops_session):
    slack = Slack(NAMES)
    rid = start(slack, deps())
    r = roster(ops_session, rid)
    assert r["UA"] == (RosterStatus.READY, None)
    assert r["UB"][0] is RosterStatus.NEEDS_ACTION and "Passport expiry date" in r["UB"][1]
    assert r["UC"] == (RosterStatus.NEEDS_ACTION, "not set up in travellers.json")
    assert "doesn't match travellers.json name" in r["UD"][1]

    card = slack.card
    assert card["text"] == "Group booking: 1 ready / 3 need action"
    [actions] = [b for b in card["blocks"] if b["type"] == "actions"]
    book, cancel = actions["elements"]
    assert (book["action_id"], book["value"]) == (CONFIRM_ACTION, str(rid))
    assert book["text"]["text"] == "Book 1 traveller · ₹25,000"
    assert cancel["action_id"] == CANCEL_ACTION


def test_reasons_never_contain_document_values(ops_session):
    slack = Slack(NAMES)
    rid = start(slack, deps())
    blob = str(roster(ops_session, rid)) + str(slack.card)
    assert "Z1234567" not in blob and PROFILES["UB"]["passport_expiry"] not in blob


def test_fare_over_cap_blocks_everyone(ops_session):
    slack = Slack(NAMES)
    rid = start(slack, deps(), policy(cap_paise=2_000_000))
    assert "over the cap ₹20,000" in roster(ops_session, rid)["UA"][1]
    [actions] = [b for b in slack.card["blocks"] if b["type"] == "actions"]
    assert [e["action_id"] for e in actions["elements"]] == [CANCEL_ACTION]  # nothing to book


# --- confirm ---------------------------------------------------------------------------------


def test_confirm_books_clean_travellers_and_reports(ops_session):
    slack, sup = Slack(NAMES), Picky()
    d = deps(sup)
    rid = start(slack, d)

    assert group.confirm(slack, rid, ADMIN, deps=d, policy=policy()) is True

    assert roster(ops_session, rid)["UA"][0] is RosterStatus.BOOKED
    assert [c[0] for c in sup.calls] == ["price_check", "book"]  # only the clean traveller
    with ops_session() as s:
        b = s.scalars(select(Booking)).one()
        assert (b.fare_paise, b.route, b.pnr) == (2_500_000, "DEL-DXB", "PNR001")
        assert [e.amount_paise for e in s.scalars(select(LedgerEntry))] == [2_500_000]
        assert s.get(Request, rid).status is RequestStatus.PARTIAL
    assert slack.dms == [("UA", slack.dms[0][1])] and "PNR *PNR001*" in slack.dms[0][1]

    card = slack.card
    assert card["text"] == "Group booking: 1 booked / 3 need action"
    retry_buttons = [b["accessory"] for b in card["blocks"] if b.get("accessory")]
    assert sorted(x["value"] for x in retry_buttons) == [f"{rid}:UB", f"{rid}:UC", f"{rid}:UD"]
    assert all(x["action_id"] == RETRY_ACTION and x["text"]["text"] == "Fix & retry" for x in retry_buttons)


def test_passport_data_sent_for_international_not_domestic(ops_session):
    sup = Picky()
    rid = start(Slack(NAMES), deps(sup))
    group.confirm(Slack(NAMES), rid, ADMIN, deps=deps(sup), policy=policy())
    assert sup.sent[0][0].extra["passport_number"] == "Z1234567"

    sup2 = Picky()
    rid2 = start(Slack(NAMES), deps(sup2, pick=domestic_pick()), text="DEL BOM for <@UA>")
    group.confirm(Slack(NAMES), rid2, ADMIN, deps=deps(sup2, pick=domestic_pick()), policy=policy())
    assert "passport_number" not in sup2.sent[0][0].extra


def test_double_confirm_books_once(ops_session):
    sup = Picky()
    d = deps(sup)
    slack = Slack(NAMES)
    rid = start(slack, d)
    assert group.confirm(slack, rid, ADMIN, deps=d, policy=policy())
    assert group.confirm(slack, rid, ADMIN, deps=d, policy=policy()) is False
    assert [c[0] for c in sup.calls].count("book") == 1


def test_one_refusal_does_not_stop_the_others(ops_session):
    profiles = {**PROFILES, "UB": person("Ravi", "Kumar")}
    sup = Picky(refuse={"UA"})
    d = deps(sup, profiles=profiles)
    slack = Slack(NAMES)
    rid = start(slack, d, text="DEL Dubai for <@UA> <@UB>")
    group.confirm(slack, rid, ADMIN, deps=d, policy=policy())
    r = roster(ops_session, rid)
    assert r["UA"] == (RosterStatus.NEEDS_ACTION, "fare sold out")
    assert r["UB"][0] is RosterStatus.BOOKED


def test_only_requester_or_admin_can_act(ops_session):
    slack, d = Slack(NAMES), deps()
    rid = start(slack, d)
    assert group.confirm(slack, rid, "UA", deps=d, policy=policy()) is False
    assert group.cancel(slack, rid, "UA", policy=policy()) is False


def test_cancel_before_confirm(ops_session):
    slack, d = Slack(NAMES), deps()
    rid = start(slack, d)
    assert group.cancel(slack, rid, ADMIN, policy=policy())
    assert slack.card["text"] == "Group booking cancelled"
    assert group.confirm(slack, rid, ADMIN, deps=d, policy=policy()) is False
    assert set(s for s, _ in roster(ops_session, rid).values()) == {RosterStatus.CANCELLED}


def test_dry_run_books_nothing(ops_session):
    sup = Picky()
    d, p = deps(sup), policy(dry_run=True)
    slack = Slack(NAMES)
    rid = start(slack, d, p)
    assert "DRY_RUN is on" in str(slack.card["blocks"])
    group.confirm(slack, rid, ADMIN, deps=d, policy=p)
    assert sup.calls == []
    assert roster(ops_session, rid)["UA"][0] is RosterStatus.DRY_RUN
    assert slack.card["text"] == "Group booking: 1 would be booked (dry run) / 3 need action"
    with ops_session() as s:
        assert s.scalar(select(func.count()).select_from(Booking)) == 0


# --- fix & retry -------------------------------------------------------------------------------


def test_fix_and_retry_rebooks_only_that_traveller(ops_session):
    profiles = dict(PROFILES)
    sup = Picky()
    d = deps(sup, profiles=profiles)
    slack = Slack(NAMES)
    rid = start(slack, d)
    group.confirm(slack, rid, ADMIN, deps=d, policy=policy())

    # Still broken → stays, no supplier call
    assert group.retry(slack, rid, "UB", ADMIN, deps=d, policy=policy())
    assert roster(ops_session, rid)["UB"][0] is RosterStatus.NEEDS_ACTION
    assert [c[0] for c in sup.calls].count("book") == 1

    profiles["UB"] = person("Ravi", "Kumar")  # passport renewed
    assert group.retry(slack, rid, "UB", ADMIN, deps=d, policy=policy())
    r = roster(ops_session, rid)
    assert r["UB"][0] is RosterStatus.BOOKED and r["UA"][0] is RosterStatus.BOOKED
    assert [c[0] for c in sup.calls].count("book") == 2
    assert sup.sent[-1][1].endswith("-a2")  # a new key per retry: no cached refusal
    assert slack.card["text"] == "Group booking: 2 booked / 2 need action"


def test_retry_not_offered_before_confirm(ops_session):
    slack, d = Slack(NAMES), deps()
    rid = start(slack, d)
    assert group.retry(slack, rid, "UB", ADMIN, deps=d, policy=policy()) is False


def test_retry_on_booked_traveller_is_ignored(ops_session):
    slack, sup = Slack(NAMES), Picky()
    d = deps(sup)
    rid = start(slack, d)
    group.confirm(slack, rid, ADMIN, deps=d, policy=policy())
    assert group.retry(slack, rid, "UA", ADMIN, deps=d, policy=policy()) is False
    assert [c[0] for c in sup.calls].count("book") == 1


# --- pending → ticketed ------------------------------------------------------------------------


def test_pending_ticket_is_recorded_when_issued(ops_session):
    sup = Picky(book_state="pending")
    d = deps(sup)
    slack = Slack(NAMES)
    rid = start(slack, d, text="DEL Dubai for <@UA>")
    group.confirm(slack, rid, ADMIN, deps=d, policy=policy())
    assert roster(ops_session, rid)["UA"][0] is RosterStatus.PENDING
    assert "ticket on the way" in slack.card["text"]
    with ops_session() as s:  # recorded at once: pending booking + pending debit
        b = s.scalars(select(Booking)).one()
        assert b.status is BookingStatus.PENDING
        assert [(e.status, e.amount_paise) for e in s.scalars(select(LedgerEntry))] == [
            (LedgerStatus.PENDING, 2_500_000)
        ]

    assert group.poll_pending(sup, slack) == 0  # still pending
    ref = next(iter(sup.bookings))
    sup.bookings[ref] = replace(sup.bookings[ref], state="ticketed", pnr="TKT123")
    assert group.poll_pending(sup, slack) == 1
    assert group.poll_pending(sup, slack) == 0  # idempotent

    assert roster(ops_session, rid)["UA"][0] is RosterStatus.BOOKED
    with ops_session() as s:
        b = s.scalars(select(Booking)).one()
        assert b.pnr == "TKT123"
        [debit] = s.scalars(select(LedgerEntry)).all()  # the same row, now confirmed
        assert debit.status is LedgerStatus.CONFIRMED
        assert s.get(Request, rid).status is RequestStatus.BOOKED
    assert any("TKT123" in t for _, t in slack.dms)
    assert slack.card["text"] == "Group booking: 1 booked / 0 need action"


def test_pending_too_long_needs_action(ops_session):
    sup = Picky(book_state="pending")
    d = deps(sup)
    slack = Slack(NAMES)
    rid = start(slack, d, text="DEL Dubai for <@UA>")
    group.confirm(slack, rid, ADMIN, deps=d, policy=policy())
    group.poll_pending(sup, slack, now=utcnow() + timedelta(hours=3))
    status, reason = roster(ops_session, rid)["UA"]
    assert status is RosterStatus.NEEDS_ACTION and "not ticketed after 2h" in reason


# --- wiring ------------------------------------------------------------------------------------


def test_register_wires_group_actions():
    class FakeApp:
        def __init__(self):
            self.actions = {}

        def action(self, action_id):
            return lambda fn: self.actions.setdefault(action_id, fn)

    app = FakeApp()
    group.register(app)
    assert {CONFIRM_ACTION, RETRY_ACTION, CANCEL_ACTION} <= set(app.actions)


def test_bot_routes_group_requests(monkeypatch):
    from traveldesk.slack import app

    seen = []
    monkeypatch.setattr(app._ops_group, "start_from_bot", lambda client, user, text, dm: seen.append(text))
    slack = Slack()
    app.start_request(slack, "UEMP1", "DEL to BOM for <@UX> <@UY>", "D1")
    assert seen == ["DEL to BOM for <@UX> <@UY>"]


def test_bot_keeps_single_requests(monkeypatch):
    from traveldesk.slack import app

    monkeypatch.setattr(app._ops_group, "start_from_bot", lambda *a: pytest.fail("not a group request"))
    monkeypatch.setattr(app.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())
    app.start_request(Slack(), "UEMP1", "DEL to BOM tomorrow", "D1")
