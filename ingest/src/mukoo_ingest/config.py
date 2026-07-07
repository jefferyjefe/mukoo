"""Runtime configuration, sourced from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass

# Local dev default; overridden in every real deployment via DATABASE_URL.
DEFAULT_DATABASE_URL = "postgresql+psycopg2://mukoo:mukoo@localhost:5432/mukoo"

# Upper bound on samples per request. A drive session batch is typically a few
# hundred points; this guards against a runaway client sending an enormous body.
DEFAULT_MAX_BATCH = 5000


@dataclass(frozen=True)
class Config:
    database_url: str = DEFAULT_DATABASE_URL
    max_batch: int = DEFAULT_MAX_BATCH

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            database_url=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
            max_batch=int(os.environ.get("MUKOO_MAX_BATCH", DEFAULT_MAX_BATCH)),
        )
