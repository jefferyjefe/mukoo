"""Tests for session and spatial-block CV — the honest schemes."""

from __future__ import annotations

import numpy as np
import pytest
from conftest import make_linear_cloud

from mukoo_model.crossval import _folds_from_labels, block_cv, kfold_cv, session_cv
from mukoo_model.kriging import OrdinaryKrigingModel


def _factory():
    return OrdinaryKrigingModel(nlags=10)


def test_folds_from_labels_partition():
    labels = np.array(["a", "b", "a", "c", "b"], dtype=object)
    folds = _folds_from_labels(labels)
    assert len(folds) == 3
    allidx = np.sort(np.concatenate(folds))
    assert np.array_equal(allidx, np.arange(5))
    # each fold is label-pure
    for fold in folds:
        assert len(set(labels[fold])) == 1


def test_session_cv_runs_and_labels_scheme():
    cloud = make_linear_cloud(n=250, seed=0, n_sessions=5)
    cv = session_cv(cloud, model_factory=_factory)
    assert "session" in cv.scheme
    assert cv.n_folds == 5
    assert cv.n == 250
    assert np.isfinite(cv.rmse)


def test_session_cv_requires_labels():
    cloud = make_linear_cloud(n=100, seed=0)  # no sessions
    with pytest.raises(ValueError):
        session_cv(cloud, model_factory=_factory)


def test_block_cv_runs_and_holds_out_tiles():
    cloud = make_linear_cloud(n=300, seed=1)
    cv = block_cv(cloud, model_factory=_factory, block_m=2500.0, n_folds=5, seed=0)
    assert "block" in cv.scheme
    assert cv.n == 300
    assert np.isfinite(cv.rmse)


def test_block_cv_rejects_single_block():
    cloud = make_linear_cloud(n=100, seed=0)
    with pytest.raises(ValueError):
        block_cv(cloud, model_factory=_factory, block_m=1e6)


def test_spatial_schemes_not_easier_than_random():
    # On a spatially structured field, holding out whole areas/sessions cannot
    # be systematically EASIER than holding out random points; allow slack for
    # noise but catch an implementation that leaks neighbours into training.
    cloud = make_linear_cloud(n=400, noise=0.5, seed=2, n_sessions=5)
    random_cv = kfold_cv(cloud, model_factory=_factory, n_folds=10, seed=0)
    sess_cv = session_cv(cloud, model_factory=_factory)
    blk_cv = block_cv(cloud, model_factory=_factory, block_m=2500.0, seed=0)
    assert sess_cv.rmse > random_cv.rmse * 0.8
    assert blk_cv.rmse > random_cv.rmse * 0.8
