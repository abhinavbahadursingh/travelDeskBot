"""Web booking service for the travel desk /book page.

A separate process from the Slack bot (`python -m traveldesk.web`). It searches
LetsFG, offers three picks, and hands the chosen flight to Slack as the normal
confirmation card. From the traveller's Confirm click onwards the Slack bot
runs the booking exactly as it does for a request typed in Slack.

Modules:
  offers     — pure offer logic: ranking the picks, card text, the pick record
  searches   — background search jobs (route extraction + LetsFG search)
  slack_out  — the only place that talks to Slack (intro message + card)
  booking    — travellers and "send this flight to Slack"
  server     — the JSON HTTP API
"""
