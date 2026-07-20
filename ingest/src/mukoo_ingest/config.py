"""Runtime configuration, sourced from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Local dev default; overridden in every real deployment via DATABASE_URL.
DEFAULT_DATABASE_URL = "postgresql+psycopg2://mukoo:mukoo@localhost:5432/mukoo"

# Upper bound on samples per request. A drive session batch is typically a few
# hundred points; this guards against a runaway client sending an enormous body.
DEFAULT_MAX_BATCH = 5000

# GeoJSON of active-learning drive suggestions, written by the model package
# (`mukoo-suggest`). GET /v1/suggestions serves this file so the field logger can
# fetch and cache it. Same default location the model writes to (~/mukoo).
DEFAULT_SUGGESTIONS_PATH = str(Path.home() / "mukoo" / "rsrp_drive_suggestions.geojson")


@dataclass(frozen=True)
class Config:
    database_url: str = DEFAULT_DATABASE_URL
    max_batch: int = DEFAULT_MAX_BATCH
    suggestions_path: str = DEFAULT_SUGGESTIONS_PATH

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            database_url=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
            max_batch=int(os.environ.get("MUKOO_MAX_BATCH", DEFAULT_MAX_BATCH)),
            suggestions_path=os.environ.get(
                "MUKOO_SUGGESTIONS_PATH", DEFAULT_SUGGESTIONS_PATH
            ),
        )
