"""Ordinary kriging of a scalar field (RSRP) over the survey area.

Thin, typed wrapper around :class:`pykrige.ok.OrdinaryKriging`. It fits an
empirical variogram to the projected points, then predicts on either an
arbitrary set of points (used by cross-validation) or a regular grid (used to
build the exported surfaces). Every prediction carries pykrige's kriging
variance, which is the model's own estimate of its uncertainty.

**Lag binning.** pykrige bins inter-point distances into ``nlags`` *equal-width*
bins spanning the whole range of separations, and offers no way to cap the
maximum lag. Over a survey ~22 km across that makes the first bin ~1.9 km wide,
so no bin observes sub-kilometre structure and the fitted nugget is extrapolated
to ~0 — the model then claims near-perfect knowledge at short range and its
kriging variance is far too small (within-1-sigma ~40% against a 68% target).

So the empirical variogram is computed here instead, with control over the
maximum lag and over *logarithmic* lag spacing, which resolves tens of metres
and tens of kilometres in one pass. The fitted parameters are then handed to
pykrige via ``variogram_parameters``, so pykrige does the kriging algebra but
not the variogram estimation. ``lag_spacing="pykrige"`` restores the old
delegated behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from pykrige import variogram_models as _vm
from pykrige.ok import OrdinaryKriging
from scipy.optimize import least_squares
from scipy.spatial.distance import pdist

from .data import PointCloud

# pykrige names the fitted parameters differently per family; these are the
# ones whose parameter vector is [partial_sill, range, nugget].
_PSILL_RANGE_NUGGET = {"spherical", "exponential", "gaussian"}

# The exact model functions pykrige will use for the kriging itself, so the
# parameters fitted here mean precisely what pykrige thinks they mean. (Its
# "range" is the practical range: the exponential uses range/3 as the e-folding
# length, so re-deriving these by hand would quietly disagree.)
_VARIOGRAM_FUNCTIONS = {
    "linear": _vm.linear_variogram_model,
    "power": _vm.power_variogram_model,
    "gaussian": _vm.gaussian_variogram_model,
    "spherical": _vm.spherical_variogram_model,
    "exponential": _vm.exponential_variogram_model,
    "hole-effect": _vm.hole_effect_variogram_model,
}

# Mirrored in ``mukoo_model.config.LAG_SPACINGS``; see the note there.
LAG_SPACINGS = ("log", "linear", "pykrige")

# Shortest lag for logarithmic binning, in metres. Below roughly this, pairs are
# GPS jitter on one stretch of road rather than distinct locations, and the bins
# hold too few pairs to mean anything.
DEFAULT_MIN_LAG_M = 25.0

# A lag bin averaged over fewer pairs than this is dropped as noise. Cross-
# validation refits on subsets, so short-lag bins can thin out per fold.
DEFAULT_MIN_PAIRS_PER_LAG = 10

# Fitting three parameters needs more than three bins to be meaningful; below
# this we fall back to letting pykrige estimate the variogram itself.
_MIN_BINS_TO_FIT = 5


@dataclass(frozen=True)
class EmpiricalVariogram:
    """Binned experimental semivariance: the data the model is fitted to."""

    lags: np.ndarray  # (nbins,) mean separation within each bin, metres
    semivariance: np.ndarray  # (nbins,) mean 0.5*(z_i - z_j)^2 within each bin
    counts: np.ndarray  # (nbins,) pairs averaged per bin
    max_lag_m: float
    spacing: str

    @property
    def nbins(self) -> int:
        return int(self.lags.shape[0])


def empirical_variogram(
    x: np.ndarray,
    y: np.ndarray,
    values: np.ndarray,
    *,
    nlags: int,
    max_lag_m: "float | None" = None,
    spacing: str = "log",
    min_lag_m: float = DEFAULT_MIN_LAG_M,
    min_pairs: int = DEFAULT_MIN_PAIRS_PER_LAG,
) -> EmpiricalVariogram:
    """Bin pairwise semivariance, optionally on a logarithmic lag axis.

    ``max_lag_m=None`` uses every pair. Capping it concentrates the fit on the
    lags that actually drive kriging weights, but beware: if the variogram has
    not plateaued inside the cap, the fitted range and sill run away (the curve
    is still rising, so a bounded model reads that as a huge range). On this
    survey, log spacing over the *full* range behaves best — short lags get
    their own bins either way, and the far field still pins the sill.
    """
    if spacing not in ("log", "linear"):
        raise ValueError(f"spacing must be 'log' or 'linear', got {spacing!r}")
    coords = np.column_stack(
        [np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)]
    )
    d = pdist(coords)
    # 0.5 * squared difference is the semivariance contribution of each pair.
    g = 0.5 * pdist(np.asarray(values, dtype=np.float64)[:, None], "sqeuclidean")

    cap = float(d.max()) if max_lag_m is None else float(max_lag_m)
    keep = (d > 0.0) & (d <= cap)
    d, g = d[keep], g[keep]
    if d.size == 0:
        raise ValueError(f"no point pairs within max_lag_m={cap}")

    if spacing == "log":
        lo = max(min_lag_m, float(d.min()))
        edges = np.geomspace(lo, cap, nlags + 1) if lo < cap else np.array([lo, cap])
    else:
        edges = np.linspace(0.0, cap, nlags + 1)

    idx = np.digitize(d, edges) - 1
    lags, semis, counts = [], [], []
    for b in range(len(edges) - 1):
        m = idx == b
        n = int(m.sum())
        if n < min_pairs:
            continue
        lags.append(float(d[m].mean()))
        semis.append(float(g[m].mean()))
        counts.append(n)
    return EmpiricalVariogram(
        lags=np.asarray(lags, dtype=np.float64),
        semivariance=np.asarray(semis, dtype=np.float64),
        counts=np.asarray(counts, dtype=np.int64),
        max_lag_m=cap,
        spacing=spacing,
    )


def fit_variogram_params(
    emp: EmpiricalVariogram, variogram_model: str
) -> "list[float]":
    """Least-squares fit of a pykrige variogram family to binned semivariance.

    Mirrors pykrige's own routine — same parameter order, same ``soft_l1`` loss
    so a wild far-field bin cannot drag the fit — with one deliberate change:
    the range may reach 3x the longest lag instead of being clamped to it. Under
    a capped ``max_lag_m`` the true range can exceed the window, and clamping
    would hide that by pinning the range to the cap.
    """
    if variogram_model not in _VARIOGRAM_FUNCTIONS:
        raise ValueError(f"unknown variogram model {variogram_model!r}")
    fn = _VARIOGRAM_FUNCTIONS[variogram_model]
    lags, semis = emp.lags, emp.semivariance
    smax, smin = float(semis.max()), float(semis.min())
    lmax = float(lags.max())

    if variogram_model == "linear":
        x0 = [(smax - smin) / (lmax - float(lags.min()) or 1.0), smin]
        bnds = ([0.0, 0.0], [np.inf, smax])
    elif variogram_model == "power":
        x0 = [(smax - smin) / (lmax - float(lags.min()) or 1.0), 1.1, smin]
        bnds = ([0.0, 0.001, 0.0], [np.inf, 1.999, smax])
    else:
        x0 = [smax - smin, 0.25 * lmax, smin]
        bnds = ([0.0, 0.0, 0.0], [10.0 * smax, 3.0 * lmax, smax])

    res = least_squares(
        lambda m: fn(m, lags) - semis,
        x0,
        bounds=bnds,
        loss="soft_l1",
    )
    return [float(v) for v in res.x]


def _params_as_pykrige_dict(variogram_model: str, params: "list[float]") -> dict:
    """Wrap fitted parameters so pykrige cannot reinterpret them.

    Passing a *list* to pykrige is a trap: for the psill/range/nugget families it
    treats ``params[0]`` as the full **sill** and stores ``params[0] - nugget`` as
    the partial sill — while its own internal fit returns and stores the partial
    sill directly. Handing it a list of partial sills therefore krige with a
    variogram whose psill is short by exactly the nugget, silently. The dict form
    keyed on ``psill`` is passed through untouched, so use it always.
    """
    if variogram_model == "linear":
        slope, nugget = params
        return {"slope": slope, "nugget": nugget}
    if variogram_model == "power":
        scale, exponent, nugget = params
        return {"scale": scale, "exponent": exponent, "nugget": nugget}
    psill, rng, nugget = params
    return {"psill": psill, "range": rng, "nugget": nugget}


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
        nlags: int = 25,
        anisotropy_scaling: float = 1.0,
        anisotropy_angle: float = 0.0,
        lag_spacing: str = "log",
        max_lag_m: "float | None" = None,
        min_lag_m: float = DEFAULT_MIN_LAG_M,
        min_pairs_per_lag: int = DEFAULT_MIN_PAIRS_PER_LAG,
    ) -> None:
        # anisotropy_scaling stretches distances perpendicular to
        # anisotropy_angle (degrees CCW from east): signal along a road can
        # decorrelate differently from across it. 1.0/0.0 = isotropic (default);
        # scan_anisotropy() in compare mode searches for a better pair by CV.
        if lag_spacing not in LAG_SPACINGS:
            raise ValueError(
                f"lag_spacing must be one of {LAG_SPACINGS}, got {lag_spacing!r}"
            )
        self.variogram_model = variogram_model
        self.nlags = nlags
        self.anisotropy_scaling = anisotropy_scaling
        self.anisotropy_angle = anisotropy_angle
        self.lag_spacing = lag_spacing
        self.max_lag_m = max_lag_m
        self.min_lag_m = min_lag_m
        self.min_pairs_per_lag = min_pairs_per_lag
        self._ok: OrdinaryKriging | None = None
        self._metric: str = ""
        self._n_points: int = 0
        self._empirical: "EmpiricalVariogram | None" = None
        self._fitted_here: bool = False

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
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        values = np.asarray(values, dtype=np.float64)

        # Estimate the variogram here (so the lag axis is ours to choose) and
        # hand pykrige the parameters. Anisotropy is pykrige's own coordinate
        # rescaling, applied before its variogram step, so a variogram fitted on
        # unrescaled distances would not match — leave those runs to pykrige.
        params: "list[float] | None" = None
        empirical: "EmpiricalVariogram | None" = None
        isotropic = self.anisotropy_scaling == 1.0 and self.anisotropy_angle == 0.0
        if self.lag_spacing != "pykrige" and isotropic:
            empirical = empirical_variogram(
                x,
                y,
                values,
                nlags=self.nlags,
                max_lag_m=self.max_lag_m,
                spacing=self.lag_spacing,
                min_lag_m=self.min_lag_m,
                min_pairs=self.min_pairs_per_lag,
            )
            if empirical.nbins >= _MIN_BINS_TO_FIT:
                params = fit_variogram_params(empirical, self.variogram_model)
            else:
                empirical = None  # too thin to fit; let pykrige try

        self._ok = OrdinaryKriging(
            x,
            y,
            values,
            variogram_model=self.variogram_model,
            variogram_parameters=(
                _params_as_pykrige_dict(self.variogram_model, params)
                if params is not None
                else None
            ),
            nlags=self.nlags,
            anisotropy_scaling=self.anisotropy_scaling,
            anisotropy_angle=self.anisotropy_angle,
            enable_plotting=False,
            coordinates_type="euclidean",
        )
        self._metric = metric
        self._n_points = int(values.shape[0])
        self._empirical = empirical
        self._fitted_here = params is not None
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
        out: dict = {
            "model": self.variogram_model,
            "raw": raw,
            "nlags": self.nlags,
            "lag_spacing": self.lag_spacing,
            "max_lag_m": self.max_lag_m,
            # False means pykrige estimated the variogram (equal-width lag bins
            # over the full separation range) rather than this module.
            "fitted_from_binned_lags": self._fitted_here,
        }
        if self.anisotropy_scaling != 1.0 or self.anisotropy_angle != 0.0:
            out["anisotropy_scaling"] = self.anisotropy_scaling
            out["anisotropy_angle"] = self.anisotropy_angle
        if self.variogram_model in _PSILL_RANGE_NUGGET and len(raw) == 3:
            psill, rng, nugget = raw
            out.update(
                partial_sill=psill,
                range_m=rng,
                nugget=nugget,
                sill=psill + nugget,
            )
        if self._empirical is not None:
            emp = self._empirical
            fn = _VARIOGRAM_FUNCTIONS[self.variogram_model]
            fitted = np.asarray(fn(raw, emp.lags), dtype=np.float64)
            resid = fitted - emp.semivariance
            short = emp.lags <= 500.0
            out["empirical"] = {
                "n_bins": emp.nbins,
                "first_lag_m": float(emp.lags[0]),
                "last_lag_m": float(emp.lags[-1]),
                # Is the model honest where it used to be blind? A nugget fitted
                # to ~0 while these bins read tens of dBm^2 is the failure mode
                # this binning exists to prevent.
                "rmse_all_bins": float(np.sqrt(np.mean(resid**2))),
                "rmse_bins_under_500m": (
                    float(np.sqrt(np.mean(resid[short] ** 2))) if short.any() else None
                ),
                "bins": [
                    {
                        "lag_m": float(L),
                        "pairs": int(C),
                        "empirical": float(S),
                        "fitted": float(F),
                    }
                    for L, C, S, F in zip(
                        emp.lags, emp.counts, emp.semivariance, fitted
                    )
                ],
            }
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
