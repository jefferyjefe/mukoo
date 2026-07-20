"""Database engine construction for the model package.

The model only ever reads (``SELECT`` from ``measurements``); the ingestion API
owns all writes. This is deliberately a small copy of ``mukoo_ingest.db`` rather
than an import, so the modelling pipeline has no dependency on the API package.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


def make_engine(database_url: str) -> Engine:
    """Build a read-only-usage SQLAlchemy engine.

    ``pool_pre_ping`` keeps connections healthy across DB restarts; a modelling
    run may sit idle for a while between the query and later work.
    """
    return create_engine(database_url, future=True, pool_pre_ping=True)
