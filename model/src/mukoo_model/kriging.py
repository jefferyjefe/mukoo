"""Ordinary kriging of a scalar field (RSRP) over the survey area.

Thin, typed wrapper around :class:`pykrige.ok.OrdinaryKriging`. It fits an
empirical variogram to the projected points, then predicts on either an
arbitrary set of points (used by cross-validation) or a regular grid (used to
build the exported surfaces). Every prediction carries pykrige's kriging
variance, which is the model's own estimate of its uncertainty.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from pykrige.ok import OrdinaryKriging

from .data import PointCloud

# pykrige names the fitted parameters differently per family; these are the
# ones whose parameter vector is [partial_sill, range, nugget].
_PSILL_RANGE_NUGGET = {"spherical", "exponential", "gaussian"}


@dataclass(frozen=True)
class Grid:
    """A regular grid of cell centres in a projected CRS (metres)."""

    x: np.ndarray  # (ncols,) ascending cell-centre eastings
    y: np.ndarray  # (nrows,) ascending cell-centre northings
    cell_m: float
    crs_epsg: int

    @property
    def shape(self) -> tuple[int, int]:
        return (int(self.y.shape[0]), int(self.x.shape[0]))

    @property
    def west(self) -> float:
        """Left edge of the westmost column (cell centre minus half a cell)."""
        return float(self.x[0] - self.cell_m / 2.0)

    @property
    def north(self) -> float:
        """Top edge of the northmost row (cell centre plus half a cell)."""
        return float(self.y[-1] + self.cell_m / 2.0)


def make_grid(cloud: PointCloud, *, cell_m: float, pad_m: float = 0.0) -> Grid:
    """Build a grid spanning the cloud's bounding box (optionally padded).

    The grid is inclusive of both extremes: ``pad_m`` extends it outward on every
    side, which is useful if you want a margin around the driven roads rather
    than clipping exactly to them.
    """
    xmin, ymin, xmax, ymax = cloud.bounds_xy()
    xmin -= pad_m
    ymin -= pad_m
    xmax += pad_m
    ymax += pad_m
    # +cell_m so arange includes the far edge; guard against degenerate extents.
    gx = np.arange(xmin, xmax + cell_m, cell_m, dtype=np.float64)
    gy = np.arange(ymin, ymax + cell_m, cell_m, dtype=np.float64)
    return Grid(x=gx, y=gy, cell_m=float(cell_m), crs_epsg=cloud.crs_epsg)


@dataclass(frozen=True)
class SurfaceResult:
    """Predicted mean and kriging variance over a grid, plus provenance."""

    mean: np.ndarray  # (nrows, ncols), gy ascending (row 0 = south)
    variance: np.ndarray  # (nrows, ncols)
    grid: Grid
    variogram_model: str
    variogram_params: dict
    metric: str
    n_points: int

    @property
    def stddev(self) -> np.ndarray:
        """Kriging standard deviation (same units as the metric, e.g. dBm)."""
        return np.sqrt(np.clip(self.variance, 0.0, None))


class OrdinaryKrigingModel:
    """Fit-once ordinary kriging model with grid and point prediction.

    A fresh instance is fit per cross-validation fold and once on the full data
    for the exported surface, so fitting stays cheap and side-effect free.
    """

    def __init__(
        self,
        *,
        variogram_model: str = "exponential",
        nlags: int = 12,
    ) -> None:
        self.variogram_model = variogram_model
        self.nlags = nlags
        self._ok: OrdinaryKriging | None = None
        self._metric: str = ""
        self._n_points: int = 0

    def fit(self, cloud: PointCloud) -> "OrdinaryKrigingModel":
        return self.fit_arrays(cloud.x, cloud.y, cloud.values, metric=cloud.metric)

    def fit_arrays(
        self,
        x: np.ndarray,
        y: np.ndarray,
        values: np.ndarray,
        *,
        metric: str = "value",
    ) -> "OrdinaryKrigingModel":
        """Fit on raw projected arrays (used directly by cross-validation)."""
        self._ok = OrdinaryKriging(
            np.asarray(x, dtype=np.float64),
            np.asarray(y, dtype=np.float64),
            np.asarray(values, dtype=np.float64),
            variogram_model=self.variogram_model,
            nlags=self.nlags,
            enable_plotting=False,
            coordinates_type="euclidean",
        )
        self._metric = metric
        self._n_points = int(np.asarray(values).shape[0])
        return self

    def _require_fit(self) -> OrdinaryKriging:
        if self._ok is None:
            raise RuntimeError("model is not fitted; call fit() first")
        return self._ok

    @property
    def variogram_params(self) -> dict:
        """Fitted variogram parameters as a labelled dict, plus the raw vector.

        For spherical/exponential/gaussian the vector is
        ``[partial_sill, range, nugget]``; sill = partial_sill + nugget. Range is
        in metres and is the headline number for "how far does one reading tell
        you about its neighbours".
        """
        ok = self._require_fit()
        raw = [float(v) for v in np.asarray(ok.variogram_model_parameters).ravel()]
        out: dict = {"model": self.variogram_model, "raw": raw, "nlags": self.nlags}
        if self.variogram_model in _PSILL_RANGE_NUGGET and len(raw) == 3:
            psill, rng, nugget = raw
            out.update(
                partial_sill=psill,
                range_m=rng,
                nugget=nugget,
                sill=psill + nugget,
            )
        return out

    def predict_points(
        self, x: np.ndarray, y: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """Predict mean and variance at scattered points."""
        ok = self._require_fit()
        mean, var = ok.execute(
            "points",
            np.asarray(x, dtype=np.float64),
            np.asarray(y, dtype=np.float64),
        )
        return np.asarray(mean, dtype=np.float64), np.asarray(var, dtype=np.float64)

    def predict_grid(self, grid: Grid) -> SurfaceResult:
        """Predict mean and variance over a full grid.

        pykrige returns arrays shaped ``(len(gy), len(gx))`` with ``gy``
        ascending, i.e. row 0 is the southernmost. GeoTIFF export flips this to
        north-up; we keep the native orientation here.
        """
        ok = self._require_fit()
        mean, var = ok.execute("grid", grid.x, grid.y)
        return SurfaceResult(
            mean=np.asarray(mean, dtype=np.float64),
            variance=np.asarray(var, dtype=np.float64),
            grid=grid,
            variogram_model=self.variogram_model,
            variogram_params=self.variogram_params,
            metric=self._metric,
            n_points=self._n_points,
        )
