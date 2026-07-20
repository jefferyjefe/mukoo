"""Tests for coordinate projection and duplicate collapsing."""

from __future__ import annotations

import numpy as np

from mukoo_model.data import _collapse_coincident, utm_epsg_for


def test_utm_epsg_georgia_is_17n():
    # Statesboro, GA sits in UTM zone 17, northern hemisphere -> 32617.
    assert utm_epsg_for(-81.74, 32.40) == 32617


def test_utm_epsg_southern_hemisphere():
    # Same zone longitude but south of the equator -> 327xx.
    assert utm_epsg_for(-60.0, -33.0) == 32721


def test_collapse_coincident_averages_duplicate_locations():
    # Two points at (0,0) with values 10 and 20 must become one point at 15.
    x = np.array([0.0, 0.0, 100.0])
    y = np.array([0.0, 0.0, 100.0])
    v = np.array([10.0, 20.0, 5.0])
    cx, cy, cv = _collapse_coincident(x, y, v, tol_m=1.0)
    assert cx.shape == (2,)
    # The collapsed group's value is the mean of the coincident pair.
    order = np.argsort(cx)
    assert np.allclose(np.sort(cv), sorted([15.0, 5.0]))
    assert 15.0 in np.round(cv, 6)


def test_collapse_coincident_noop_when_all_distinct():
    x = np.array([0.0, 100.0, 200.0])
    y = np.array([0.0, 100.0, 200.0])
    v = np.array([1.0, 2.0, 3.0])
    cx, cy, cv = _collapse_coincident(x, y, v)
    assert cx.shape == (3,)
    assert np.array_equal(cv, v)


def test_collapse_snaps_within_tolerance():
    # Sub-metre GPS jitter should be treated as the same location.
    x = np.array([0.0, 0.3])
    y = np.array([0.0, 0.2])
    v = np.array([10.0, 30.0])
    cx, cy, cv = _collapse_coincident(x, y, v, tol_m=1.0)
    assert cx.shape == (1,)
    assert np.isclose(cv[0], 20.0)
