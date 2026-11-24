# TravelDesk Slack bot

`/traveldesk Delhi to Mumbai Tuesday morning` → the bot finds real fares on
LetsFG, picks one by the company rule, asks **you** (the requester) to
confirm in Slack, and sends the PNR back once the ticket is issued.

```
Slack /traveldesk ──► app.py ──► Claude (Haiku/Sonnet) ──► LetsFG (MCP)
         ▲                │                                        │
         │                ▼                                        │
         │    Confirm / Cancel buttons sent to the requester       │
         │                │                                        │
         └── PNR / payment link / status (Slack DM) ◄──────────────┘
```

**Flight booking only.** No hotels, no trains, no manager-approval workflow.
The requesting employee is the booking authority — they see the picked
offer in DM, click **Confirm & book**, and the booking runs under their
Slack identity.

## Files

| File | What it does |
|---|---|
| `app.py` | Slack bot: slash command, employee confirmation card, polling |
| `agent.py` | Picks the provider based on `AI_PROVIDER` |
| `claude_agent.py` | Claude with intelligent **Haiku ↔ Sonnet routing** + usage tracking |
| `groq_agent.py` | Groq fallback: real tool-calling against LetsFG MCP |
| `mcp_client.py` | Minimal MCP Streamable-HTTP client (used by the Groq path) |
| `config.py` | Reads `.env` and `travellers.json` |
| `db.py` | SQLite log: `bookings` + `ai_usage` (tokens, cost) |
| `tests/` | `pytest` unit tests (no network, no real bookings) |
| `travellers.example.json` | Slack user → traveller name/email/phone |
| `com.traveldesk.bot.plist` | Keeps the bot running on the Mac mini |

## 1. Create the Slack app

1. <https://api.slack.com/apps> → **Create New App** → **From scratch**.
2. **Socket Mode** → turn on → create an app-level token with scope
   `connections:write` → `SLACK_APP_TOKEN` (`xapp-…`).
3. **Slash Commands** → create `/traveldesk`.
4. **Interactivity & Shortcuts** → turn **on** (required for the confirmation
   buttons). No Request URL needed with Socket Mode.
5. **OAuth & Permissions** → Bot Token Scopes: `commands`, `chat:write`,
   `im:write`, `users:read`.
6. **App Home** → enable the **Messages tab**.
7. **Install to Workspace** → copy **Bot User OAuth Token** (`xoxb-…`) into
   `SLACK_BOT_TOKEN`.

No manager role, admin dashboard or approver list is required.

## 2. AI provider — Claude with intelligent routing

Claude is the primary provider (`AI_PROVIDER=claude` in `.env`). Each API
call is routed to the smallest capable model:

| Task | Model | Why |
|---|---|---|
| Intent / complexity classification | **Haiku** | fast binary decision |
| Airport lookup, data extraction, formatting | **Haiku** | small, cheap |
| Booking-status polling | **Haiku** | one tool call, no reasoning |
| Picking a flight from a ranked list | **Sonnet** | rule-following |
| Multi-city, ambiguous, flexible search | **Sonnet** | real reasoning |
| Any `book_flight` call | **Sonnet** | payment-affecting, never delegated |

Routing is driven by `claude_agent._classify_complexity` (a short Haiku
call) with cheap regex fast-paths for obviously-simple / obviously-complex
requests, so the classifier itself is rarely the bottleneck.

**Fallback.** `CLAUDE_FALLBACK=true` (default) means Sonnet → Haiku (or
vice-versa) on a rate limit / overload / transient 5xx, once per task.

**Usage tracking.** Every call records model, task kind, input / output
tokens, cache tokens and an estimated USD cost in the `ai_usage` table.
`db.usage_totals()` returns the aggregate.

**Groq fallback.** `AI_PROVIDER=groq` switches to the Llama-on-Groq path.
For safety, Groq is never given `book_flight` or `get_flight_booking` —
those are called deterministically from Python after the employee confirms.
Set a model listed as *tool-calling capable* at
<https://console.groq.com/docs/models>.

## 3. LetsFG token

Email **adam@letsfg.co** for an MCP server URL + token for your own app
(MCP connector / HTTP). Ask whether their MCP supports OAuth 2.1 Dynamic
Client Registration — the current code uses the static Bearer token because
that is what LetsFG exposes today.

