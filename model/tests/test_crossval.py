"""Tests for k-fold cross-validation metrics and calibration."""

from __future__ import annotations

import numpy as np
import pytest

from conftest import make_linear_cloud

from mukoo_model.crossval import _fold_indices, kfold_cv
from mukoo_model.kriging import OrdinaryKrigingModel


def _factory():
    return OrdinaryKrigingModel(nlags=10)


def test_fold_indices_partition_is_complete_and_disjoint():
    folds = _fold_indices(100, 10, seed=0)
    assert len(folds) == 10
    allidx = np.concatenate(folds)
    # Every index appears exactly once across all folds.
    assert np.array_equal(np.sort(allidx), np.arange(100))


def test_fold_indices_is_deterministic_under_seed():
    a = _fold_indices(50, 5, seed=42)
    b = _fold_indices(50, 5, seed=42)
    for fa, fb in zip(a, b):
        assert np.array_equal(fa, fb)


def test_fold_indices_rejects_bad_k():
    with pytest.raises(ValueError):
        _fold_indices(10, 1, seed=0)
    with pytest.raises(ValueError):
        _fold_indices(5, 6, seed=0)


def test_cv_on_smooth_field_has_high_skill():
    # A near-planar field is easy to interpolate: skill (R^2) should be high,
    # RMSE close to the noise floor, and bias near zero.
    cloud = make_linear_cloud(n=300, noise=0.5, seed=0)
    cv = kfold_cv(cloud, model_factory=_factory, n_folds=10, seed=0)
    assert cv.n == 300
    assert cv.rmse < 1.5  # noise sd is 0.5; interpolation error stays small
    assert cv.r2 > 0.7
    assert abs(cv.bias) < 0.5
    assert cv.r > 0.85


def test_cv_calibration_within_bounds():
    # For a correctly-specified field the kriging sigma should be roughly honest:
    # ~68% of held-out points inside 1 sigma, standardized-residual std near 1.
    cloud = make_linear_cloud(n=400, noise=0.5, seed=3)
    cv = kfold_cv(cloud, model_factory=_factory, n_folds=10, seed=0)
    assert 0.45 < cv.within_1sigma < 0.9
    assert 0.5 < cv.std_resid_std < 2.0


def test_cv_on_pure_noise_has_no_skill():
    # A spatially structureless field cannot be interpolated: R^2 should be low
    # (kriging does no better than predicting the mean).
    rng = np.random.RandomState(7)
    n = 250
    x = rng.rand(n) * 10000.0
    y = rng.rand(n) * 10000.0
    z = -100.0 + rng.randn(n) * 5.0  # no spatial structure at all
    from mukoo_model.data import PointCloud

    cloud = PointCloud(
        lon=x / 1e5 - 81.8,
        lat=y / 1e5 + 32.35,
        x=x,
        y=y,
        values=z,
        crs_epsg=32617,
        metric="rsrp",
        n_raw=n,
    )
    cv = kfold_cv(cloud, model_factory=_factory, n_folds=10, seed=0)
    assert cv.r2 < 0.2  # essentially no interpolation skill


def test_cv_summary_renders():
    cloud = make_linear_cloud(n=150, seed=1)
    cv = kfold_cv(cloud, model_factory=_factory, n_folds=5, seed=0)
    text = cv.summary()
    assert "RMSE" in text
    assert "CALIBRATION" in text
    d = cv.to_dict()
    assert d["accuracy"]["rmse"] == cv.rmse
    assert d["calibration"]["within_1sigma"] == cv.within_1sigma
