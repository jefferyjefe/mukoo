"""Cross-validation: does the data support interpolation, and is the
uncertainty honest?

k-fold CV holds out a fraction of points, kriges from the rest, and compares the
prediction at the held-out locations against their true values. It answers two
distinct questions:

1. **Accuracy** — RMSE / MAE / skill (R^2 vs. the naive "predict the mean"
   baseline). If kriging barely beats the mean, the points are too sparse
   relative to the field's correlation range to interpolate between them.
2. **Calibration** — the kriging *variance* claims to be the error variance.
   If ~68% of held-out points fall within +/-1 kriging-sigma and the
   standardized residuals have std ~= 1, the exported uncertainty surface can be
   trusted; if far off, the variance is mis-scaled.

Leave-one-out is k-fold with ``n_folds == n`` (exact, slow: it refits the
variogram n times). Ten folds is the default — a good accuracy/cost trade-off.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from .data import PointCloud
from .kriging import OrdinaryKrigingModel

ModelFactory = Callable[[], OrdinaryKrigingModel]


@dataclass(frozen=True)
class CVResult:
    """Aggregate cross-validation metrics and the raw held-out predictions."""

    n: int
    n_folds: int
    rmse: float
    mae: float
    bias: float  # mean(pred - actual); >0 means predictions run high
    r: float  # Pearson correlation, predicted vs actual
    r2: float  # skill vs. predicting the global mean (1 - SSE/SST)
    min_error: float
    max_error: float
    # Calibration of the kriging variance against realised error.
    mean_kriging_std: float
    residual_std: float
    std_resid_mean: float  # mean of error / kriging_sigma  (target ~0)
    std_resid_std: float  # std  of error / kriging_sigma  (target ~1)
    within_1sigma: float  # fraction with |error| <= 1 kriging-sigma (target ~0.68)
    within_2sigma: float  # fraction with |error| <= 2 kriging-sigma (target ~0.95)
    # Raw per-point arrays, aligned, for plotting or further analysis.
    actual: np.ndarray
    predicted: np.ndarray
    kriging_std: np.ndarray
    metric: str

    def summary(self) -> str:
        """Human-readable block, accuracy first then calibration."""
        lines = [
            f"Cross-validation ({self.n_folds}-fold) of {self.metric} "
            f"on {self.n} points",
            "-" * 60,
            "ACCURACY (held-out predicted vs actual)",
            f"  RMSE            {self.rmse:8.3f} {self._unit}",
            f"  MAE             {self.mae:8.3f} {self._unit}",
            f"  bias (pred-act) {self.bias:+8.3f} {self._unit}",
            f"  Pearson r       {self.r:8.3f}",
            f"  R^2 vs mean     {self.r2:8.3f}   "
            f"({self._skill_verdict})",
            f"  error range     [{self.min_error:+.2f}, {self.max_error:+.2f}] "
            f"{self._unit}",
            "",
            "CALIBRATION (is the kriging variance honest?)",
            f"  mean kriging sd {self.mean_kriging_std:8.3f} {self._unit}",
            f"  residual sd     {self.residual_std:8.3f} {self._unit}",
            f"  std-resid mean  {self.std_resid_mean:+8.3f}   (target ~0)",
            f"  std-resid sd    {self.std_resid_std:8.3f}   (target ~1; "
            f"{self._calib_verdict})",
            f"  within +/-1 sd  {self.within_1sigma:8.1%}   (target ~68%)",
            f"  within +/-2 sd  {self.within_2sigma:8.1%}   (target ~95%)",
        ]
        return "\n".join(lines)

    @property
    def _unit(self) -> str:
        return "dBm" if self.metric in {"rsrp", "rsrq"} else ""

    @property
    def _skill_verdict(self) -> str:
        if self.r2 >= 0.5:
            return "strong: interpolation clearly beats the mean"
        if self.r2 >= 0.2:
            return "moderate: interpolation helps"
        if self.r2 > 0.0:
            return "weak: barely beats the mean"
        return "none: no better than the mean -- too sparse"

    @property
    def _calib_verdict(self) -> str:
        s = self.std_resid_std
        if 0.8 <= s <= 1.25:
            return "well-calibrated"
        return "over-confident" if s > 1.25 else "under-confident"

    def to_dict(self) -> dict:
        """JSON-serialisable metrics (raw per-point arrays excluded)."""
        return {
            "n": self.n,
            "n_folds": self.n_folds,
            "metric": self.metric,
            "accuracy": {
                "rmse": self.rmse,
                "mae": self.mae,
                "bias": self.bias,
                "pearson_r": self.r,
                "r2_vs_mean": self.r2,
                "min_error": self.min_error,
                "max_error": self.max_error,
            },
            "calibration": {
                "mean_kriging_std": self.mean_kriging_std,
                "residual_std": self.residual_std,
                "std_resid_mean": self.std_resid_mean,
                "std_resid_std": self.std_resid_std,
                "within_1sigma": self.within_1sigma,
                "within_2sigma": self.within_2sigma,
            },
        }


def _fold_indices(n: int, n_folds: int, seed: int) -> list[np.ndarray]:
    """Shuffle 0..n-1 with a fixed seed and split into ``n_folds`` groups."""
    if n_folds < 2:
        raise ValueError("n_folds must be >= 2 (use n_folds=n for leave-one-out)")
    if n_folds > n:
        raise ValueError(f"n_folds ({n_folds}) cannot exceed n points ({n})")
    rng = np.random.RandomState(seed)
    order = rng.permutation(n)
    return [np.sort(f) for f in np.array_split(order, n_folds)]


def kfold_cv(
    cloud: PointCloud,
    *,
    model_factory: ModelFactory,
    n_folds: int = 10,
    seed: int = 0,
) -> CVResult:
    """Run k-fold CV and return aggregate metrics + per-point predictions.

    ``model_factory`` builds a fresh (unfitted) kriging model each fold, so the
    variogram is refit on the training subset — the held-out fold never informs
    its own prediction. Set ``n_folds == cloud.n`` for leave-one-out.
    """
    x, y, z = cloud.x, cloud.y, cloud.values
    n = cloud.n

    actual = np.empty(n, dtype=np.float64)
    predicted = np.empty(n, dtype=np.float64)
    kstd = np.empty(n, dtype=np.float64)

    for test_idx in _fold_indices(n, n_folds, seed):
        train_mask = np.ones(n, dtype=bool)
        train_mask[test_idx] = False
        model = model_factory().fit_arrays(
            x[train_mask], y[train_mask], z[train_mask], metric=cloud.metric
        )
        mean, var = model.predict_points(x[test_idx], y[test_idx])
        actual[test_idx] = z[test_idx]
        predicted[test_idx] = mean
        kstd[test_idx] = np.sqrt(np.clip(var, 0.0, None))

    return _metrics(actual, predicted, kstd, n_folds=n_folds, metric=cloud.metric)


def _metrics(
    actual: np.ndarray,
    predicted: np.ndarray,
    kriging_std: np.ndarray,
    *,
    n_folds: int,
    metric: str,
) -> CVResult:
    error = predicted - actual
    n = int(actual.shape[0])
    sse = float(np.sum(error**2))
    sst = float(np.sum((actual - actual.mean()) ** 2))

    # Pearson r; guard the degenerate constant-prediction case.
    if predicted.std() > 0 and actual.std() > 0:
        r = float(np.corrcoef(actual, predicted)[0, 1])
    else:
        r = float("nan")

    # Standardized residuals; guard against any zero kriging-sigma.
    safe_std = np.where(kriging_std > 0, kriging_std, np.nan)
    std_resid = error / safe_std
    finite = np.isfinite(std_resid)

    return CVResult(
        n=n,
        n_folds=n_folds,
        rmse=float(np.sqrt(sse / n)),
        mae=float(np.mean(np.abs(error))),
        bias=float(np.mean(error)),
        r=r,
        r2=float(1.0 - sse / sst) if sst > 0 else float("nan"),
        min_error=float(error.min()),
        max_error=float(error.max()),
        mean_kriging_std=float(np.mean(kriging_std)),
        residual_std=float(np.std(error)),
        std_resid_mean=float(np.nanmean(std_resid)),
        std_resid_std=float(np.nanstd(std_resid)),
        within_1sigma=float(np.mean(np.abs(std_resid[finite]) <= 1.0)),
        within_2sigma=float(np.mean(np.abs(std_resid[finite]) <= 2.0)),
        actual=actual,
        predicted=predicted,
        kriging_std=kriging_std,
        metric=metric,
    )
