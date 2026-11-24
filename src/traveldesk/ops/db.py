"""Engine and session handling for the ops tables."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from traveldesk.ops.settings import get_settings

_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None


def make_engine(url: str, **kw) -> Engine:
    engine = create_engine(url, **kw)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # the legacy bot writes the same file concurrently
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys = ON")
            cur.execute("PRAGMA busy_timeout = 5000")
            cur.execute("PRAGMA journal_mode = WAL")
            cur.close()

    return engine


def configure(url: str | None = None) -> Engine:
    """(Re)bind the module to a database. Tests call this with a temp URL."""
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = make_engine(url or get_settings().database_url)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def engine() -> Engine:
    return _engine or configure()


@contextmanager
def session_scope() -> Iterator[Session]:
    """One unit of work: commit on success, roll back on any error."""
    if _Session is None:
        configure()
    assert _Session is not None
    s = _Session()
    try:
        yield s
        s.commit()
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()
