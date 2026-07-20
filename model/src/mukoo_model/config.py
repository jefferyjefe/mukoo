"""Runtime configuration for the coverage model, sourced from the environment.

Mirrors ``mukoo_ingest.config`` so both packages read the same ``DATABASE_URL``,
but the model owns its own copy: it is a separate installable that must run and
scale independently of the API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Local dev default; overridden in every real deployment via DATABASE_URL. Kept
# byte-for-byte identical to the ingest package's default on purpose.
DEFAULT_DATABASE_URL = "postgresql+psycopg2://mukoo:mukoo@localhost:5432/mukoo"

# Where GeoTIFF surfaces and the CV report are written. The task asks for ~/mukoo.
DEFAULT_OUTPUT_DIR = Path.home() / "mukoo"

# Grid resolution for the predicted surface, in metres. The survey area is only
# ~14 x 10 km, so 150 m cells give a ~93 x 66 raster: fine enough to see
# structure, coarse enough that kriging a few thousand cells stays quick.
DEFAULT_CELL_METRES = 150.0

# Variogram family for ordinary kriging. Exponential is the default because, on
# the current RSRP survey, 10-fold CV picks it over spherical/gaussian/linear
# (lower RMSE, higher R^2, still well-calibrated) — RSRP decorrelates gradually
# with a long tail, which the exponential's slow approach to the sill captures.
# Override with --variogram-model to re-check as more data lands.
DEFAULT_VARIOGRAM_MODEL = "exponential"

# Number of empirical-variogram lag bins. pykrige defaults to 6, which is coarse
# for ~1.4k points; 12 resolves the short-range structure that matters here.
DEFAULT_NLAGS = 12

# k for k-fold cross-validation, and the RNG seed for the fold assignment.
DEFAULT_CV_FOLDS = 10
DEFAULT_CV_SEED = 0


@dataclass(frozen=True)
class Config:
    database_url: str = DEFAULT_DATABASE_URL
    output_dir: Path = field(default_factory=lambda: DEFAULT_OUTPUT_DIR)
    cell_metres: float = DEFAULT_CELL_METRES
    variogram_model: str = DEFAULT_VARIOGRAM_MODEL
    nlags: int = DEFAULT_NLAGS
    cv_folds: int = DEFAULT_CV_FOLDS
    cv_seed: int = DEFAULT_CV_SEED

    @classmethod
    def from_env(cls) -> "Config":
        out = os.environ.get("MUKOO_MODEL_OUTPUT_DIR")
        return cls(
            database_url=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
            output_dir=Path(out) if out else DEFAULT_OUTPUT_DIR,
        )
