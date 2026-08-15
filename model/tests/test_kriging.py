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


# -- grid support masking -------------------------------------------------


def test_support_is_none_unless_asked_for(linear_cloud):
    # Back-compat: every existing caller keeps getting a fully predicted grid.
    grid = make_grid(linear_cloud, cell_m=500.0)
    assert grid.support is None
    assert grid.support_radius_m is None
    assert grid.n_supported == grid.shape[0] * grid.shape[1]


def test_grid_records_the_radius_that_drew_the_mask(linear_cloud):
    # A mask cannot be read back without knowing how far "supported" was.
    grid = make_grid(linear_cloud, cell_m=1000.0, support_radius_m=1234.0)
    assert grid.support_radius_m == 1234.0


def test_cells_near_data_are_supported_and_distant_ones_are_not(linear_cloud):
    # 20 km of padding around a 10 km cloud: the corners are far outside any
    # plausible radius, the cell holding a measurement is inside every one.
    grid = make_grid(
        linear_cloud, cell_m=1000.0, pad_m=20000.0, support_radius_m=1000.0
    )
    assert grid.support is not None
    assert grid.support.shape == grid.shape
    row = int(np.argmin(np.abs(grid.y - linear_cloud.y[0])))
    col = int(np.argmin(np.abs(grid.x - linear_cloud.x[0])))
    assert grid.support[row, col]
    assert not grid.support[0, 0]
    assert not grid.support[-1, -1]


def test_support_radius_is_inclusive():
    # "Within the radius" includes the boundary. Worth pinning: the obvious
    # KD-tree speedup (distance_upper_bound) quietly makes it exclusive.
    from mukoo_model.kriging import _support_mask

    x = np.array([0.0])
    y = np.array([0.0])
    gx = np.array([0.0, 1000.0, 1000.1])
    mask = _support_mask(gx, np.array([0.0]), x, y, 1000.0)
    assert mask.tolist() == [[True, True, False]]


def test_support_mask_matches_brute_force_distances(linear_cloud):
    # The KD-tree is an optimisation, not a different definition: on a grid small
    # enough to check exhaustively it must agree cell for cell.
    radius = 800.0
    grid = make_grid(
        linear_cloud, cell_m=1000.0, pad_m=5000.0, support_radius_m=radius
    )
    xx, yy = np.meshgrid(grid.x, grid.y)
    nearest = np.hypot(
        xx[..., None] - linear_cloud.x[None, None, :],
        yy[..., None] - linear_cloud.y[None, None, :],
    ).min(axis=2)
    assert np.array_equal(grid.support, nearest <= radius)


def test_predict_grid_is_nan_exactly_where_unsupported(linear_cloud):
    model = OrdinaryKrigingModel(nlags=10).fit(linear_cloud)
    grid = make_grid(
        linear_cloud, cell_m=1000.0, pad_m=15000.0, support_radius_m=1500.0
    )
    surface = model.predict_grid(grid)
    assert grid.support.any() and not grid.support.all()  # a real mixture
    for field in (surface.mean, surface.variance):
        assert field.shape == grid.shape
        assert np.all(np.isnan(field[~grid.support]))
        assert np.all(np.isfinite(field[grid.support]))


def test_stddev_keeps_nan_rather_than_reading_as_certainty(linear_cloud):
    # np.clip(variance, 0, None) must not pull NaN up to 0: a cell with no
    # prediction would then export as the most confident cell on the map.
    model = OrdinaryKrigingModel(nlags=10).fit(linear_cloud)
    grid = make_grid(
        linear_cloud, cell_m=1000.0, pad_m=15000.0, support_radius_m=1500.0
    )
    stddev = model.predict_grid(grid).stddev
    assert np.all(np.isnan(stddev[~grid.support]))
    assert np.all(np.isfinite(stddev[grid.support]))


def test_masking_predicts_far_fewer_cells(linear_cloud):
    # The point of the exercise: a bounding box stretched by one long drive is
    # mostly empty countryside, and kriging it is pure cost.
    kwargs = dict(cell_m=1000.0, pad_m=20000.0)
    full = make_grid(linear_cloud, **kwargs)
    masked = make_grid(linear_cloud, support_radius_m=1500.0, **kwargs)
    assert masked.n_supported < full.n_supported / 4

    model = OrdinaryKrigingModel(nlags=10).fit(linear_cloud)
    surface = model.predict_grid(masked)
    assert int(np.isfinite(surface.mean).sum()) == masked.n_supported