Put the URL and token in `LETSFG_MCP_URL` / `LETSFG_TOKEN`.

The current code calls these LetsFG tools (verified names from the MCP
server — adjust `claude_agent.SEARCH_TOOLS` / `BOOK_TOOLS` and
`groq_agent._TOOLS` if LetsFG renames one):

- `find_airports`, `search_flights`, `get_flight_results`,
  `get_flight_details` — search
- `book_flight`, `get_flight_booking`, `stop_flight_booking` — booking

Nothing else is enabled — no hotels, no payment-method edits, no profile
changes.

## 4. Install & run

```bash
cd ~/traveldesk-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill in the values
cp travellers.example.json travellers.json   # add your team

# Keep DRY_RUN=true for the first run.
python app.py
```

In Slack: `/traveldesk help`, then `/traveldesk Delhi to Mumbai next Tuesday morning`.

**Switching providers**
```bash
# Edit .env → AI_PROVIDER=claude (or groq), save, then:
launchctl kickstart -k gui/$(id -u)/com.traveldesk.bot    # or re-run python app.py
```

**Inspect logs and costs**
```bash
tail -f ~/traveldesk-bot/bot.log

# Latest bookings
sqlite3 ~/traveldesk-bot/traveldesk.db \
  'SELECT id, status, provider, price, pnr FROM bookings ORDER BY id DESC LIMIT 20;'

# Model usage and estimated cost
sqlite3 ~/traveldesk-bot/traveldesk.db \
  'SELECT model, task_kind, SUM(input_tokens) AS inp, SUM(output_tokens) AS outp,
          ROUND(SUM(est_cost_usd), 4) AS usd FROM ai_usage GROUP BY model, task_kind;'
```

**Run tests**
```bash
pytest -q
```
Tests never hit Slack, Groq, Claude or LetsFG — all external calls are
stubbed, and `DRY_RUN=true` is forced in the test environment.

## 5. Keep it running 24/7

```bash
# edit the paths in the plist first (username / folder)
cp com.traveldesk.bot.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.traveldesk.bot.plist
tail -f ~/traveldesk-bot/bot.log
```

Stop: `launchctl unload ~/Library/LaunchAgents/com.traveldesk.bot.plist`

## How the flow behaves

1. Employee runs `/traveldesk …`.
2. The provider searches LetsFG and picks one offer by `BOOKING_RULE` /
   `MAX_PRICE`. (Claude: Haiku for extraction, Sonnet for the pick.)
3. A Block Kit card goes to the **requesting employee** in their DM with
   **Confirm & book** / **Cancel** buttons.
4. Only that employee can click — any other user gets an ephemeral
   "not authorized" message and the DB row is unchanged.
5. A single atomic SQL update wins the click. Duplicate clicks or Slack
   event retries see "already confirmed/cancelled" and change nothing.
6. On confirm, the booking tool is called. If LetsFG returns an approval
   URL it is posted to the employee. **The bot never claims payment
   succeeded until LetsFG returns a `completed` status with a PNR.**
7. Status polling every `POLL_SECONDS` for up to `POLL_MAX_MINUTES`.
   Final states handled: `completed`, `failed`, `approval_expired`,
   `not_started`, `needs_attention`, `cancelled`, timeout.
8. On process restart, any row with a `booking_ref` and non-terminal
   status is automatically picked back up (`_resume_unfinished_bookings`).

## Safety switches

- `DRY_RUN=true` — search and pick only; `book_flight` is never called.
- Near-duplicate request from the same user within 5 minutes → blocked.
- Only the requester can confirm their own booking.
- Duplicate click / Slack retry → ignored atomically (DB `claim_decision`).
- Groq model is never given `book_flight` or `get_flight_booking`.
- Payment-affecting calls always run on Sonnet, never Haiku.
- API keys and the LetsFG token are redacted from debug logs; secrets
  stay out of Git — replace every placeholder in `.env.example`.
- Signed Slack requests: Socket Mode authenticates every event over the
  app-level WebSocket, so no HTTP signature verification is needed here.

