"""Slack button handlers for ops messages, registered on the bot's Bolt app."""

from __future__ import annotations

import logging
from typing import Any

from traveldesk.ops import db as ops_db
from traveldesk.ops.digest import OPEN_ACTION, OPEN_PAY_ACTION, RESOLVE_ACTION
from traveldesk.ops.models import Booking, Event
from traveldesk.ops.notify import ACK_ACTION, acknowledged_blocks
from traveldesk.ops.records import add_event
from traveldesk.ops.units import fmt_ist, utcnow

log = logging.getLogger("traveldesk.ops.slack")


def handle_ack(body: dict, client: Any) -> bool:
    """[Acknowledge] on an alert. Records it once (first click wins) and updates the clicked message.

    Returns True when this click was the first acknowledgement.
    """
    event_id = int(body["actions"][0]["value"])
    user = body["user"]["id"]
    now = utcnow()
    with ops_db.session_scope() as s:
        alert = s.get(Event, event_id)
        if alert is None:
            log.warning("ack for unknown ops event %s", event_id)
            return False
        first = (
            add_event(
                s,
                alert.booking,
                "acknowledged",
                {"event_id": event_id, "kind": alert.kind, "user": user},
                dedupe_key=f"ack:{event_id}",
            )
            is not None
        )
        if first:
            alert.payload_json = {**(alert.payload_json or {}), "acked_by": user, "acked_at": now.isoformat()}

    msg = body.get("message") or {}
    channel = (body.get("channel") or body.get("container") or {}).get("id") or (
        body.get("container") or {}
    ).get("channel_id")
    if channel and msg.get("ts"):
        try:
            client.chat_update(
                channel=channel,
                ts=msg["ts"],
                text=msg.get("text") or "Acknowledged",
                blocks=acknowledged_blocks(msg.get("blocks") or [], user, fmt_ist(now, "%d %b %H:%M IST")),
            )
        except Exception:  # noqa: BLE001 — the ack is recorded; a failed cosmetic update is fine
            log.exception("could not update acknowledged message")
    return first


def _channel(body: dict) -> str | None:
    return (body.get("channel") or {}).get("id") or (body.get("container") or {}).get("channel_id")


def handle_resolve(body: dict, client: Any) -> bool:
    """[Mark resolved] on a digest item: hide it from later digests, mark it in this message.

    Returns True when this click was the first to resolve it.
    """
    bucket, _, bid = body["actions"][0]["value"].partition(":")
    user = body["user"]["id"]
    now = utcnow()
    with ops_db.session_scope() as s:
        booking = s.get(Booking, int(bid))
        if booking is None:
            log.warning("resolve for unknown ops booking %s", bid)
            return False
        first = (
            add_event(
                s,
                booking,
                "leak_resolved",
                {"bucket": bucket, "user": user},
                dedupe_key=f"leak_resolved:{bucket}:{booking.id}",
                created_at=now,
            )
            is not None
        )

    msg = body.get("message") or {}
    channel = _channel(body)
    if channel and msg.get("ts"):
        block_id = f"leak:{bucket}:{bid}"
        done = {
            "type": "context",
            "block_id": block_id,
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f":white_check_mark: Resolved by <@{user}> · {fmt_ist(now, '%H:%M IST')}",
                }
            ],
        }
        blocks = [done if b.get("block_id") == block_id else b for b in msg.get("blocks") or []]
        try:
            client.chat_update(channel=channel, ts=msg["ts"], text=msg.get("text") or "Digest", blocks=blocks)
        except Exception:  # noqa: BLE001 — the resolution is recorded; a failed cosmetic update is fine
            log.exception("could not update digest message")
    return first


def register(app: Any) -> None:
    @app.action(ACK_ACTION)
    def _on_ack(ack, body, client):
        ack()
        handle_ack(body, client)

    @app.action(RESOLVE_ACTION)
    def _on_resolve(ack, body, client):
        ack()
        handle_resolve(body, client)

    @app.action(OPEN_ACTION)
    def _on_open(ack):
        ack()  # link button: Slack opens the URL; it still sends us the click, which must be acked

    @app.action(OPEN_PAY_ACTION)
    def _on_open_pay(ack):
        ack()  # link button, as above

    from traveldesk.ops import group

    group.register(app)
