"""Database engine construction."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


def make_engine(database_url: str) -> Engine:
    """Build a SQLAlchemy engine.

    ``pool_pre_ping`` keeps long-lived connections healthy across DB restarts,
    which matters for a service that mostly idles between drive-session uploads.
    """
    return create_engine(database_url, future=True, pool_pre_ping=True)
