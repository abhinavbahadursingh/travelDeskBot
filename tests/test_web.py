"""Web booking service: picks, the Slack hand-off, and the HTTP API.

LetsFG, Claude and Slack are stubbed; nothing leaves the machine.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from tests.test_need_info_flow import client  # noqa: F401 — fixture
from traveldesk.storage import db
from traveldesk.web import offers, server
from traveldesk.web.booking import BookingError, send_to_slack
from traveldesk.web.searches import Search, SearchStore

USER = "UEMP1"  # in the conftest travellers.json
ROUTE = {
    "origin": "DEL",
    "destination": "BOM",
    "date_from": "2026-12-05",
    "return_from": None,
    "passengers": 1,
}


def offer(
    oid, price, dep="07:10", arr="09:20", stops=0, refund=None, change=None, checked=None, airline="6E"
):
    segs = [
        {
            "airline": airline,
            "airline_name": {"6E": "IndiGo", "QP": "Akasa Air", "AI": "Air India"}[airline],
            "flight_no": f"{oid}{i}",
            "origin": "DEL" if i == 0 else "AMD",
            "destination": "BOM" if i == stops else "AMD",
            "departure": f"2026-12-05T{dep}:00",
            "arrival": f"2026-12-05T{arr}:00",
            "cabin_class": "economy",
        }
        for i in range(stops + 1)
    ]
    h1, m1 = map(int, dep.split(":"))
    h2, m2 = map(int, arr.split(":"))
    return {
        "id": oid,
        "price_total": price,
        "currency": "INR",
        "owner_airline": airline,
        "outbound": {"segments": segs, "total_duration_seconds": ((h2 * 60 + m2) - (h1 * 60 + m1)) * 60},
        "conditions": {
            "refund_before_departure": refund,
            "change_before_departure": change,
            "checked_bag": checked,
        },
        "booking_url": f"https://letsfg.example/{oid}",
    }


CHEAP_STOP = offer("A", 6000, "10:00", "14:50", stops=1)
DIRECT = offer("B", 7200, "07:10", "09:20", checked="checked bag included")
DIRECT_SLOW = offer("C", 7000, "06:00", "09:30")
FLEX = offer("D", 9800, "08:30", "10:35", refund="allowed_with_fee", change="allowed_with_fee", airline="AI")
OFFERS = [CHEAP_STOP, DIRECT, DIRECT_SLOW, FLEX]


def done_search(user=USER, items=OFFERS):
    return Search(
        id="s1",
        user=user,
        text="DEL to BOM 5 Dec",
        status="ok",
        route=ROUTE,
        search_id="S1",
        offers=items,
        found=9,
    )


# --- picks -------------------------------------------------------------------------------


def test_picks_are_best_value_cheapest_and_most_flexible():
    picks = offers.rank(OFFERS)
    assert [(tag[0], o["id"]) for tag, o in picks] == [
        ("Best value", "B"),  # cheapest nonstop within 25% of the quickest nonstop (C is too slow)
        ("Cheapest", "A"),
        ("Most flexible", "D"),
    ]


def test_when_best_value_is_also_cheapest_the_second_pick_is_fastest():
    picks = offers.rank([DIRECT, offer("E", 9000, "07:00", "08:50"), FLEX])
    assert [(tag[0], o["id"]) for tag, o in picks] == [
        ("Best value", "B"),
        ("Fastest", "E"),
        ("Most flexible", "D"),
    ]


def test_fewer_offers_give_fewer_picks():
    assert [o["id"] for _, o in offers.rank([DIRECT])] == ["B"]
    assert offers.rank([]) == []


def test_card_text():
    c = offers.card(FLEX, offers.FLEXIBLE)
    assert c["airline"] == "Air India · Nonstop" and (c["dep"], c["arr"], c["duration"]) == (
        "08:30",
        "10:35",
        "2h 05m",
    )
    assert (
        c["fare_rules"] == "Refundable with a fee" and c["tag"] == "Most flexible" and c["tone"] == "violet"
    )
    assert offers.card(DIRECT)["bags"] == "Checked bag included"
    assert offers.card(CHEAP_STOP)["airline"] == "IndiGo · 1 stop"
    stop = offers.card(CHEAP_STOP)
    assert stop["stops"] == 1 and stop["stop_airports"] == ["AMD"] and stop["airline_code"] == "6E"
    assert offers.card(DIRECT)["stops"] == 0 and offers.card(DIRECT)["stop_airports"] == []
    assert stop["duration_seconds"] and stop["duration_seconds"] > offers.card(DIRECT)["duration_seconds"]


def test_pick_record_has_what_the_slack_booking_flow_needs():
    rec = offers.pick_record(ROUTE, "S1", DIRECT, offers_found=9)
    for key in (
        "search_id",
        "offer_id",
        "price",
        "currency",
        "summary",
        "domestic",
        "signature",
        "depart_date",
    ):
        assert rec[key] is not None
    assert rec["status"] == "picked" and rec["backend"] == "rest" and rec["model"] == "web"
    assert rec["offer_id"] == "B" and rec["domestic"] is True


# --- search jobs ---------------------------------------------------------------------------


def wait(store, job_id):
    for _ in range(200):
        job = store.get(job_id)
        if job.status != "running":
            return job
        time.sleep(0.01)
    pytest.fail("search never finished")


def test_search_job_returns_picks_and_all_flights():
    store = SearchStore(
        runner=lambda text: {"status": "ok", "route": ROUTE, "search_id": "S1", "offers": OFFERS, "found": 30}
    )
    job = wait(store, store.start(USER, "DEL to BOM").id)
    out = job.to_json()
    assert out["status"] == "ok" and out["found"] == 30 and len(out["picks"]) == 3
    assert [c["id"] for c in out["all"]] == ["A", "C", "B", "D"]  # cheapest first


def test_search_job_reports_runner_errors():
    def boom(text):
        raise RuntimeError("letsfg down")

    store = SearchStore(runner=boom)
    job = wait(store, store.start(USER, "x").id)
    assert job.status == "failed" and "RuntimeError" in job.message


def test_need_info_is_passed_through():
    store = SearchStore(runner=lambda text: {"status": "need_info", "message": "Which date?"})
    job = wait(store, store.start(USER, "x").id)
    assert job.to_json()["message"] == "Which date?" and "picks" not in job.to_json()


# --- hand-off to Slack ---------------------------------------------------------------------


def test_sending_creates_the_row_and_posts_the_standard_card(client, monkeypatch):
    cards = []
    monkeypatch.setattr("traveldesk.slack.app._post_confirmation_card", lambda *a: cards.append(a))
    row_id = send_to_slack(done_search(), "B", USER, client)
    row = db.get(row_id)
    assert row["status"] == "awaiting_confirm" and row["slack_user"] == USER and row["price"] == 7200
    assert row["slack_thread_ts"] == "5.0"  # the intro message starts the request's thread
    assert json.loads(row["details"])["offer_id"] == "B"
    intro = client.chat_postMessage.call_args.kwargs
    assert intro["channel"] == f"D-{USER}" and "web page" in intro["text"]
    (c, rid, user, text, picked, dm, traveller) = cards[0]
    assert (rid, user, dm, picked["offer_id"]) == (row_id, USER, f"D-{USER}", "B")
    assert "passport_number" not in traveller  # public view only, as in Slack
    db.update(row_id, status="cancelled")  # keep later duplicate checks clean


def test_someone_elses_search_is_refused(client):
    with pytest.raises(BookingError) as e:
        send_to_slack(done_search(user="UOTHER"), "B", USER, client)
    assert e.value.status == 403


def test_unknown_offer_is_refused(client):
    with pytest.raises(BookingError) as e:
        send_to_slack(done_search(), "nope", USER, client)
    assert e.value.status == 404
    client.chat_postMessage.assert_not_called()


def test_duplicate_request_in_flight_is_refused(client, monkeypatch):
    monkeypatch.setattr("traveldesk.slack.app._post_confirmation_card", lambda *a: None)
    first = send_to_slack(done_search(), "B", USER, client)
    with pytest.raises(BookingError) as e:
        send_to_slack(done_search(), "D", USER, client)
    assert e.value.status == 409 and f"#{first}" in e.value.message
    db.update(first, status="cancelled")


def test_traveller_not_in_travellers_json_cannot_search():
    api = server.Api(searches=SearchStore(runner=lambda t: {"status": "ok"}))
    status, body = api.handle("POST", "/api/book/searches", {"user": "UNOPE", "text": "DEL to BOM"})
    assert status == 403 and "travellers.json" in body["error"]


# --- HTTP ---------------------------------------------------------------------------------------


@pytest.fixture
def http(client, monkeypatch):
    monkeypatch.setattr("traveldesk.slack.app._post_confirmation_card", lambda *a: None)
    store = SearchStore(
        runner=lambda text: {"status": "ok", "route": ROUTE, "search_id": "S1", "offers": OFFERS, "found": 4}
    )
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(server.Api(store, slack_client=client)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def call(base, method, path, body=None):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body else None)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_search_then_send(http):
    status, people = call(http, "GET", "/api/book/travellers")
    assert status == 200 and people[0]["id"] == USER and people[0]["name"] == "Test User"
    assert set(people[0]) == {"id", "name", "ready", "problems", "passport_on_file"}  # no profile values
    assert isinstance(people[0]["passport_on_file"], bool)
    status, job = call(
        http, "POST", "/api/book/searches", {"user": USER, "text": "DEL to BOM 5 Dec, morning"}
    )
    assert status == 202 and job["status"] in ("running", "ok")
    for _ in range(200):
        status, job = call(http, "GET", f"/api/book/searches/{job['id']}")
        if job["status"] != "running":
            break
        time.sleep(0.01)
    assert job["status"] == "ok" and job["picks"][0]["id"] == "B"
    status, out = call(http, "POST", "/api/book/requests", {"user": USER, "search": job["id"], "offer": "B"})
    assert status == 201 and db.get(out["booking_id"])["status"] == "awaiting_confirm"
    db.update(out["booking_id"], status="cancelled")


def test_http_errors_are_json(http):
    assert call(http, "GET", "/api/book/nope")[0] == 404
    assert call(http, "GET", "/api/book/searches/missing") == (
        404,
        {"error": "Search not found or expired. Search again."},
    )
    assert call(http, "POST", "/api/book/searches", {"user": USER, "text": ""})[0] == 400


# --- confirm on the web ---------------------------------------------------------------------

from traveldesk.slack import app as bot_app  # noqa: E402
from traveldesk.storage import secure_store  # noqa: E402
from traveldesk.web import booking as web_booking  # noqa: E402
from traveldesk.web import passenger  # noqa: E402


def awaiting_row(user=USER):
    rid, _ = db.new_request(user, "DEL to BOM 5 Dec", "claude", thread_ts="100.1")
    db.update(
        rid,
        status="awaiting_confirm",
        details=offers.pick_record(ROUTE, "S1", DIRECT, offers_found=4),
        manager_channel=f"D-{user}",
        manager_message_ts="200.2",
    )
    return rid


@pytest.fixture
def details_ok(monkeypatch):
    monkeypatch.setattr(passenger, "form", lambda profile, picked: None)


def test_web_confirm_marks_the_row_for_the_bot(details_ok):
    rid = awaiting_row()
    assert web_booking.confirm_on_web(rid, USER) == {"booking_id": rid, "status": "web_confirmed"}
    r = db.get(rid)
    assert r["status"] == "web_confirmed" and r["approver"] == USER and r["approval_ts"]
    assert r["confirmed_via"] == "web"
    db.update(rid, status="cancelled")


def test_only_the_traveller_can_confirm(details_ok):
    rid = awaiting_row()
    with pytest.raises(BookingError) as e:
        web_booking.confirm_on_web(rid, "USOMEONE")
    assert e.value.status == 403 and db.get(rid)["status"] == "awaiting_confirm"
    db.update(rid, status="cancelled")


def test_missing_passenger_details_return_the_form_without_values():
    rid = awaiting_row()  # the conftest profile has no date of birth / gender
    with pytest.raises(BookingError) as e:
        web_booking.confirm_on_web(rid, USER)
    assert e.value.status == 409
    fields = {f["name"]: f for f in e.value.extra["needs"]["fields"]}
    assert {"date_of_birth", "gender"} <= set(fields)
    assert fields["given_name"]["on_file"] and fields["given_name"]["optional"]  # saved: kept if blank
    assert all("value" not in f for f in fields.values())  # nothing saved is sent to the browser
    assert db.get(rid)["status"] == "awaiting_confirm"
    db.update(rid, status="cancelled")


def test_typed_details_are_checked_then_saved_encrypted_and_booking_proceeds(client):
    rid = awaiting_row()
    with pytest.raises(BookingError) as e:
        web_booking.confirm_on_web(rid, USER, {"date_of_birth": "2030-01-01", "gender": "m"}, client)
    assert e.value.status == 422 and "date_of_birth" in e.value.extra["field_errors"]
    client.chat_postMessage.assert_not_called()  # nothing saved, nothing to tell
    out = web_booking.confirm_on_web(rid, USER, {"date_of_birth": "1990-04-02", "gender": "f"}, client)
    assert out["status"] == "web_confirmed"
    note = client.chat_postMessage.call_args.kwargs
    assert "Passenger details saved" in note["text"] and note["thread_ts"] == "100.1"
    stored = secure_store.load(USER)
    assert stored["date_of_birth"] == "1990-04-02" and stored["gender"] == "f"
    secure_store.delete(USER)
    db.update(rid, status="cancelled")


def test_confirm_and_book_here_needs_no_slack_card(client, details_ok):
    rid = web_booking.confirm_and_book(done_search(), "B", USER, slack_client=client)
    r = db.get(rid)
    assert r["status"] == "web_confirmed" and r["approver"] == USER and r["slack_thread_ts"] == "5.0"
    assert r["manager_message_ts"] is None  # no card to click
    (note,) = texts(client)
    assert f"#{rid}" in note and "Confirmed on the travel desk web page" in note
    db.update(rid, status="cancelled")


def test_confirm_and_book_here_still_books_if_the_slack_note_fails(client, details_ok):
    client.chat_postMessage.side_effect = RuntimeError("slack down")
    rid = web_booking.confirm_and_book(done_search(), "B", USER, slack_client=client)
    assert db.get(rid)["status"] == "web_confirmed"
    db.update(rid, status="cancelled")


def test_bot_books_a_cardless_web_booking_without_a_second_note(client, monkeypatch, details_ok):
    booked = []
    monkeypatch.setattr(bot_app, "run_booking", lambda *a: booked.append(a))
    rid = web_booking.confirm_and_book(done_search(), "B", USER, slack_client=client)
    client.reset_mock()
    assert bot_app.start_web_confirmed(client) == 1
    for _ in range(100):
        if booked:
            break
        time.sleep(0.01)
    assert booked and booked[0][1] == rid and db.get(rid)["status"] == "confirmed"
    client.chat_postMessage.assert_not_called()
    client.chat_update.assert_not_called()
    db.update(rid, status="cancelled")


def test_confirm_twice_or_after_cancel_is_refused(details_ok):
    rid = awaiting_row()
    web_booking.confirm_on_web(rid, USER)
    with pytest.raises(BookingError) as e:
        web_booking.confirm_on_web(rid, USER)
    assert e.value.status == 409
    db.update(rid, status="cancelled")


def test_bot_books_web_confirmed_rows_like_a_slack_click(client, monkeypatch, details_ok):
    booked = []
    monkeypatch.setattr(bot_app, "run_booking", lambda *a: booked.append(a))
    rid = awaiting_row()
    web_booking.confirm_on_web(rid, USER)
    assert bot_app.start_web_confirmed(client) == 1
    for _ in range(100):
        if booked:
            break
        time.sleep(0.01)
    (c, row_id, employee, picked, traveller) = booked[0]
    assert (row_id, employee, picked["offer_id"]) == (rid, USER, "B")
    assert db.get(rid)["status"] == "confirmed"
    card = client.chat_update.call_args.kwargs
    assert card["ts"] == "200.2" and not any(b["type"] == "actions" for b in card["blocks"])
    assert any("confirmed on the web page" in t for t in texts(client))
    assert bot_app.start_web_confirmed(client) == 0  # only once
    db.update(rid, status="cancelled")


def test_cancelled_before_the_bot_saw_it_is_not_booked(client, monkeypatch, details_ok):
    booked = []
    monkeypatch.setattr(bot_app, "run_booking", lambda *a: booked.append(a))
    rid = awaiting_row()
    web_booking.confirm_on_web(rid, USER)
    db.update(rid, status="cancelled", approver=db.DASHBOARD)
    assert bot_app.start_web_confirmed(client) == 0 and not booked


def texts(client):
    return [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]


def test_card_carries_full_fare_conditions():
    rich = {
        **FLEX,
        "conditions": {
            "fare_family": "Eco Flex",
            "refund_before_departure": "allowed_with_fee",
            "refund_penalty_amount": "3500.00",
            "change_before_departure": "allowed",
            "checked_bag": "checked bag included (Up to 62in total)",
            "carry_on": "cabin bag included",
            "seat": "seat selection: from ~200.0 INR",
            "self_transfer": "self_transfer",
            "seller_label": "Air India",
        },
    }
    rows = {r["label"]: r["value"] for r in offers.card(rich)["fare_conditions"]}
    assert rows["Fare"] == "Eco Flex"
    assert rows["Cancel & refund before departure"] == "Allowed with a fee (3,500 INR)"
    assert rows["Change before departure"] == "Allowed, no fee"
    assert rows["No-show"].startswith("Not provided by the seller")
    assert rows["Checked bag"] == "Checked bag included (Up to 62in total)"
    assert "Separate tickets" in rows["Self-transfer"]
    assert rows["Sold by"] == "Air India"


def test_missing_fare_rules_are_never_guessed():
    rows = {r["label"]: r["value"] for r in offers.card({**FLEX, "conditions": {}})["fare_conditions"]}
    for label in ("Cancel & refund before departure", "Change before departure", "No-show", "Checked bag"):
        assert rows[label].startswith("Not provided by the seller")
