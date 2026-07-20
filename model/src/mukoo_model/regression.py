"""Regression kriging with a path-loss prior.

Ordinary kriging assumes a constant unknown mean; RSRP has obvious physical
structure instead — it decays with distance from the serving tower. Regression
kriging splits the field into ``trend + residual``:

  trend:     RSRP ≈ a + b·log10(d_nearest_tower)   (log-distance path loss,
             towers bootstrapped from the data — see :mod:`.towers`)
  residual:  ordinary kriging of (value − trend)

Predictions add the trend back; the variance is the residual kriging variance
(trend-coefficient uncertainty is ignored — with ~1.4k points it is dwarfed by
the residual variance, and keeping the surface's variance semantics identical
to ordinary kriging lets everything downstream stay unchanged).

The class mirrors :class:`~mukoo_model.kriging.OrdinaryKrigingModel`'s fit /
predict_points / predict_grid API, so cross-validation and the pipeline can use
either interchangeably. Because CV refits per fold, towers and trend are
re-estimated from each fold's training subset — no leakage.
"""

from __future__ import annotations

import numpy as np

from .kriging import Grid, OrdinaryKrigingModel, SurfaceResult
from .towers import estimate_towers, nearest_tower_distance


class PathLossKrigingModel:
    """Log-distance path-loss trend + ordinary kriging of the residuals."""

    def __init__(
        self,
        *,
        variogram_model: str = "exponential",
        nlags: int = 12,
        cell: np.ndarray | None = None,
        min_tower_samples: int = 20,
    ) -> None:
        # ``cell`` holds the per-point serving-cell labels for the FULL cloud;
        # fit_arrays receives x/y subsets during CV, so labels are matched to
        # rows by exact (x, y) position at fit time via an index map.
        self.variogram_model = variogram_model
        self.nlags = nlags
        self._cell_lookup_xy: np.ndarray | None = None
        self._cell_labels: np.ndarray | None = None
        if cell is not None:
            self._cell_labels = np.asarray(cell, dtype=object)
        self.min_tower_samples = min_tower_samples

        self._residual = OrdinaryKrigingModel(
            variogram_model=variogram_model, nlags=nlags
        )
        self._towers: np.ndarray | None = None
        self._coef: tuple[float, float] | None = None  # (a, b)
        self._metric: str = ""
        self._n_points: int = 0

    # -- fitting -----------------------------------------------------------

    def bind_cloud(self, x: np.ndarray, y: np.ndarray) -> "PathLossKrigingModel":
        """Register the full cloud's coordinates so fit_arrays can map subset
        rows back to their cell labels (CV hands us subsets by value)."""
        self._cell_lookup_xy = np.stack(
            [np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)], axis=1
        )
        return self

    def fit(self, cloud) -> "PathLossKrigingModel":
        self._cell_labels = (
            np.asarray(cloud.cell, dtype=object) if cloud.cell is not None else None
        )
        self.bind_cloud(cloud.x, cloud.y)
        return self.fit_arrays(cloud.x, cloud.y, cloud.values, metric=cloud.metric)

    def _cells_for(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """Cell labels for a subset of the bound cloud, matched by position."""
        if self._cell_labels is None or self._cell_lookup_xy is None:
            raise ValueError(
                "path-loss kriging needs cell labels: fit via fit(cloud) or "
                "bind_cloud() first, with a cloud loaded by load_rsrp_points"
            )
        # exact match: CV subsets are literal slices of the bound arrays.
        full = self._cell_lookup_xy
        sub = np.stack([x, y], axis=1)
        # match rows via a dict keyed on rounded coords (mm precision).
        key = lambda p: (round(p[0] * 1000), round(p[1] * 1000))  # noqa: E731
        index = {key(p): i for i, p in enumerate(full)}
        idx = np.array([index[key(p)] for p in sub], dtype=np.int64)
        return self._cell_labels[idx]

    def fit_arrays(
        self, x, y, values, *, metric: str = "value"
    ) -> "PathLossKrigingModel":
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        z = np.asarray(values, dtype=np.float64)
        cells = self._cells_for(x, y)

        towers = estimate_towers(
            x, y, z, cells, min_samples=self.min_tower_samples
        )
        if towers.shape[0] == 0:
            raise ValueError(
                "no towers could be estimated (too few samples per cell_id)"
            )
        self._towers = towers

        d = nearest_tower_distance(x, y, towers)
        logd = np.log10(d)
        # OLS for value = a + b*log10(d); lstsq is exact and tiny here.
        A = np.stack([np.ones_like(logd), logd], axis=1)
        coef, *_ = np.linalg.lstsq(A, z, rcond=None)
        self._coef = (float(coef[0]), float(coef[1]))

        residuals = z - (coef[0] + coef[1] * logd)
        self._residual.fit_arrays(x, y, residuals, metric=metric)
        self._metric = metric
        self._n_points = int(z.shape[0])
        return self

    # -- prediction --------------------------------------------------------

    def _trend(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        assert self._towers is not None and self._coef is not None
        a, b = self._coef
        return a + b * np.log10(nearest_tower_distance(x, y, self._towers))

    def predict_points(self, x, y):
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        res_mean, res_var = self._residual.predict_points(x, y)
        return res_mean + self._trend(x, y), res_var

    def predict_grid(self, grid: Grid) -> SurfaceResult:
        res = self._residual.predict_grid(grid)
        xx, yy = np.meshgrid(grid.x, grid.y)
        trend = self._trend(xx.ravel(), yy.ravel()).reshape(res.mean.shape)
        params = dict(res.variogram_params)
        a, b = self._coef  # type: ignore[misc]
        params.update(
            trend="pathloss",
            trend_intercept=a,
            trend_slope_per_log10m=b,
            n_towers=int(self._towers.shape[0]),  # type: ignore[union-attr]
        )
        return SurfaceResult(
            mean=res.mean + trend,
            variance=res.variance,
            grid=grid,
            variogram_model=f"pathloss+{self.variogram_model}",
            variogram_params=params,
            metric=self._metric,
            n_points=self._n_points,
        )

    @property
    def variogram_params(self) -> dict:
        params = dict(self._residual.variogram_params)
        if self._coef is not None:
            params.update(
                trend="pathloss",
                trend_intercept=self._coef[0],
                trend_slope_per_log10m=self._coef[1],
            )
        return params