def test_masking_changes_which_cells_are_predicted_not_their_values(linear_cloud):
    model = OrdinaryKrigingModel(nlags=10).fit(linear_cloud)
    kwargs = dict(cell_m=1000.0, pad_m=10000.0)
    full = model.predict_grid(make_grid(linear_cloud, **kwargs))
    masked_grid = make_grid(linear_cloud, support_radius_m=2000.0, **kwargs)
    masked = model.predict_grid(masked_grid)
    keep = masked_grid.support
    assert np.allclose(masked.mean[keep], full.mean[keep])
    assert np.allclose(masked.variance[keep], full.variance[keep])


def test_support_radius_can_exclude_everything(linear_cloud):
    # Degenerate but reachable (a tiny multiple of a tiny range); it must return
    # an all-NaN surface rather than handing pykrige an empty point list.
    model = OrdinaryKrigingModel(nlags=10).fit(linear_cloud)
    grid = make_grid(linear_cloud, cell_m=1000.0, support_radius_m=0.0)
    assert grid.n_supported == 0
    assert np.all(np.isnan(model.predict_grid(grid).mean))


# -- bounded-memory chunking ----------------------------------------------


def _mixed_support_grid(cloud, cell_m=2000.0):
    """A small grid with a genuine mixture of supported and unsupported cells.

    Deliberately tiny (tens of cells), because pykrige rebuilds and inverts the
    kriging matrix on every call and these tests chunk down to single figures.
    """
    grid = make_grid(cloud, cell_m=cell_m, pad_m=2000.0, support_radius_m=2500.0)
    assert grid.support.any() and not grid.support.all()
    assert grid.n_supported > 10  # room for several chunks
    return grid


def test_chunked_prediction_is_bit_identical_to_one_big_call(linear_cloud):
    # The reason chunking was chosen over pykrige's own memory knobs: each cell's
    # kriging system is independent, so batching them differently is the same
    # arithmetic. n_closest_points would also bound memory but by switching to a
    # moving neighbourhood, which moves the surface. Nothing here may move.
    model = OrdinaryKrigingModel(nlags=10).fit(linear_cloud)
    ok = model._require_fit()
    grid = _mixed_support_grid(linear_cloud)
    whole_m, whole_v = model._predict_supported(ok, grid, chunk=grid.n_supported)
    for chunk in (3, 7, 13, grid.n_supported - 1):
        m, v = model._predict_supported(ok, grid, chunk=chunk)
        assert np.array_equal(m, whole_m, equal_nan=True), f"mean moved at {chunk=}"
        assert np.array_equal(v, whole_v, equal_nan=True), f"variance moved at {chunk=}"


def test_chunks_of_one_or_two_agree_to_floating_point_noise(linear_cloud):
    """Documents the one place exactness is not on offer, and why it is moot.

    Batches of one or two cells make BLAS reach for a matrix-vector kernel
    instead of the blocked matrix-matrix one, which sums the same products in a
    different order; the answer moves by ~1e-11 dBm, far under the ~1 dBm the
    instrument resolves. ``_MIN_PREDICT_CHUNK_CELLS`` keeps production well
    clear of that regime, so the surfaces callers actually get are exact.
    """
    model = OrdinaryKrigingModel(nlags=10).fit(linear_cloud)
    ok = model._require_fit()
    grid = _mixed_support_grid(linear_cloud, cell_m=4000.0)
    whole_m, whole_v = model._predict_supported(ok, grid, chunk=grid.n_supported)
    for chunk in (1, 2):
        m, v = model._predict_supported(ok, grid, chunk=chunk)
        assert np.nanmax(np.abs(m - whole_m)) < 1e-6
        assert np.nanmax(np.abs(v - whole_v)) < 1e-6


