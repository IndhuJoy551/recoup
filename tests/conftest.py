"""Point every test at a throwaway database, before app modules are imported."""

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="recoup-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"

import pytest  # noqa: E402

from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture
def session():
    Base.metadata.drop_all(engine)
    init_db()
    with SessionLocal() as s:
        yield s
