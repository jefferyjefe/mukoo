"""Model comparison and anisotropy scan, both judged by cross-validation.

Everything here answers one question — "which model settings should the surface
use?" — with held-out error, never in-sample fit. The scan uses the honest
session scheme when available (falling back to random k-fold) so an anisotropy
that only helps along-track densification doesn't win.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Optional

from .config import Config
from .crossval import CVResult, kfold_cv, session_cv
from .data import PointCloud
from .pipeline import make_model_factory

# Variogram families worth CVing against each other. linear/power are left out:
# they have no sill, so the kriging variance loses its calibration meaning.
SCAN_FAMILIES = ("exponential", "spherical", "gaussian")

# A coarse but sufficient grid: angles every 30° (anisotropy is symmetric under
# 180°), stretch factors up to 3x. Finer search overfits the scan to CV noise.
SCAN_ANGLES = (0.0, 30.0, 60.0, 90.0, 120.0, 150.0)
SCAN_SCALINGS = (1.5, 2.0, 3.0)


def _cv_once(cloud: PointCloud, factory: Callable[[], object], *, folds: int) -> CVResult:
    try:
        return session_cv(cloud, model_factory=factory)
    except ValueError:
        return kfold_cv(cloud, model_factory=factory, n_folds=folds, seed=0)


@dataclass(frozen=True)
class ScanResult:
    scaling: float
    angle: float
    rmse: float
    baseline_rmse: float

    @property
    def helps(self) -> bool:
        # demand a real margin: CV noise on ~1.4k points is a few hundredths.
        return self.rmse < self.baseline_rmse - 0.05


def scan_anisotropy(
    cloud: PointCloud, config: Config, *, folds: int = 5
) -> ScanResult:
    """Grid-search anisotropy (scaling, angle) by CV; returns the best pair.

    ``folds`` is small (5) because the scan runs 19 CVs; the winner should then
    be confirmed with the full CV schemes via ``--kriging ordinary
    --anisotropy S:A``.
    """
    base = make_model_factory(config, cloud, kriging="ordinary")
    baseline = _cv_once(cloud, base, folds=folds).rmse

    best: "tuple[float, float, float]" = (1.0, 0.0, baseline)
    for scaling in SCAN_SCALINGS:
        for angle in SCAN_ANGLES:
            factory = make_model_factory(
                config,
                cloud,
                kriging="ordinary",
                anisotropy_scaling=scaling,
                anisotropy_angle=angle,
            )
            rmse = _cv_once(cloud, factory, folds=folds).rmse
            if rmse < best[2]:
                best = (scaling, angle, rmse)
    return ScanResult(
        scaling=best[0], angle=best[1], rmse=best[2], baseline_rmse=baseline
    )


def compare_models(
    cloud: PointCloud,
    config: Config,
    *,
    folds: int = 10,
    include_anisotropy_scan: bool = True,
) -> "list[tuple[str, CVResult]]":
    """CV each candidate model; returns (label, result) pairs, best RMSE first.

    Candidates: ordinary kriging in each variogram family, path-loss regression
    kriging (skipped with a clear label if cell data can't support towers), and
    — when the scan finds a winning pair — anisotropic ordinary kriging.
    """
    rows: "list[tuple[str, CVResult]]" = []

    for family in SCAN_FAMILIES:
        cfg = replace(config, variogram_model=family)
        factory = make_model_factory(cfg, cloud, kriging="ordinary")
        rows.append((f"ordinary {family}", _cv_once(cloud, factory, folds=folds)))

    try:
        # the ValueError (no towers / no cell labels) surfaces at fit time,
        # inside the CV run — so the whole CV is what gets guarded.
        pathloss = make_model_factory(config, cloud, kriging="pathloss")
        rows.append(("pathloss", _cv_once(cloud, pathloss, folds=folds)))
    except ValueError:
        pass  # no usable cell labels; nothing to compare

    if include_anisotropy_scan:
        scan = scan_anisotropy(cloud, config)
        if scan.helps:
            factory = make_model_factory(
                config,
                cloud,
                kriging="ordinary",
                anisotropy_scaling=scan.scaling,
                anisotropy_angle=scan.angle,
            )
            rows.append(
                (
                    f"ordinary anisotropic {scan.scaling:g}x @ {scan.angle:g}deg",
                    _cv_once(cloud, factory, folds=folds),
                )
            )

    rows.sort(key=lambda r: r[1].rmse)
    return rows
