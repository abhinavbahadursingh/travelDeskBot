"""The web service's only contact with Slack.

It posts what the Slack bot posts at the end of a search — an intro message
(the request's thread) and the standard confirmation card — using the bot's
own card builder, so the card and its Confirm / Cancel buttons are identical
and are handled by the running Slack bot.
"""

from __future__ import annotations

from traveldesk.slack import app as slack_app


def client():
    return slack_app.app.client


def open_dm(slack_client, user: str) -> str:
    return slack_client.conversations_open(users=user)["channel"]["id"]


def post_intro(slack_client, dm: str, text: str) -> str:
    """Start the request's thread; returns its ts (stored as slack_thread_ts)."""
    msg = slack_client.chat_postMessage(
        channel=dm, text=f":airplane: *{text}*\nFlight chosen on the travel desk web page."
    )
    return msg["ts"]


def post_note(slack_client, dm: str, text: str, thread_ts: str | None = None) -> str:
    """A short line to the traveller; returns its ts."""
    kwargs = {"thread_ts": thread_ts} if thread_ts else {}
    return slack_client.chat_postMessage(channel=dm, text=text, **kwargs)["ts"]


def post_card(
    slack_client, row_id: int, user: str, text: str, picked: dict, dm: str, traveller: dict
) -> None:
    slack_app._post_confirmation_card(slack_client, row_id, user, text, picked, dm, traveller)
