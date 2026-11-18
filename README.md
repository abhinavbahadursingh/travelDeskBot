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
