"""Ops job runner (APScheduler). Run as its own process next to the bot:

    python -m traveldesk.ops.scheduler            # run forever
    python -m traveldesk.ops.scheduler --once watchdog   # one pass, then exit
    python -m traveldesk.ops.scheduler --once digest     # post today's digest (once per IST day)

Jobs use coalesce + max_instances=1 (a slow run never overlaps the next; missed runs
collapse into one). The jobs themselves are idempotent, so a restart mid-run is safe.
"""

from __future__ import annotations

import argparse
import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from traveldesk.ops.digest import post_digest
from traveldesk.ops.notify import LogNotifier, Notifier, SlackNotifier
from traveldesk.ops.settings import OpsSettings, get_settings
from traveldesk.ops.supplier import get_supplier
from traveldesk.ops.units import utcnow
from traveldesk.ops.watchdog import run_watchdog

log = logging.getLogger("traveldesk.ops.scheduler")


def make_notifier(cfg: OpsSettings) -> Notifier:
    if cfg.slack_bot_token:
        return SlackNotifier(cfg.slack_bot_token)
    log.warning("SLACK_BOT_TOKEN not set: alerts go to the log only")
    return LogNotifier()


def watchdog_job() -> None:
    cfg = get_settings()
    run_watchdog(get_supplier(), make_notifier(cfg), settings=cfg)


def digest_job() -> None:
    cfg = get_settings()
    post_digest(make_notifier(cfg), cfg=cfg)


def group_poll_job() -> None:
    """Pending group bookings → ticketed (the bot polls too; this covers bot restarts)."""
    from traveldesk.ops.group import poll_pending

    cfg = get_settings()
    notifier = make_notifier(cfg)
    poll_pending(get_supplier(), getattr(notifier, "client", None), ticket_timeout=cfg.ticket_timeout)


JOBS = {"watchdog": watchdog_job, "digest": digest_job, "group_poll": group_poll_job}


def build_scheduler(cfg: OpsSettings | None = None) -> BlockingScheduler:
    cfg = cfg or get_settings()
    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(
        watchdog_job,
        IntervalTrigger(hours=cfg.watchdog_interval_hours),
        id="watchdog",
        next_run_time=utcnow(),  # first pass at start-up
        max_instances=1,
        coalesce=True,
        misfire_grace_time=1800,
        replace_existing=True,
    )
    hour, minute = (int(x) for x in cfg.digest_time_ist.split(":"))
    sched.add_job(
        digest_job,
        CronTrigger(hour=hour, minute=minute, timezone="Asia/Kolkata"),
        id="digest",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3 * 3600,  # machine asleep at 19:00 → still post when it wakes, same evening
        replace_existing=True,
    )
    sched.add_job(
        group_poll_job,
        IntervalTrigger(minutes=5),
        id="group_poll",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
        replace_existing=True,
    )
    return sched


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="python -m traveldesk.ops.scheduler")
    ap.add_argument("--once", choices=sorted(JOBS), help="run one job once and exit")
    args = ap.parse_args(argv)
    if args.once:
        JOBS[args.once]()
        return
    sched = build_scheduler()
    log.info("ops scheduler started: %s", ", ".join(j.id for j in sched.get_jobs()))
    sched.start()


if __name__ == "__main__":
    main()
