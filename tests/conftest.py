"""Set up a safe environment for all tests.

Fills every required .env variable with stub values and points DATABASE_PATH
at a tmp file, so importing config/db/agent never touches real credentials or
the production SQLite file. No real Slack / LetsFG / Claude call is
made in any test.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="traveldesk-test-"))

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("CLAUDE_HAIKU_MODEL", "claude-haiku-5-5")
os.environ.setdefault("CLAUDE_SONNET_MODEL", "claude-sonnet-5-5")
os.environ.setdefault("LETSFG_API_KEY", "letsfg-test")
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("DATABASE_PATH", str(_TMP / "traveldesk.db"))
# Throwaway key so tests never touch the real encrypted store.
os.environ["TRAVELDESK_DATA_KEY"] = "pR0m9Hn8m5d1G3Wv9h0V4Zr1C3x2s7QnJ6bK8yT5uE0="

_travellers = ROOT / "travellers.json"
_backup = None
if _travellers.exists():
    _backup = _TMP / "travellers.json.bak"
    shutil.copy(_travellers, _backup)
_travellers.write_text(
    json.dumps(
        {
            "UEMP1": {
                "given_name": "Test",
                "family_name": "User",
                "email": "test@example.com",
                "phone": "+910000000000",
            }
        }
    )
)


def _restore_travellers():
    try:
        if _backup is not None:
            shutil.copy(_backup, _travellers)
        else:
            _travellers.unlink(missing_ok=True)
    except Exception:
        pass


atexit.register(_restore_travellers)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    _restore_travellers()
