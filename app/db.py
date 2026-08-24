"""Database engine, sessions, and schema creation.

SQLite, on purpose. A reviewer should be able to clone this repo and run it
without asking anyone for a credential. At our scale (a few hundred cases) a
hosted Postgres would buy nothing and cost the reviewer five minutes and a
password they do not have.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Base

_settings = get_settings()

if _settings.database_url.startswith("sqlite"):
    db_path = _settings.database_url.split("sqlite:///", 1)[-1]
    if db_path and db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    _settings.database_url,
    connect_args={"check_same_thread": False}
    if _settings.database_url.startswith("sqlite")
    else {},
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """Write-ahead logging and enforced foreign keys on every SQLite connection."""
    if engine.dialect.name != "sqlite":
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# The ledger is append-only at the database level, not merely by convention.
# Anyone who reaches for an UPDATE — including future me, in a hurry — gets an
# error instead of a silently rewritten audit trail.
_APPEND_ONLY_TRIGGERS = (
    """
    CREATE TRIGGER IF NOT EXISTS ledger_no_update
    BEFORE UPDATE ON ledger
    BEGIN
        SELECT RAISE(ABORT, 'ledger is append-only: UPDATE is not permitted');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS ledger_no_delete
    BEFORE DELETE ON ledger
    BEGIN
        SELECT RAISE(ABORT, 'ledger is append-only: DELETE is not permitted');
    END;
    """,
)


def init_db() -> None:
    """Create tables and install the append-only guarantees. Safe to re-run."""
    Base.metadata.create_all(engine)
    if engine.dialect.name == "sqlite":
        with engine.begin() as conn:
            for trigger_sql in _APPEND_ONLY_TRIGGERS:
                conn.execute(text(trigger_sql))


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
