"""Alembic environment for the ops_* tables (shares the DB file with the legacy bot)."""

from __future__ import annotations

from alembic import context
from sqlalchemy import pool

from traveldesk.ops.db import make_engine
from traveldesk.ops.models import VERSION_TABLE, Base, include_object
from traveldesk.ops.settings import get_settings

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    # `-x url=...` (tests) > sqlalchemy.url set programmatically > ops settings.
    return (
        context.get_x_argument(as_dictionary=True).get("url")
        or config.get_main_option("sqlalchemy.url")
        or get_settings().database_url
    )


def _configure(**kw) -> None:
    context.configure(
        target_metadata=target_metadata,
        include_object=include_object,
        version_table=VERSION_TABLE,
        render_as_batch=True,  # SQLite ALTER support
        compare_type=True,
        **kw,
    )


def run_migrations_offline() -> None:
    _configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
