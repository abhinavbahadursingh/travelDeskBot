"""Messages in the bot's DM start a request just like /traveldesk."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app as bot_app
import traveller_profile as tp

PROFILE = {"given_name": "Raj", "family_name": "Gupta", "email": "raj@example.com"}


@pytest.fixture
def threads(monkeypatch):
    started = []
    monkeypatch.setattr(bot_app.threading, "Thread",
                        lambda target=None, args=(), kwargs=None, **_k: SimpleNamespace(
                            start=lambda: started.append((target, args))))
    return started


@pytest.fixture
def client():
    c = MagicMock()
    c.chat_postMessage.return_value = {"ts": "9.9"}
    return c


def _dm(text, **extra):
    return {"type": "message", "channel_type": "im", "channel": "DCHAN",
            "user": "UDM1", "text": text, "ts": "1.0", **extra}


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


@pytest.mark.parametrize("event", [
    _dm("Dry run — would book …", bot_id="B1"),               # the bot's own post
    _dm("edited", subtype="message_changed"),
    {"type": "message", "channel_type": "channel", "channel": "C1", "user": "UDM1",
     "text": "DEL to BOM", "ts": "1.0"},                       # public channel
])
def test_ignored_messages(threads, client, event):
    bot_app.on_message(event, client)
    assert threads == []
    client.chat_postMessage.assert_not_called()


def test_thread_reply_gets_hint_not_search(threads, client):
    bot_app.on_message(_dm("thanks", thread_ts="0.5"), client)
    assert threads == []
    assert "new message" in _texts(client)[0]
