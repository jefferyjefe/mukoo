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

# Number of empirical-variogram lag bins. pykrige defaults to 6; with the
# logarithmic lag axis below, 25 bins put ~8 of them under 200 m while still
# reaching the far field, which is what pins down the nugget.
DEFAULT_NLAGS = 25

# How inter-point distances are binned when estimating the variogram:
#   "log"     — logarithmic lag axis (default). Resolves tens of metres and tens
#               of kilometres at once, so the nugget is measured rather than
#               extrapolated.
#   "linear"  — equal-width bins, but honouring MUKOO_MAX_LAG_M.
#   "pykrige" — delegate to pykrige: equal-width bins over the full separation
#               range, no cap. Reproduces reports written before this existed;
#               on a ~22 km survey its first bin is ~1.9 km wide, which fits the
#               nugget to ~0 and makes the kriging variance far too confident.
DEFAULT_LAG_SPACING = "log"

# Kept in step with ``mukoo_model.kriging.LAG_SPACINGS`` by hand: importing that
# module here would drag pykrige/scipy into every entry point, including
# mukoo-claims, which deliberately keeps heavy imports off its fast path.
LAG_SPACINGS = ("log", "linear", "pykrige")

# Cap on the longest lag used to fit the variogram, in metres. None = use every
# pair. Capping focuses the fit on the lags that drive kriging weights, but only
# helps if the variogram plateaus inside the cap — on this survey the empirical
# curve is still climbing at 6 km, so a cap there sends the fitted range and sill
# running away. Left unset for that reason; the log lag axis is what buys the
# short-range resolution.
DEFAULT_MAX_LAG_M: "float | None" = None

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

# Collapse runs of consecutive samples whose (rsrp, rsrq, sinr) triple is
# unchanged within a session down to their first sample. The logger samples every
# ~3.5 s but the modem refreshes its signal report far more slowly, so such a run
# is one reading re-read — not independent observations. Off by default so the
# historical row counts and published reports stay reproducible; settable via
# MUKOO_DEDUPE_RUNS so the auto-refresh agent and the suggester pick it up
# without CLI flags. See ``dedupe_runs`` in ``mukoo_model.data``.
DEFAULT_DEDUPE_RUNS = False

# Sessions with fewer than this many raw rows are dropped before modelling: a
# start that records one or two samples is a phantom service start, not a drive.
# On the current data the split is unambiguous — 8 sessions of exactly 1 row and
# 16 of 60+, nothing between — so 3 sits in an empty band and cannot clip a real
# drive. Counted over raw rows, since dedupe can legitimately shrink a long drive
# to a handful of points. Set to 1 to disable. See MUKOO_MIN_SESSION_ROWS.
DEFAULT_MIN_SESSION_ROWS = 3

# How far from a measurement the surface is still predicted, as a multiple of
# the fitted variogram range: cells beyond it are left NaN instead of kriged.
# The grid spans the bounding box of the drives, so one 150 km trip stretched it
# to 161 x 76 km — 553k cells, nearly all of them far enough from any reading
# that kriging just returns the global mean at maximum variance. That is wasted
# compute, and it hands the suggester a "highest uncertainty" map whose winners
# are all empty countryside. One range is the natural cutoff: past it a reading
# tells you nothing about its neighbours by definition. Set to 0 via
# MUKOO_SUPPORT_RANGE_MULTIPLE to predict the whole box, as builds before this
# setting existed did.
DEFAULT_SUPPORT_RANGE_MULTIPLE = 1.0

# Variogram anisotropy (scaling, angle-deg CCW from east) for ordinary kriging.
# (1.0, 0.0) = isotropic. Settable via MUKOO_ANISOTROPY as "SCALING:ANGLE"
# (e.g. "3:150") so a winner found by `mukoo-krige --compare` can be adopted by
# the auto-refresh agent and the suggester without CLI flags.
DEFAULT_ANISOTROPY: "tuple[float, float]" = (1.0, 0.0)


def parse_bool(value: str) -> bool:
    """Parse a boolean environment variable ("1", "true", "yes", "on")."""
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"expected a boolean like 1/0 or true/false, got {value!r}")


def parse_anisotropy(value: str) -> "tuple[float, float]":
    """Parse "SCALING:ANGLE" (e.g. "3:150") into (scaling, angle_deg)."""
    try:
        scaling_s, angle_s = value.split(":", 1)
        return (float(scaling_s), float(angle_s))
    except ValueError as exc:
        raise ValueError(
            f"anisotropy must look like SCALING:ANGLE (e.g. 3:150), got {value!r}"
        ) from exc


def parse_support_range_multiple(value: str) -> float:
    """Parse a grid-support radius multiple: any float >= 0 (0 disables masking).

    Rejected rather than waved through, because a negative multiple is not the
    "mask everything" it looks like: ``pipeline.support_radius_m`` treats any
    multiple at or below zero as masking-off, so a typo would quietly hand back
    the whole 553k-cell bounding box while reading like a tightened radius.
    Shared with the ``--support-range-multiple`` flag so the two entry points
    cannot drift apart on what they accept or what it means.
    """
    multiple = float(value)
    if multiple < 0.0:
        raise ValueError(
            f"support range multiple must be >= 0 (0 disables masking), got {value!r}"
        )
    return multiple


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
    dedupe_runs: bool = DEFAULT_DEDUPE_RUNS
    min_session_rows: int = DEFAULT_MIN_SESSION_ROWS
    lag_spacing: str = DEFAULT_LAG_SPACING
    max_lag_m: "float | None" = DEFAULT_MAX_LAG_M
    support_range_multiple: float = DEFAULT_SUPPORT_RANGE_MULTIPLE

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
        dedupe_raw = os.environ.get("MUKOO_DEDUPE_RUNS")
        spacing = os.environ.get("MUKOO_LAG_SPACING", DEFAULT_LAG_SPACING)
        if spacing not in LAG_SPACINGS:
            raise ValueError(
                f"MUKOO_LAG_SPACING must be one of {LAG_SPACINGS}, got {spacing!r}"
            )
        max_lag_raw = os.environ.get("MUKOO_MAX_LAG_M")
        min_rows_raw = os.environ.get("MUKOO_MIN_SESSION_ROWS")
        support_raw = os.environ.get("MUKOO_SUPPORT_RANGE_MULTIPLE")
        return cls(
            database_url=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
            output_dir=Path(out) if out else DEFAULT_OUTPUT_DIR,
            kriging_mode=mode,
            none_floor=float(floor_raw) if floor_raw else DEFAULT_NONE_FLOOR,
            anisotropy=parse_anisotropy(aniso_raw) if aniso_raw else DEFAULT_ANISOTROPY,
            dedupe_runs=(
                parse_bool(dedupe_raw) if dedupe_raw else DEFAULT_DEDUPE_RUNS
            ),
            lag_spacing=spacing,
            max_lag_m=float(max_lag_raw) if max_lag_raw else DEFAULT_MAX_LAG_M,
            min_session_rows=(
                int(min_rows_raw) if min_rows_raw else DEFAULT_MIN_SESSION_ROWS
            ),
            support_range_multiple=(
                parse_support_range_multiple(support_raw)
                if support_raw
                else DEFAULT_SUPPORT_RANGE_MULTIPLE
            ),
        )
