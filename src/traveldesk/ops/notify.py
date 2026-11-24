"""Outbound Slack messages for ops alerts (Block Kit), behind a small Notifier protocol.

The scheduler process posts with the bot token; button clicks come back to the
bot's Socket Mode app (see ops/slack_actions.py), so no public URL is needed.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

log = logging.getLogger("traveldesk.ops.notify")

ACK_ACTION = "ops_ack"


class Notifier(Protocol):
    def post(self, channel: str, text: str, blocks: list[dict] | None = None) -> str | None:
        """Post to a channel; returns the message ts."""
        ...

    def dm(self, user_id: str, text: str, blocks: list[dict] | None = None) -> str | None:
        """Direct-message a Slack user; returns the message ts."""
        ...


class SlackNotifier:
    def __init__(self, token: str, client: Any | None = None):
        if client is None:
            from slack_sdk import WebClient

            client = WebClient(token=token)
        self.client = client

    def post(self, channel: str, text: str, blocks: list[dict] | None = None) -> str | None:
        resp = self.client.chat_postMessage(channel=channel, text=text, blocks=blocks, unfurl_links=False)
        return resp.get("ts")

    def dm(self, user_id: str, text: str, blocks: list[dict] | None = None) -> str | None:
        channel = self.client.conversations_open(users=user_id)["channel"]["id"]
        return self.post(channel, text, blocks)


class LogNotifier:
    """No Slack token (dry runs, `--once` on a laptop): write messages to the log instead."""

    def post(self, channel: str, text: str, blocks: list[dict] | None = None) -> str | None:
        log.info("[would post to %s] %s", channel, text)
        return None

    def dm(self, user_id: str, text: str, blocks: list[dict] | None = None) -> str | None:
        log.info("[would DM %s] %s", user_id, text)
        return None


def alert_blocks(text: str, detail: str, event_id: int) -> list[dict]:
    """Headline + detail + [Acknowledge]. The button carries the ops_events id."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{text}*\n{detail}"}},
        {
            "type": "actions",
            "block_id": f"ops_alert_{event_id}",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACK_ACTION,
                    "text": {"type": "plain_text", "text": "Acknowledge"},
                    "style": "primary",
                    "value": str(event_id),
                }
            ],
        },
    ]


def acknowledged_blocks(blocks: list[dict], who: str, when: str) -> list[dict]:
    """The same message with its buttons replaced by who acknowledged it."""
    kept = [b for b in blocks or [] if b.get("type") != "actions"]
    return kept + [
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f":white_check_mark: Acknowledged by <@{who}> · {when}"}],
        }
    ]
