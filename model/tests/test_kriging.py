"""Tests for grid construction and kriging prediction."""

from __future__ import annotations

import numpy as np

from mukoo_model.kriging import OrdinaryKrigingModel, make_grid


def test_make_grid_covers_bounds(linear_cloud):
    grid = make_grid(linear_cloud, cell_m=500.0)
    xmin, ymin, xmax, ymax = linear_cloud.bounds_xy()
    # Grid must start at or before the min extent and reach past the max.
    assert grid.x[0] <= xmin
    assert grid.x[-1] >= xmax
    assert grid.y[0] <= ymin
    assert grid.y[-1] >= ymax
    assert grid.cell_m == 500.0
    assert grid.shape == (grid.y.shape[0], grid.x.shape[0])


def test_grid_edges_are_half_a_cell_outside_centres(linear_cloud):
    grid = make_grid(linear_cloud, cell_m=500.0)
    assert np.isclose(grid.west, grid.x[0] - 250.0)
    assert np.isclose(grid.north, grid.y[-1] + 250.0)


def test_kriging_recovers_smooth_field(linear_cloud):
    # On a near-planar field, kriging interpolated back at the data locations
    # should match closely and the surface should span a sensible range.
    model = OrdinaryKrigingModel(nlags=10).fit(linear_cloud)
    grid = make_grid(linear_cloud, cell_m=500.0)
    surface = model.predict_grid(grid)
    assert surface.mean.shape == grid.shape
    assert surface.variance.shape == grid.shape
    # Predicted values stay within the data's range give or take the noise band.
    assert surface.mean.min() > linear_cloud.values.min() - 3.0
    assert surface.mean.max() < linear_cloud.values.max() + 3.0
    # Variance is non-negative everywhere.
    assert np.all(surface.variance >= -1e-9)


def test_variance_lower_near_data_than_far_away(linear_cloud):
    # Kriging variance should grow as you move away from observed points. A point
    # dropped in the middle of the cloud should be more certain than one far out.
    model = OrdinaryKrigingModel(nlags=10).fit(linear_cloud)
    cx, cy = linear_cloud.x.mean(), linear_cloud.y.mean()
    far_x, far_y = linear_cloud.x.max() + 20000.0, linear_cloud.y.max() + 20000.0
    _, var_near = model.predict_points(np.array([cx]), np.array([cy]))
    _, var_far = model.predict_points(np.array([far_x]), np.array([far_y]))
    assert var_far[0] > var_near[0]


def test_variogram_params_exposed(linear_cloud):
    model = OrdinaryKrigingModel(variogram_model="spherical", nlags=10).fit(linear_cloud)
    params = model.variogram_params
    assert params["model"] == "spherical"
    assert params["range_m"] > 0
    assert params["sill"] >= params["nugget"]


def test_predict_before_fit_raises():
    model = OrdinaryKrigingModel()
    try:
        model.predict_points(np.array([0.0]), np.array([0.0]))
    except RuntimeError as exc:
        assert "not fitted" in str(exc)
    else:
        raise AssertionError("expected RuntimeError when predicting before fit")


# -- variogram lag binning ------------------------------------------------


def _nugget_cloud(n=400, nugget_sd=3.0, seed=1):
    """A smooth field plus pure white noise, i.e. a known non-zero nugget.

    The structured part varies over kilometres; the noise is independent per
    point, so the true variogram has a nugget of about ``nugget_sd**2``.
    """
    from mukoo_model.data import PointCloud

    rng = np.random.RandomState(seed)
    x = rng.rand(n) * 20000.0
    y = rng.rand(n) * 20000.0
    z = -100.0 + 5e-4 * x + 3e-4 * y + rng.randn(n) * nugget_sd
    return PointCloud(
        lon=-81.8 + x / 1e5,
        lat=32.35 + y / 1e5,
        x=x,
        y=y,
        values=z,
        crs_epsg=32617,
        metric="rsrp",
        n_raw=n,
    )


