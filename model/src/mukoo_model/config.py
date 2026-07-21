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

# Tile size for spatial block CV. ~2 km sits between the cell size and the
# variogram range, so held-out blocks are genuinely "away" from training data.
DEFAULT_CV_BLOCK_M = 2000.0

# Which kriging model the pipelines run: "ordinary", or "pathloss" (regression
# kriging with the log-distance tower prior). Settable via MUKOO_KRIGING so the
# auto-refresh agent and the suggester pick it up without CLI flags; use
# `mukoo-krige --compare` to decide which one your data supports.
DEFAULT_KRIGING_MODE = "ordinary"
KRIGING_MODES = ("ordinary", "pathloss")

# Optional RSRP floor (dBm) at which network_type='none' dead-zone rows join
# the interpolation. None (default) keeps dead zones out of the model, matching
# historical behaviour. Settable via MUKOO_NONE_FLOOR (e.g. -127): a dead zone
# then counts as "at most this weak" instead of being invisible — the phone
# couldn't hear the cell at all, so the true value is at or below any floor a
# phone can report.
DEFAULT_NONE_FLOOR: "float | None" = None

# Variogram anisotropy (scaling, angle-deg CCW from east) for ordinary kriging.
# (1.0, 0.0) = isotropic. Settable via MUKOO_ANISOTROPY as "SCALING:ANGLE"
# (e.g. "3:150") so a winner found by `mukoo-krige --compare` can be adopted by
# the auto-refresh agent and the suggester without CLI flags.
DEFAULT_ANISOTROPY: "tuple[float, float]" = (1.0, 0.0)


def parse_anisotropy(value: str) -> "tuple[float, float]":
    """Parse "SCALING:ANGLE" (e.g. "3:150") into (scaling, angle_deg)."""
    try:
        scaling_s, angle_s = value.split(":", 1)
        return (float(scaling_s), float(angle_s))
    except ValueError as exc:
        raise ValueError(
            f"anisotropy must look like SCALING:ANGLE (e.g. 3:150), got {value!r}"
        ) from exc


@dataclass(frozen=True)
class Config:
    database_url: str = DEFAULT_DATABASE_URL
    output_dir: Path = field(default_factory=lambda: DEFAULT_OUTPUT_DIR)
    cell_metres: float = DEFAULT_CELL_METRES
    variogram_model: str = DEFAULT_VARIOGRAM_MODEL
    nlags: int = DEFAULT_NLAGS
    cv_folds: int = DEFAULT_CV_FOLDS
    cv_seed: int = DEFAULT_CV_SEED
    cv_block_m: float = DEFAULT_CV_BLOCK_M
    kriging_mode: str = DEFAULT_KRIGING_MODE
    none_floor: "float | None" = DEFAULT_NONE_FLOOR
    anisotropy: "tuple[float, float]" = DEFAULT_ANISOTROPY

    @classmethod
    def from_env(cls) -> "Config":
        out = os.environ.get("MUKOO_MODEL_OUTPUT_DIR")
        mode = os.environ.get("MUKOO_KRIGING", DEFAULT_KRIGING_MODE)
        if mode not in KRIGING_MODES:
            raise ValueError(
                f"MUKOO_KRIGING must be one of {KRIGING_MODES}, got {mode!r}"
            )
        floor_raw = os.environ.get("MUKOO_NONE_FLOOR")
        aniso_raw = os.environ.get("MUKOO_ANISOTROPY")
        return cls(
            database_url=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
            output_dir=Path(out) if out else DEFAULT_OUTPUT_DIR,
            kriging_mode=mode,
            none_floor=float(floor_raw) if floor_raw else DEFAULT_NONE_FLOOR,
            anisotropy=parse_anisotropy(aniso_raw) if aniso_raw else DEFAULT_ANISOTROPY,
        )