## Required `.env` variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | yes | — | `xoxb-…` |
| `SLACK_APP_TOKEN` | yes | — | `xapp-…`, Socket Mode |
| `AI_PROVIDER` | no | `claude` | `claude` or `groq` |
| `ANTHROPIC_API_KEY` | yes if Claude | — | |
| `CLAUDE_HAIKU_MODEL` | no | `claude-haiku-5-5` | |
| `CLAUDE_SONNET_MODEL` | no | `claude-sonnet-5-5` | |
| `CLAUDE_FALLBACK` | no | `true` | Haiku ↔ Sonnet on transient errors |
| `MCP_BETA` | no | `mcp-client-2025-11-20` | |
| `GROQ_API_KEY` | yes if Groq | — | |
| `GROQ_MODEL` | no | `llama-3.3-70b-versatile` | Must support tool calls |
| `LETSFG_MCP_URL` | yes | — | From adam@letsfg.co |
| `LETSFG_TOKEN` | yes | — | From adam@letsfg.co |
| `CURRENCY` | no | `INR` | |
| `BOOKING_RULE` | no | cheapest nonstop | Natural language |
| `MAX_PRICE` | no | — | Hard per-person cap |
| `DRY_RUN` | no | `true` | **Never auto-disable** |
| `POLL_SECONDS` | no | `45` | |
| `POLL_MAX_MINUTES` | no | `30` | |
| `DATABASE_PATH` | no | `traveldesk.db` | |

## Remaining blockers for real bookings

- A LetsFG MCP URL + token from adam@letsfg.co with `book_flight` enabled
  for your account.
- One-time LetsFG account setup (traveller details + saved card) on
  letsfg.co — payment ultimately clears there.
- Confirm whether your LetsFG account charges automatically or returns an
  `approve_url` that the traveller still has to tap. Both are handled.
- Verify the live LetsFG tool names once with:
  ```
  python -c "import mcp_client, config; \
    print([t['name'] for t in mcp_client.MCPClient(config.LETSFG_MCP_URL, config.LETSFG_TOKEN).list_tools()])"
  ```
  Adjust `claude_agent.SEARCH_TOOLS` / `BOOK_TOOLS` and
  `groq_agent._TOOLS` if names drifted, before flipping `DRY_RUN=false`.
- For Groq: pick a model currently listed as tool-calling capable at
  <https://console.groq.com/docs/models>.

## Features verified with mocks vs live

**Verified in the test suite (all mocked):**

- Haiku/Sonnet routing selection and fallback
- `/traveldesk` slash command handling
- Employee self-confirmation flow (Confirm / Cancel)
- Rejection of confirmation attempts from anyone but the requester
- Duplicate-click / event-retry protection via atomic DB claim
- DRY_RUN never books
- Groq tool allowlist refuses `book_flight` / `get_flight_booking`
- Startup resume of in-flight bookings
- AI usage recording and cost estimation

**Not yet verified live (requires LetsFG credentials):**

- Real LetsFG `search_flights` / `book_flight` round trip
- Real payment via `approve_url` or auto-charge
- Real PNR delivery

## Safe-test checklist

1. `AI_PROVIDER=claude`, `DRY_RUN=true`, `pytest -q` → all green.
2. `AI_PROVIDER=groq`, `DRY_RUN=true`, `pytest -q` → all green.
3. `/traveldesk DEL to BOM next Tuesday morning` → employee gets a
   Block Kit card in DM → click Confirm → dry-run notification.
4. Non-requester clicks the Confirm button → ephemeral
   "not authorized"; DB unchanged.
5. Click Confirm twice in a row → second click says "already
   confirmed"; DB row has one `confirmed` entry.
6. Query `ai_usage` — see Haiku vs Sonnet rows with non-zero token counts
   and a small `est_cost_usd`.
7. Only then, in a quiet window, flip `DRY_RUN=false`. The first live
   booking should be a low-stakes one you are ready to let go through and
   watch end to end.
# travelDeskBot

## Ops layer (`traveldesk.ops`)

Ledger-grade records beside the original bot, plus (in later phases) the fare/schedule
watchdog, the end-of-day leakage digest, group booking and policy simulation.

**Separate tables, same database.** Everything is in `ops_*` tables
(`ops_travelers`, `ops_requests`, `ops_bookings`, `ops_segments_snapshot`, `ops_ledger`,
`ops_events`). The bot's own `bookings` / `ai_usage` tables, and the dashboard that reads
them, are not changed. Alembic only sees `ops_*` tables (its version table is
`ops_alembic_version`), so a migration can never alter or drop the bot's tables.

