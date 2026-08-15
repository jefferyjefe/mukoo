"""Test fixtures.

The suite runs against a real PostGIS database (the ON CONFLICT / spatial /
timestamptz behaviour under test cannot be faithfully mocked). Point it at a DB
with ``TEST_DATABASE_URL`` or ``DATABASE_URL``; if none is reachable, the whole
suite skips rather than failing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from mukoo_ingest.app import create_app
from mukoo_ingest.config import Config
from mukoo_ingest.db import make_engine

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_DIR = REPO_ROOT / "db"

DEFAULT_TEST_URL = "postgresql+psycopg2://mukoo:mukoo@localhost:5432/mukoo_test"


def _database_url() -> str:
    return (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or DEFAULT_TEST_URL
    )


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _database_url()

    # HARD GUARD: this suite TRUNCATEs the measurements table around every
    # test. Running it against a database whose name doesn't end in "_test"
    # (e.g. because DATABASE_URL points at dev and TEST_DATABASE_URL is unset)
    # destroys real field data. Refuse, loudly, rather than trust the caller.
    db_name = url.rsplit("/", 1)[-1].split("?")[0]
    if not db_name.endswith("_test"):
        pytest.skip(
            f"Refusing to run destructive DB tests against {db_name!r} — the "
            "suite truncates tables. Set TEST_DATABASE_URL to a *_test database."
        )

    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any connection failure -> skip
        pytest.skip(f"No database reachable at {url}: {exc}")
    finally:
        engine.dispose()

    # Bring the schema up to head against the target database. Invoked through
    # this interpreter rather than a bare "alembic" on PATH: pytest is often run
    # as ".venv/bin/python -m pytest" without the venv activated, and then the
    # bare name resolves to some other environment's alembic or to nothing at
    # all — 23 collection errors that look like a database problem and are not.
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(DB_DIR),
        env={**os.environ, "DATABASE_URL": url},
        check=True,
    )
    return url


@pytest.fixture()
def app(database_url):
    return create_app(Config(database_url=database_url))


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def clean_db(app):
    """Empty the measurements table around each test for isolation."""
    engine = app.config["ENGINE"]
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE measurements RESTART IDENTITY"))
    yield
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE measurements RESTART IDENTITY"))
