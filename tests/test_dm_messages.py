"""Messages in the bot's DM start a request just like /traveldesk."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from traveldesk.booking import traveller_profile as tp
from traveldesk.slack import app as bot_app

PROFILE = {"given_name": "Raj", "family_name": "Gupta", "email": "raj@example.com"}


@pytest.fixture
def threads(monkeypatch):
    started = []
    monkeypatch.setattr(
        bot_app.threading,
        "Thread",
        lambda target=None, args=(), kwargs=None, **_k: SimpleNamespace(
            start=lambda: started.append((target, args))
        ),
    )
    return started


@pytest.fixture
def client():
    c = MagicMock()
    c.chat_postMessage.return_value = {"ts": "9.9"}
    return c


def _dm(text, **extra):
    return {
        "type": "message",
        "channel_type": "im",
        "channel": "DCHAN",
        "user": "UDM1",
        "text": text,
        "ts": "1.0",
        **extra,
    }


def _texts(client):
    return [c.kwargs.get("text", "") for c in client.chat_postMessage.call_args_list]


def test_dm_starts_search_like_slash_command(monkeypatch, threads, client):
    monkeypatch.setitem(tp.config.TRAVELLERS, "UDM1", dict(PROFILE))
    bot_app.on_message(_dm("DEL to BOM 5 Dec"), client)
    target, args = threads[0]
    assert target is bot_app.run_search
    _client, channel, thread_ts, user, text, traveller = args
    assert (channel, thread_ts, user, text) == ("DCHAN", "9.9", "UDM1", "DEL to BOM 5 Dec")
    assert traveller["given_name"] == "Raj"
    assert "Searching flights" in _texts(client)[0]
    client.conversations_open.assert_not_called()  # replies in the same DM


@pytest.mark.parametrize("text", ["hi", "Hello!", "help", ""])
def test_dm_greeting_gets_help(threads, client, text):
    bot_app.on_message(_dm(text), client)
    assert threads == []
    assert "How to use" in _texts(client)[0]


def test_dm_from_unknown_user_is_told_to_ask_admin(threads, client):
    bot_app.on_message(_dm("DEL to BOM 5 Dec", user="UNOBODY"), client)
    assert threads == []
    assert "UNOBODY" in _texts(client)[0]


@pytest.mark.parametrize(
    "event",
    [
        _dm("Dry run — would book …", bot_id="B1"),  # the bot's own post
        _dm("edited", subtype="message_changed"),
        {
            "type": "message",
            "channel_type": "channel",
            "channel": "C1",
            "user": "UDM1",
            "text": "DEL to BOM",
            "ts": "1.0",
        },  # public channel
    ],
)
def test_ignored_messages(threads, client, event):
    bot_app.on_message(event, client)
    assert threads == []
    client.chat_postMessage.assert_not_called()


def test_thread_reply_gets_hint_not_search(threads, client):
    bot_app.on_message(_dm("thanks", thread_ts="0.5"), client)
    assert threads == []
    assert "new message" in _texts(client)[0]


def _stopped_search(user, thread_ts, status="need_info", **details):
    from traveldesk.storage import db

    rid, _ = db.new_request(user, "2 tickets delhi to baku next sunday", "claude", thread_ts=thread_ts)
    db.update(rid, status=status, details={"status": status, **details})
    return rid


def test_thread_reply_to_stopped_search_searches_again(monkeypatch, threads, client):
    monkeypatch.setitem(tp.config.TRAVELLERS, "UFOLLOW1", dict(PROFILE))
    _stopped_search("UFOLLOW1", "7.1", search_stopped=True)
    bot_app.on_message(_dm("yes, Baku is right", user="UFOLLOW1", thread_ts="7.1"), client)
    target, args = threads[0]
    assert target is bot_app.run_search
    assert args[4] == "2 tickets delhi to baku next sunday — yes, Baku is right"


def test_thread_reply_after_booking_phase_need_info_gets_hint(monkeypatch, threads, client):
    monkeypatch.setitem(tp.config.TRAVELLERS, "UFOLLOW2", dict(PROFILE))
    _stopped_search("UFOLLOW2", "7.2")  # need_info from booking, not from the search
    bot_app.on_message(_dm("yes", user="UFOLLOW2", thread_ts="7.2"), client)
    assert threads == []
    assert "new message" in _texts(client)[0]


def test_thread_reply_from_someone_else_gets_hint(monkeypatch, threads, client):
    _stopped_search("UFOLLOW3", "7.3", search_stopped=True)
    bot_app.on_message(_dm("yes", user="UOTHER", thread_ts="7.3"), client)
    assert threads == []
    assert "new message" in _texts(client)[0]


def test_heartbeat_logs_until_stopped(caplog):
    import logging
    import threading
    import time

    stop = threading.Event()
    with caplog.at_level(logging.INFO, logger="traveldesk"):
        t = threading.Thread(target=bot_app._heartbeat, args=(stop, 0.01, time.monotonic()))
        t.start()
        time.sleep(0.05)
        stop.set()
        t.join(1)
    assert not t.is_alive()
    assert any("heartbeat: alive" in r.getMessage() for r in caplog.records)


def _card_text(blocks):
    out = []
    for b in blocks:
        if b.get("text"):
            out.append(b["text"]["text"])
        out += [f["text"] for f in b.get("fields", [])]
        out += [e["text"] for e in b.get("elements", []) if e.get("type") == "mrkdwn"]
        out += [e["confirm"]["text"]["text"] for e in b.get("elements", []) if e.get("confirm")]
    return "\n".join(out)


def _card(**picked):
    base = {
        "summary": "Air India · 29 Nov 04:15 DEL → 07:50 GYD · nonstop",
        "price": 58238.0,
        "currency": "INR",
    }
    return _card_text(bot_app._confirm_blocks(1, "U", "2 tickets delhi to baku", {**base, **picked}, PROFILE))


def test_card_shows_tickets_per_ticket_price_and_total(monkeypatch):
    monkeypatch.setattr(bot_app.config, "DRY_RUN", False)
    text = _card(passengers_requested=1)
    assert "*Tickets*\n1" in text
    assert "*Price per ticket*\n₹58,238" in text
    assert "*Total*\n*₹58,238*" in text
    assert "book 1 ticket for ₹58,238 total" in text
    assert "You asked for" not in text


def test_card_warns_when_more_tickets_were_asked_for():
    text = _card(passengers_requested=2)
    assert "You asked for *2 tickets*, but I can only book *1*" in text


@pytest.mark.parametrize(
    ("amount", "currency", "shown"),
    [
        (58238.0, "INR", "₹58,238"),
        (99.5, "USD", "$99.50"),
        (1200, "AZN", "1,200 AZN"),
        (None, "INR", "price n/a"),
    ],
)
def test_money(amount, currency, shown):
    assert bot_app._money(amount, currency) == shown