**Rules**
- Money is integer paise (`fare_paise`, `amount_paise`). Ledger amounts are positive; `type`
  (`debit | refund | fee | credit_note`) gives the direction.
- Times are stored in UTC (timezone-aware) and shown in Asia/Kolkata. LetsFG gives airport-local
  times with no offset: Indian airports are read as IST; other airports are left empty for now
  instead of being stored wrong.
- Every booking is written as requests → bookings → ledger debit in **one transaction**, and
  every write is idempotent (unique supplier ref, ledger idempotency key, event dedupe key).
- **The debit is written when the supplier accepts the booking, not when the ticket arrives.**
  One debit per supplier booking (key `debit:{supplier}:{supplier_ref}`), with a status:

  | Supplier answer | Booking status | Debit |
  |---|---|---|
  | Accepted (charged) | `pending` | `pending`, at the booked fare |
  | Pay link ("payment needed", nothing charged) | `awaiting_payment` (+ link, expiry) | none |
  | Pay link paid | `pending` | `pending` |
  | Ticket issued | `ticketed` | the same row → `confirmed` (charged amount); created if missing |
  | Refused / cancelled / pay link expired before a ticket | `failed` / `cancelled` | `void` (if any) |

  The group poller, the bot's own single-booking flow and the legacy mirror all write through
  the same key, so restarts and re-runs never add a second debit. Bot single bookings are
  tracked from the moment LetsFG accepts them (guarded calls in `slack/app.py`).
- `ops_travelers.passport_no` stores only the last 4 digits. The full number stays in the
  encrypted `traveller_secure` store.
- Only `traveldesk.ops.supplier` talks to suppliers. Every call is logged with a `trace_id`.
  `SUPPLIER=letsfg` is live; `SUPPLIER=tbo` raises `NotImplementedError` (TODOs for hold PNR,
  re-quote, cancel/refund, wallet balance and invoices/GST are in `ops/supplier/tbo.py`).

**How tickets get in.** When the bot sees LetsFG `completed` with a PNR, it calls
`traveldesk.ops.legacy.mirror_legacy_id`, which writes the request, booking and debit. A
failure there is logged and never blocks the ticket message.

**Environment**

| Variable | Default | Meaning |
|---|---|---|
| `OPS_DATABASE_URL` | empty → `sqlite:///<project root>/<DATABASE_PATH>` | SQLAlchemy URL for `ops_*` tables |
| `SUPPLIER` | `letsfg` | `letsfg` or `tbo` (stub) |
| `TICKET_TIMEOUT` | `2h` | A pending (charged) debit with no ticket after this → digest "Charged, no ticket"; a pending group traveller → "needs action". `90m`, `2h`, `1h30m` or minutes |
| `AWAITING_PAYMENT_ALERT` | `30m` | An unpaid pay link older than this → digest "Awaiting payment" |
| `GROUP_OFFER_MODE` | `per_traveller` | `per_traveller`: re-search the same flight and book a fresh offer for each traveller. `shared`: book the one picked offer for everyone (only if LetsFG allows offer reuse) |
| `PRICE_TOLERANCE_PCT` | `5` | A re-priced fare may rise this much (and stay ≤ `MAX_PRICE`) and still book without asking |
| `DISPLAY_TZ` | `Asia/Kolkata` | Time zone for Slack text |

**Set up / upgrade the tables** (from the project root, bot stopped or running — WAL mode):

```bash
alembic upgrade head                      # creates/updates ops_* tables
python -m traveldesk.ops.legacy backfill  # mirror bookings already ticketed by the bot
alembic downgrade base                    # removes ops_* tables only
```

**Tests:** `pytest tests/ops` — fresh SQLite per test, built by the real migration;
a `FakeSupplier` (tests/ops/fakes.py) stands in for LetsFG. No network calls.

### Scheduler

