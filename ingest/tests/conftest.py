"""Test fixtures.

The suite runs against a real PostGIS database (the ON CONFLICT / spatial /
timestamptz behaviour under test cannot be faithfully mocked). Point it at a DB
with ``TEST_DATABASE_URL`` or ``DATABASE_URL``; if none is reachable, the whole
suite skips rather than failing.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text

from mukoo_ingest.app import create_app
from mukoo_ingest.config import Config
from mukoo_ingest.db import make_engine

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_DIR = REPO_ROOT / "db"

DEFAULT_TEST_URL = "postgresql+psycopg2://mukoo:mukoo@localhost:5432/mukoo"


def _database_url() -> str:
    return (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or DEFAULT_TEST_URL
    )


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _database_url()

    engine = make_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any connection failure -> skip
        pytest.skip(f"No database reachable at {url}: {exc}")
    finally:
        engine.dispose()

    # Bring the schema up to head against the target database.
    subprocess.run(
        ["alembic", "upgrade", "head"],
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
