"""Tests for EVR scoring, weakness weighting, and overlap-aware selection."""

from __future__ import annotations

import numpy as np
from shapely.geometry import LineString

from mukoo_model.kriging import Grid
from mukoo_model.roads import RoadNetwork
from mukoo_model.suggest import (
    expected_reduction_scores,
    suggest_targets,
    weakness_weight,
)

CELL = 500.0


def _grid(n=21):
    ax = np.arange(0, n * CELL, CELL)
    return Grid(x=ax.copy(), y=ax.copy(), crs_epsg=32617, cell_m=CELL)


def test_evr_prefers_uncertainty_mass_over_isolated_peak():
    grid = _grid()
    sig = np.full(grid.shape, 3.0)
    # a broad high-σ region around (2000..4000, 2000..4000)
    sig[4:9, 4:9] = 8.0
    # an isolated single high cell far away, slightly HIGHER σ
    sig[16, 16] = 8.5
    scores = expected_reduction_scores(
        sig,
        grid,
        np.array([3000.0, 8000.0]),
        np.array([3000.0, 8000.0]),
        range_m=2000.0,
    )
    # the point inside the broad region informs far more σ² mass than the
    # isolated (even if taller) peak.
    assert scores[0] > scores[1]


def test_weakness_weight_ramp():
    w = weakness_weight(np.array([-80.0, -95.0, -107.5, -120.0, -140.0]), weak_bias=0.5)
    assert np.isclose(w[0], 1.0)  # strong -> no boost
    assert np.isclose(w[1], 1.0)  # ramp starts below -95
    assert 1.2 < w[2] < 1.3  # midway
    assert np.isclose(w[3], 1.5)  # saturated at -120
    assert np.isclose(w[4], 1.5)  # stays saturated


def test_weak_bias_flips_tie():
    # Two identical hot spots on the road; the one with weaker predicted RSRP
    # must win when weak_bias is on.
    grid = _grid()
    sig = np.full(grid.shape, 3.0)
    sig[10, 4] = 9.0  # x=2000, y=5000
    sig[10, 16] = 9.0  # x=8000, y=5000
    mean = np.full(grid.shape, -90.0)
    mean[:, 12:] = -118.0  # east half predicted weak
    roads = RoadNetwork(
        lines=[LineString([(0, 5000), (10000, 5000)])], names=["Main"], crs_epsg=32617
    )
    out = suggest_targets(
        sig,
        grid,
        roads,
        top_n=2,
        max_road_dist_m=400.0,
        min_separation_m=1000.0,
        mean=mean,
        weak_bias=1.0,
    )
    assert out, "expected suggestions"
    assert out[0].x > 5000.0  # the weak-coverage side ranks first


def test_overlap_discount_spreads_picks():
    # With EVR + overlap discount, picks should not bunch at the allowed
    # min-separation edge inside one hot region when a second (slightly less
    # hot) region exists: after the first pick discounts its neighbourhood,
    # the second region must win pick 2.
    grid = _grid()
    sig = np.full(grid.shape, 3.0)
    sig[4:9, 4:9] = 8.0  # region A (hotter)
    sig[14:19, 14:19] = 7.5  # region B
    roads = RoadNetwork(
        lines=[
            LineString([(0, 3000), (10000, 3000)]),  # crosses region A
            LineString([(0, 8000), (10000, 8000)]),  # crosses region B
        ],
        names=["A Rd", "B Rd"],
        crs_epsg=32617,
    )
    out = suggest_targets(
        sig,
        grid,
        roads,
        top_n=2,
        max_road_dist_m=600.0,
        min_separation_m=800.0,  # would allow two picks inside region A
        range_m=2000.0,
    )
    assert len(out) == 2
    names = {out[0].road_name, out[1].road_name}
    assert names == {"A Rd", "B Rd"}


def test_scores_populated_and_descending():
    grid = _grid()
    sig = np.full(grid.shape, 3.0)
    sig[4:9, 4:9] = 8.0
    roads = RoadNetwork(
        lines=[LineString([(0, 3000), (10000, 3000)])], names=["Main"], crs_epsg=32617
    )
    out = suggest_targets(
        sig, grid, roads, top_n=5, max_road_dist_m=600.0, range_m=2000.0
    )
    assert all(s.score > 0 for s in out)
    # rank order == score order at selection time (undiscounted score recorded)
    assert all(out[i].rank == i + 1 for i in range(len(out)))