Ops jobs run in their own process (the bot doesn't need restarting for them):

```bash
python -m traveldesk.ops.scheduler                 # run forever (first watchdog pass at start-up)
python -m traveldesk.ops.scheduler --once watchdog # one pass, then exit
python -m traveldesk.ops.scheduler --once digest   # post today's digest now (once per IST day)
python -m traveldesk.ops.scheduler --once group_poll  # check pending group tickets now
```

Keep it running with launchd, like the bot (logs to `ops.log`):

```bash
cp deploy/launchd/com.traveldesk.ops.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.traveldesk.ops.plist
```

Jobs never overlap (`max_instances=1`), missed runs collapse into one, and every job is
idempotent, so restarts are safe. Without `SLACK_BOT_TOKEN` alerts are only logged.

### Slack setup for ops

- Scopes: the bot's existing `chat:write` and `im:write` are enough. Add `chat:write.public`
  only if you'd rather not invite the bot to the ops channel.
- Create `#travel-ops`, `/invite` the bot, set `OPS_SLACK_CHANNEL`.
- Interactivity must be on (it already is for Confirm). The scheduler posts the messages; the
  bot's Socket Mode app receives the **[Acknowledge]** clicks, so restart the bot once after
  upgrading.

### Fare-drop & schedule-drift watchdog (every `WATCHDOG_INTERVAL_HOURS`, default 3)

Checks held/ticketed bookings that haven't departed and weren't checked in the last 150 min:

| Finding | Action |
|---|---|
| Held, fare down ≥ ₹500 | Re-quote the hold at the lower fare (`fare_requoted`); if the supplier can't, ask ops to do it (`fare_drop`) |
| Ticketed + refundable, fare down ≥ ₹500 | Tell ops only (`fare_drop`) |
| Ticketed, not refundable / unknown | Nothing |
| Departure or arrival moved ≥ 20 min | DM the traveller + post to ops (`schedule_drift`) |

Every check stores an `ops_segments_snapshot`. Messages look like *"Optimization caught: fare
dropped ₹1,000"* or *"DEL-BOM departure moved 35m earlier — check cab/meetings"*, with an
**[Acknowledge]** button (first click is recorded as an `acknowledged` event).

The same change never alerts twice (dedupe key = booking + new fare/times). If Slack is down,
the alert is kept and the unsent targets are retried on the next runs (for 48 h).

**LetsFG limits.** LetsFG bookings are never `held`, so re-quoting only happens with TBO.
LetsFG's booking status has no flight times, so a schedule change is found by re-searching
the trip: same flight number at a new time = moved. If the flight number isn't known (older
picks), a re-timed flight just looks "not found" (`flight_not_found` event, no alert).

### End-of-day leakage digest (daily at `DIGEST_TIME_IST`, default 19:00 IST)

One Block Kit post to `OPS_SLACK_CHANNEL`. Each booking lands in at most one bucket:

| Bucket | Listed when |
|---|---|
| Charged, no ticket | a `pending` debit older than `TICKET_TIMEOUT` and the booking has no PNR — buttons [Mark resolved], [Open booking] |
| Awaiting payment | a pay link unpaid for more than `AWAITING_PAYMENT_ALERT` (shows its expiry) — buttons [Open pay link], [Mark resolved] |
| Ticketed, no ledger debit or invoice | status `ticketed` and no debit, or the debit has no invoice number |
| Refund pending > 7 days | `refund_pending` for longer than `REFUND_PENDING_MAX_DAYS` (from `status_changed_at`) |
| GST missing/mismatched or invoice ≠ debit | invoice GSTIN missing, invalid (format + check digit), or not `COMPANY_GSTIN`; or invoice amount ≠ debit |

Buckets appear in that order and are numbered as shown. Each item is one line (route, date, PNR, traveller, fare — problem) with **[Mark resolved]**
(hides it from later digests; event `leak_resolved`) and **[Open booking]** (opens the dashboard
at `/?booking=<id>`; only for bookings the bot made, and only if `DASHBOARD_URL` is set).
Empty buckets are skipped; nothing at all → "All clear". Posted once per IST day; if Slack
fails, the next run posts it. Long buckets are cut to fit Slack's 50-block limit with a
"+N more" line.

```bash
python -m traveldesk.ops.digest --list    # today's items in the terminal (#id = ops booking id)
python -m traveldesk.ops.invoices attach <id> --invoice-no INV-123 --gstin 27AAPFU0939F1ZV --amount 10500.50
```

### GST

Rates live in `src/traveldesk/ops/gst_rates.json`, versioned by `effective_from` (add a new version for a
rate change). `itc_eligible` is `false` everywhere until your CA confirms otherwise. Each ticketed booking
gets a GST estimate (flight, cabin from the offer, taxable value backed out of the fare); the supplier
invoice replaces it. Same state as the company (`COMPANY_STATE_CODE`, else from `COMPANY_GSTIN`) →
CGST + SGST, otherwise IGST. Invoice GST more than ₹1 from the computed figure is flagged.