def test_chunk_bigger_than_the_supported_count_is_a_single_call(linear_cloud):
    model = OrdinaryKrigingModel(nlags=10).fit(linear_cloud)
    ok = model._require_fit()
    grid = _mixed_support_grid(linear_cloud)
    exact = model._predict_supported(ok, grid, chunk=grid.n_supported)
    for chunk in (grid.n_supported + 1, 10 * grid.n_supported, 10**9):
        m, v = model._predict_supported(ok, grid, chunk=chunk)
        assert np.array_equal(m, exact[0], equal_nan=True)
        assert np.array_equal(v, exact[1], equal_nan=True)
    # The default path is one call too at this size, and must agree.
    surface = model.predict_grid(grid)
    assert np.array_equal(surface.mean, exact[0], equal_nan=True)
    assert np.array_equal(surface.variance, exact[1], equal_nan=True)


def test_empty_support_never_reaches_pykrige_however_it_is_chunked(linear_cloud):
    # pykrige raises on an empty point list, so the chunk loop must simply not
    # run rather than clamping to one empty batch.
    model = OrdinaryKrigingModel(nlags=10).fit(linear_cloud)
    ok = model._require_fit()
    grid = make_grid(linear_cloud, cell_m=1000.0, support_radius_m=0.0)
    assert grid.n_supported == 0
    for chunk in (None, 1, 10**9):
        kwargs = {} if chunk is None else {"chunk": chunk}
        mean, var = model._predict_supported(ok, grid, **kwargs)
        assert np.all(np.isnan(mean))
        assert np.all(np.isnan(var))


def test_a_single_supported_cell_still_predicts(linear_cloud):
    # The other edge of the loop: one cell, so the batch is shorter than any
    # chunk. pykrige squeezes length-1 inputs, which is where this could break.
    from mukoo_model.kriging import Grid

    model = OrdinaryKrigingModel(nlags=10).fit(linear_cloud)
    base = make_grid(linear_cloud, cell_m=2000.0)
    support = np.zeros(base.shape, dtype=bool)
    support[1, 2] = True
    grid = Grid(
        x=base.x,
        y=base.y,
        cell_m=base.cell_m,
        crs_epsg=base.crs_epsg,
        support=support,
    )
    surface = model.predict_grid(grid)
    assert int(np.isfinite(surface.mean).sum()) == 1
    assert int(np.isfinite(surface.variance).sum()) == 1
    expected, expected_var = model.predict_points(
        np.array([base.x[2]]), np.array([base.y[1]])
    )
    assert surface.mean[1, 2] == expected[0]
    assert surface.variance[1, 2] == expected_var[0]


def test_chunk_shrinks_as_the_cloud_grows(linear_cloud):
    # Memory is (cells x data points), so a budget counted in cells alone would
    # still creep upward as readings accumulate — which is the bug being fixed.
    from mukoo_model.kriging import (
        _MIN_PREDICT_CHUNK_CELLS,
        _VECTORIZED_WORKING_COPIES,
        PREDICT_CHUNK_BYTES,
        _predict_chunk_cells,
    )

    assert _predict_chunk_cells(3130) < _predict_chunk_cells(430)
    for n_data in (10, 430, 900, 3130, 50_000):
        cells = _predict_chunk_cells(n_data)
        assert cells >= _MIN_PREDICT_CHUNK_CELLS
        working = cells * (n_data + 1) * 8 * _VECTORIZED_WORKING_COPIES
        assert working <= PREDICT_CHUNK_BYTES or cells == _MIN_PREDICT_CHUNK_CELLS


def test_rejects_a_chunk_below_one(linear_cloud):
    # range(0, n, 0) raises deep inside the loop; fail on the argument instead.
    import pytest

    model = OrdinaryKrigingModel(nlags=10).fit(linear_cloud)
    grid = _mixed_support_grid(linear_cloud)
    with pytest.raises(ValueError, match="chunk"):
        model._predict_supported(model._require_fit(), grid, chunk=0)


def test_unmasked_grids_still_take_pykriges_own_grid_path(linear_cloud, monkeypatch):
    # Back-compat: chunking is for masked grids only, and a caller that never
    # asked for masking must reach pykrige's "grid" execution as it always did.
    model = OrdinaryKrigingModel(nlags=10).fit(linear_cloud)

    def _boom(*args, **kwargs):
        raise AssertionError("unmasked grid must not go through _predict_supported")

    monkeypatch.setattr(model, "_predict_supported", _boom)
    surface = model.predict_grid(make_grid(linear_cloud, cell_m=2000.0))
    assert np.all(np.isfinite(surface.mean))


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
