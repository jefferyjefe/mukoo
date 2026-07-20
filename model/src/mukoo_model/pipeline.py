"""End-to-end coverage-model pipeline: PostGIS -> CV -> surfaces -> GeoTIFF.

The ordering is deliberate and matches how the result should be read: run
cross-validation and surface its numbers *first*, because they decide whether
the interpolated surface is trustworthy at all. Only then fit on the full data,
predict the grid, and write the rasters.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .config import Config
from .crossval import CVResult, kfold_cv
from .data import PointCloud, load_rsrp_points
from .db import make_engine
from .export import build_report, write_report_json, write_surfaces
from .kriging import OrdinaryKrigingModel, SurfaceResult, make_grid


@dataclass
class PipelineResult:
    cloud: PointCloud
    cv: CVResult
    surface: SurfaceResult
    surface_paths: dict
    report_path: Path
    report: dict


def run(
    config: Config,
    *,
    metric: str = "rsrp",
    where: Optional[str] = None,
    prefix: str = "rsrp_kriging",
    on_cv: Optional[Callable[[CVResult], None]] = None,
) -> PipelineResult:
    """Load points, cross-validate, fit, predict the grid, and export.

    ``on_cv`` is invoked with the CV result as soon as it is computed and before
    the (slower) full-grid prediction — so a caller can print the numbers that
    decide trust before committing to the surface.
    """
    engine = make_engine(config.database_url)

    def factory() -> OrdinaryKrigingModel:
        return OrdinaryKrigingModel(
            variogram_model=config.variogram_model, nlags=config.nlags
        )

    cloud = load_rsrp_points(engine, metric=metric, where=where)

    # 1. Trust check first.
    cv = kfold_cv(
        cloud, model_factory=factory, n_folds=config.cv_folds, seed=config.cv_seed
    )
    if on_cv is not None:
        on_cv(cv)

    # 2. Fit on everything and predict the surface + uncertainty.
    model = factory().fit(cloud)
    grid = make_grid(cloud, cell_m=config.cell_metres)
    surface = model.predict_grid(grid)

    # 3. Export rasters + JSON report.
    surface_paths = write_surfaces(config.output_dir, surface, prefix=prefix)
    report = build_report(
        surface,
        cv,
        n_raw=cloud.n_raw,
        n_merged=cloud.n_merged,
        bounds_lonlat=cloud.bounds_lonlat(),
        surface_paths=surface_paths,
    )
    report_path = write_report_json(
        Path(config.output_dir) / f"{prefix}_report.json", report
    )

    return PipelineResult(
        cloud=cloud,
        cv=cv,
        surface=surface,
        surface_paths=surface_paths,
        report_path=report_path,
        report=report,
    )
