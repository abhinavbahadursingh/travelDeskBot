"""Background search jobs: free text → route → LetsFG offers.

The route extraction and airport-code check are the Slack bot's own steps
(agents.claude_agent), so a web search understands requests the same way.
Only Claude's single "pick by BOOKING_RULE" step is left out: on the web the
traveller chooses from the picks instead.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from traveldesk import config
from traveldesk.agents import claude_agent as agent
from traveldesk.web import offers as offer_logic

log = logging.getLogger("traveldesk.web.searches")


def find_offers(text: str) -> dict:
    """Same route handling as agent.pick (incl. the Sonnet retry on a wrong
    airport code) and the same MAX_PRICE filter, without choosing an offer.

    Returns {"status": "ok", "route", "search_id", "offers", "found"} or
    {"status": "need_info" | "no_match" | "failed", "message", ...}."""
    ex = agent._extract_route(text, config.CLAUDE_HAIKU_MODEL, None)
    if err := agent._route_error(ex):
        return err
    search = agent._search(ex)
    wrong = agent._wrong_airports(ex, search)
    if wrong:
        log.warning("web search: airport code mismatch %s; retrying with Sonnet", wrong)
        ex = agent._extract_route(text, config.CLAUDE_SONNET_MODEL, None, wrong=wrong)
        if err := agent._route_error(ex):
            return err
        search = agent._search(ex)
        wrong = agent._wrong_airports(ex, search)
    route = {k: ex[k] for k in ("origin", "destination", "date_from", "return_from", "passengers")}
    if wrong:
        return {"status": "need_info", "message": agent._mismatch_message(wrong), "route": route}
    if search["status"] != "ok":
        return {**search, "route": route}

    found = [o for o in search["offers"] if isinstance(o, dict) and o.get("id")]
    bookable = found
    if config.MAX_PRICE:
        bookable = [o for o in found if (p := offer_logic.price(o)) is not None and p <= config.MAX_PRICE]
    where = f"{route['origin']} → {route['destination']} on {route['date_from']}"
    if not found:
        return {"status": "no_match", "message": f"No flights found for {where}.", "route": route}
    if not bookable:
        cheapest = min((p for o in found if (p := offer_logic.price(o)) is not None), default=None)
        return {
            "status": "no_match",
            "message": f"All {len(found)} flights for {where} are above the trip budget "
            f"({config.MAX_PRICE:.0f} {config.CURRENCY})."
            + (f" Cheapest found: {cheapest:.0f} {search['currency']}." if cheapest else ""),
            "route": route,
        }
    return {
        "status": "ok",
        "route": route,
        "search_id": search["search_id"],
        "offers": bookable,
        "found": len(found),
    }


@dataclass
class Search:
    id: str
    user: str
    text: str
    started: float = field(default_factory=time.time)
    finished: float | None = None
    status: str = "running"  # running | ok | need_info | no_match | failed
    message: str = ""
    route: dict = field(default_factory=dict)
    search_id: str | None = None
    offers: list = field(default_factory=list)  # raw bookable offers, cheapest first
    found: int = 0

    def offer(self, offer_id: str) -> dict | None:
        return next((o for o in self.offers if str(o.get("id")) == str(offer_id)), None)

    def to_json(self) -> dict:
        """What the page sees: picks + every bookable flight as cards."""
        out = {
            "id": self.id,
            "status": self.status,
            "message": self.message,
            "text": self.text,
            "route": self.route,
            "started": self.started,
            "finished": self.finished,
            "max_price": config.MAX_PRICE or None,
            "currency": config.CURRENCY,
        }
        if self.status == "ok":
            picks = offer_logic.rank(self.offers)
            out |= {
                "found": self.found,
                "picks": [offer_logic.card(o, tag) for tag, o in picks],
                "all": [offer_logic.card(o) for o in self.offers],
            }
        return out


class SearchStore:
    """In-memory search jobs, run on a small thread pool and kept for `ttl` seconds.

    One process holds the jobs; run one web service instance (or swap this for a
    shared store) if the service is ever scaled out."""

    def __init__(
        self,
        runner: Callable[[str], dict] = find_offers,
        *,
        ttl: float = 30 * 60,
        max_jobs: int = 200,
        workers: int = 4,
    ):
        self._runner = runner
        self._ttl = ttl
        self._max = max_jobs
        self._jobs: OrderedDict[str, Search] = OrderedDict()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="web-search")

    def start(self, user: str, text: str) -> Search:
        job = Search(id=secrets.token_urlsafe(9), user=user, text=text)
        with self._lock:
            self._evict()
            self._jobs[job.id] = job
        self._pool.submit(self._run, job)
        return job

    def get(self, job_id: str) -> Search | None:
        with self._lock:
            self._evict()
            return self._jobs.get(job_id)

    def _run(self, job: Search) -> None:
        try:
            result = self._runner(job.text)
        except Exception as e:  # noqa: BLE001 — reported to the page
            log.exception("web search %s failed", job.id)
            result = {"status": "failed", "message": f"Search failed: {type(e).__name__}"}
        offers = sorted(result.get("offers") or [], key=lambda o: offer_logic.price(o) or 0.0)
        job.route = result.get("route") or {}
        job.search_id = result.get("search_id")
        job.offers = offers
        job.found = int(result.get("found") or len(offers))
        job.message = result.get("message") or ""
        job.finished = time.time()
        job.status = result.get("status") or "failed"  # last: readers poll on status

    def _evict(self) -> None:
        now = time.time()
        for jid in [j for j, s in self._jobs.items() if s.finished and now - s.finished > self._ttl]:
            del self._jobs[jid]
        while len(self._jobs) > self._max:
            self._jobs.popitem(last=False)
