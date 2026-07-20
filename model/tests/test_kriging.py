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
