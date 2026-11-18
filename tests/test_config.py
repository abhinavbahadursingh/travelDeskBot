"""Project root resolution (TRAVELDESK_HOME, else the current folder)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parent.parent / "src")
ENV = {
    "SLACK_BOT_TOKEN": "xoxb-t",
    "SLACK_APP_TOKEN": "xapp-t",
    "ANTHROPIC_API_KEY": "sk-ant-t",
    "LETSFG_API_KEY": "k",
    "PYTHONPATH": SRC,
    "PATH": os.environ.get("PATH", ""),
}
PRINT = "from traveldesk import config; print(config.BASE); print(config.DATABASE_PATH)"


def _home(tmp_path: Path) -> Path:
    (tmp_path / "travellers.json").write_text(json.dumps({"U1": {"given_name": "A"}}))
    return tmp_path.resolve()


def _run(cwd, extra=None):
    out = subprocess.run(
        [sys.executable, "-c", PRINT],
        cwd=cwd,
        env={**ENV, **(extra or {})},
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()


def test_root_defaults_to_current_folder(tmp_path):
    home = _home(tmp_path)
    base, db_path = _run(home)
    assert base == str(home) and db_path == str(home / "traveldesk.db")


def test_traveldesk_home_wins_and_relative_db_path_is_under_it(tmp_path):
    (tmp_path / "home").mkdir()
    home = _home(tmp_path / "home")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    base, db_path = _run(elsewhere, {"TRAVELDESK_HOME": str(home), "DATABASE_PATH": "data/td.db"})
    assert base == str(home) and db_path == str(home / "data" / "td.db")