def test_log_spacing_resolves_short_lags():
    from mukoo_model.kriging import empirical_variogram

    cloud = _nugget_cloud()
    log_v = empirical_variogram(cloud.x, cloud.y, cloud.values, nlags=25, spacing="log")
    lin_v = empirical_variogram(
        cloud.x, cloud.y, cloud.values, nlags=25, spacing="linear"
    )
    # The whole point: equal-width bins over a 20 km domain can afford at most
    # one bin below a kilometre, so nothing constrains the curve near the origin.
    # Log spacing spends several bins down there.
    assert (log_v.lags < 1000.0).sum() > (lin_v.lags < 1000.0).sum()
    assert (log_v.lags < 1000.0).sum() >= 3
    assert log_v.lags[0] < lin_v.lags[0] / 2


def test_equal_width_bins_miss_the_nugget_and_log_bins_find_it():
    from mukoo_model.kriging import empirical_variogram, fit_variogram_params

    cloud = _nugget_cloud(nugget_sd=3.0)
    truth = 3.0**2  # variance of the injected white noise

    lin = fit_variogram_params(
        empirical_variogram(
            cloud.x, cloud.y, cloud.values, nlags=12, spacing="linear"
        ),
        "exponential",
    )
    log = fit_variogram_params(
        empirical_variogram(cloud.x, cloud.y, cloud.values, nlags=25, spacing="log"),
        "exponential",
    )
    # Coarse equal-width bins extrapolate the nugget toward zero; log bins land
    # within a factor of two of the injected noise variance.
    assert lin[2] < truth / 2
    assert truth / 2 < log[2] < truth * 2


def test_fitted_parameters_reach_pykrige_untransformed():
    """Guards a pykrige trap that silently shrinks the partial sill.

    Given a *list*, pykrige reads element 0 as the full sill and stores
    ``sill - nugget`` as the psill — while its own fit returns a psill. Passing a
    list would krige with a partial sill short by exactly the nugget.
    """
    from mukoo_model.kriging import empirical_variogram, fit_variogram_params

    cloud = _nugget_cloud()
    expected = fit_variogram_params(
        empirical_variogram(cloud.x, cloud.y, cloud.values, nlags=25, spacing="log"),
        "exponential",
    )
    model = OrdinaryKrigingModel(nlags=25, lag_spacing="log").fit(cloud)
    assert np.allclose(model.variogram_params["raw"], expected)
    assert model.variogram_params["nugget"] > 0.0
    assert model.variogram_params["fitted_from_binned_lags"] is True


def test_pykrige_spacing_delegates_variogram_estimation():
    cloud = _nugget_cloud()
    model = OrdinaryKrigingModel(nlags=12, lag_spacing="pykrige").fit(cloud)
    params = model.variogram_params
    assert params["fitted_from_binned_lags"] is False
    assert "empirical" not in params


def test_anisotropy_falls_back_to_pykrige_estimation():
    # pykrige rescales coordinates before its variogram step, so a variogram
    # fitted on unrescaled distances would not describe what it kriges with.
    cloud = _nugget_cloud()
    model = OrdinaryKrigingModel(
        nlags=25, lag_spacing="log", anisotropy_scaling=2.0, anisotropy_angle=30.0
    ).fit(cloud)
    assert model.variogram_params["fitted_from_binned_lags"] is False


def test_max_lag_excludes_distant_pairs():
    from mukoo_model.kriging import empirical_variogram

    cloud = _nugget_cloud()
    capped = empirical_variogram(
        cloud.x, cloud.y, cloud.values, nlags=20, spacing="log", max_lag_m=5000.0
    )
    assert capped.lags[-1] <= 5000.0
    assert capped.max_lag_m == 5000.0


def test_rejects_unknown_lag_spacing():
    import pytest

    with pytest.raises(ValueError, match="lag_spacing"):
        OrdinaryKrigingModel(lag_spacing="quadratic")


def test_thin_binning_falls_back_rather_than_fitting_noise():
    # Too few surviving bins to fit three parameters -> defer to pykrige.
    cloud = _nugget_cloud(n=30)
    model = OrdinaryKrigingModel(
        nlags=3, lag_spacing="log", min_pairs_per_lag=10_000
    ).fit(cloud)
    assert model.variogram_params["fitted_from_binned_lags"] is False
