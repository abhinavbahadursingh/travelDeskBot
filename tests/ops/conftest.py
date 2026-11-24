"""Each ops test gets a fresh SQLite file built by the real Alembic migration."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from traveldesk.ops import db as ops_db

ROOT = Path(__file__).resolve().parents[2]


def alembic_cfg(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def db_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'ops.db'}"


@pytest.fixture
def ops_session(db_url):
    """Migrated DB bound to traveldesk.ops.db; yields session_scope."""
    command.upgrade(alembic_cfg(db_url), "head")
    ops_db.configure(db_url)
    yield ops_db.session_scope
    ops_db.engine().dispose()
