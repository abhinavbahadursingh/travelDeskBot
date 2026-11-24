"""Autonomous ops layer: ledger-grade booking records, watchdog, digest, group booking, simulation.

Lives beside the original bot. Its tables are prefixed `ops_` so the legacy
`bookings` table (read by the dashboard UI) is never touched. Importing this
package does not import `traveldesk.config`, so it works without Slack /
Anthropic / LetsFG secrets (tests, CLI, migrations).
"""