```bash
alembic upgrade head   # once: adds the GST columns (migration 0006)
python -m traveldesk.ops.invoices attach <id> --invoice-no INV-123 --supplier-gstin 29AAGCB7383J1Z4 \
    --invoice-date 2026-11-20 --taxable 10000 --igst 500 --invoice-url https://…   # hotel: --category hotel --per-night 6800
python -m traveldesk.ops.invoices itc-claimed <id>
```

In Slack (travel admins): `/traveldesk gst [2026-11 | Nov | FY2026-27]` → summary + link to the dashboard's `/gst` page
(filters, per-booking breakdown, GSTR-2B-style CSV export).

LetsFG sends invoices by email, not through its API, so every LetsFG ticket starts in bucket 1
until its invoice is attached (or it's marked resolved). With TBO, invoice number and GSTIN
will be filled automatically (TODO in `ops/supplier/tbo.py`).

### Group booking (split roster)

```
/traveldesk DEL to Dubai 12 Dec morning for @asha @ravi @meera
```

- **Who:** only travel admins (`TRAVEL_ADMIN_SLACK_IDS`) can book for others. Anyone else gets
  "ask a travel admin". Requests without @mentions keep the normal single-person flow.
- **Slack setting:** turn on **Escape channels, users, and links** for `/traveldesk`
  (Slash Commands → edit). Without it Slack sends `@asha` as plain text and the bot asks
  you to fix the setting. DMs to the bot always carry real mentions.
- **Checks per traveller:** in `travellers.json` · Slack name shares the profile's given or
  family name (catches a wrong Slack ID) · booking details complete — international trips need a
  passport valid ≥ 6 months after the last flight · fare ≤ `MAX_PRICE`. Reasons name the field,
  never its value.
- **One confirm:** the requester gets one card, "7 ready / 3 need action", and one
  **[Book 7 travellers · ₹…]** button. Each ready traveller is booked as a separate one-passenger
  booking, so one refusal (sold out, fare expired) only affects that person.
- **Fresh offer per traveller** (`GROUP_OFFER_MODE=per_traveller`, default): before each
  traveller, the bot re-searches the same flight — same carrier, flight number(s) and departure
  time — and books a fresh offer. Gone → "needs action: flight no longer available". A changed
  fare follows the price rules below. `shared` books the one picked offer for everyone.
- **Price rules** (fresh offers, every [Fix & retry], every [Book at ₹X]):

  | Fresh fare vs the fare shown for that traveller | Result |
  |---|---|
  | same or lower | book |
  | up ≤ `PRICE_TOLERANCE_PCT` and ≤ `MAX_PRICE` | book; the card shows "₹25,600 (was ₹25,000)" |
  | up more, or over `MAX_PRICE` | not booked; the card shows "₹25,000 → ₹27,500 (+10%)" with **[Book at ₹27,500]** |
  | not available (retry) | "needs action: fare expired — send a new request" |

  [Book at ₹X] re-checks once more and books only at ₹X or lower (the admin's click is the
  approval, even over `MAX_PRICE`); if the price moved again, the card shows the new one and a
  new button. Nothing is ever booked at a price that isn't on the card.
- **Result:** the card becomes "7 booked / 3 need action". Each exception has its reason and
  **[Fix & retry]**, which re-loads that traveller's profile, re-prices (rules above) and books only
  them (with a fresh booking key, so the supplier doesn't replay the earlier refusal).
- **Tickets:** LetsFG tickets asynchronously. The bot polls until each PNR arrives (the
  scheduler's `group_poll` job every 5 min covers restarts), then records request → booking →
  ledger debit and DMs the traveller their PNR. Still not ticketed after `TICKET_TIMEOUT` →
  "needs action" (the pending debit stays and shows in the digest). A pay link that expires or is
  closed → "needs action", nothing charged.
- `DRY_RUN=true`: everything up to the booking call runs; the card says "would be booked".

All one flight: the bot picks it with the same picker and `BOOKING_RULE` as single bookings,
priced per person. Seat availability is checked by the booking call itself; LetsFG's API
doesn't expose seat counts.
