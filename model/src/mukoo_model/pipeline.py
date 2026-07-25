"""End-to-end coverage-model pipeline: PostGIS -> CV -> surfaces -> GeoTIFF.

The ordering is deliberate and matches how the result should be read: run
cross-validation and surface its numbers *first*, because they decide whether
the interpolated surface is trustworthy at all. Only then fit on the full data,
predict the grid, and write the rasters.

Three CV schemes run, honest-first:

- **leave-one-session-out** — predict whole unseen drives; the headline number
  for "how wrong is the surface on a road I haven't driven".
- **spatial block** — hold out map tiles; between-area skill.
- **random k-fold** — the flattering along-track number, kept for continuity
  and as the near-road bound.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .config import Config
from .crossval import CVResult, block_cv, kfold_cv, session_cv
from .data import PointCloud, load_rsrp_points
from .db import make_engine
from .export import build_report, write_report_json, write_surfaces
from .kriging import OrdinaryKrigingModel, SurfaceResult, make_grid
from .regression import PathLossKrigingModel


@dataclass
class PipelineResult:
    cloud: PointCloud
    cvs: "list[CVResult]"  # session, block, random — in reporting order
    surface: SurfaceResult
    surface_paths: dict
    report_path: Path
    report: dict

    @property
    def cv(self) -> CVResult:
        """The headline (leave-one-session-out) result, if present, else first."""
        return self.cvs[0]


def make_model_factory(
    config: Config,
    cloud: PointCloud,
    *,
    kriging: str = "ordinary",
    anisotropy_scaling: float = 1.0,
    anisotropy_angle: float = 0.0,
) -> Callable[[], object]:
    """A zero-arg factory for the chosen model, refit-safe for CV folds.

    ``kriging="pathloss"`` builds regression kriging with the log-distance
    tower prior; it binds the cloud's cell labels so per-fold refits can map
    their subset rows back to labels without leakage.
    """
    if kriging == "pathloss":
        if anisotropy_scaling != 1.0 or anisotropy_angle != 0.0:
            raise ValueError(
                "anisotropy is only supported with ordinary kriging; "
                "unset MUKOO_ANISOTROPY / --anisotropy or use kriging='ordinary'"
            )

        def factory() -> PathLossKrigingModel:
            model = PathLossKrigingModel(
                variogram_model=config.variogram_model,
                nlags=config.nlags,
                cell=cloud.cell,
                lag_spacing=config.lag_spacing,
                max_lag_m=config.max_lag_m,
            )
            return model.bind_cloud(cloud.x, cloud.y)

        return factory
    if kriging != "ordinary":
        raise ValueError(f"unknown kriging mode {kriging!r}")

    def factory() -> OrdinaryKrigingModel:
        return OrdinaryKrigingModel(
            variogram_model=config.variogram_model,
            nlags=config.nlags,
            anisotropy_scaling=anisotropy_scaling,
            anisotropy_angle=anisotropy_angle,
            lag_spacing=config.lag_spacing,
            max_lag_m=config.max_lag_m,
        )

    return factory


def run_cvs(
    cloud: PointCloud,
    factory: Callable[[], object],
    *,
    n_folds: int = 10,
    seed: int = 0,
    block_m: float = 2000.0,
    on_cv: Optional[Callable[[CVResult], None]] = None,
) -> "list[CVResult]":
    """Session, block, and random CV (in that order), skipping session CV
    gracefully when the cloud has fewer than two sessions.

    ``on_cv`` fires as each scheme finishes — the honest session numbers reach
    the terminal while the remaining schemes are still computing.
    """
    cvs: "list[CVResult]" = []

    def _add(cv: CVResult) -> None:
        cvs.append(cv)
        if on_cv is not None:
            on_cv(cv)

    try:
        _add(session_cv(cloud, model_factory=factory))
    except ValueError:
        pass  # single session / no labels: the other two schemes still run
    _add(
        block_cv(
            cloud, model_factory=factory, block_m=block_m, n_folds=n_folds, seed=seed
        )
    )
    _add(kfold_cv(cloud, model_factory=factory, n_folds=n_folds, seed=seed))
    return cvs


def run(
    config: Config,
    *,
    metric: str = "rsrp",
    where: Optional[str] = None,
    prefix: str = "rsrp_kriging",
    kriging: Optional[str] = None,
    anisotropy_scaling: Optional[float] = None,
    anisotropy_angle: Optional[float] = None,
    on_cv: Optional[Callable[[CVResult], None]] = None,
) -> PipelineResult:
    """Load points, cross-validate (all schemes), fit, predict, and export.

    ``kriging`` and the anisotropy pair default to the config's values (which
    themselves come from MUKOO_KRIGING / MUKOO_ANISOTROPY), so the refresh
    agent and other callers follow the operator's chosen model automatically;
    pass them explicitly to override for one run.

    ``on_cv`` is invoked per CV result as soon as they are computed and before
    the (slower) full-grid prediction — so a caller can print the numbers that
    decide trust before committing to the surface.
    """
    if kriging is None:
        kriging = config.kriging_mode
    if anisotropy_scaling is None:
        anisotropy_scaling = config.anisotropy[0]
    if anisotropy_angle is None:
        anisotropy_angle = config.anisotropy[1]

    engine = make_engine(config.database_url)
    cloud = load_rsrp_points(
        engine,
        metric=metric,
        where=where,
        none_floor=config.none_floor if metric == "rsrp" else None,
        dedupe_runs=config.dedupe_runs,
    )
    factory = make_model_factory(
        config,
        cloud,
        kriging=kriging,
        anisotropy_scaling=anisotropy_scaling,
        anisotropy_angle=anisotropy_angle,
    )

    # 1. Trust check first — honest schemes ahead of the flattering one.
    cvs = run_cvs(
        cloud,
        factory,
        n_folds=config.cv_folds,
        seed=config.cv_seed,
        block_m=config.cv_block_m,
        on_cv=on_cv,
    )

    # 2. Fit on everything and predict the surface + uncertainty.
    model = factory()
    model.fit(cloud)
    grid = make_grid(cloud, cell_m=config.cell_metres)
    surface = model.predict_grid(grid)

    # 3. Export rasters + JSON report.
    surface_paths = write_surfaces(config.output_dir, surface, prefix=prefix)
    report = build_report(
        surface,
        cvs,
        n_raw=cloud.n_raw,
        n_merged=cloud.n_merged,
        bounds_lonlat=cloud.bounds_lonlat(),
        surface_paths=surface_paths,
        dedupe_runs=config.dedupe_runs,
        n_dedupe_dropped=cloud.n_dedupe_dropped,
    )
    report_path = write_report_json(
        Path(config.output_dir) / f"{prefix}_report.json", report
    )

    return PipelineResult(
        cloud=cloud,
        cvs=cvs,
        surface=surface,
        surface_paths=surface_paths,
        report_path=report_path,
        report=report,
    )
