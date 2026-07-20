"""Tests for the active-learning suggester, on a synthetic surface + roads.

No database, no OSM: we build a Grid, a stddev array with known hot spots, and a
couple of Shapely roads, then assert the suggester lands drivable, spreads out,
and drops unreachable hot spots.
"""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import LineString

from mukoo_model.kriging import Grid
from mukoo_model.roads import RoadNetwork
from mukoo_model.suggest import (
    Suggestion,
    suggest_targets,
    suggestions_to_geojson,
    write_suggestions_geojson,
)

CELL = 500.0


def _grid(n=21):
    ax = np.arange(0, n * CELL, CELL)
    return Grid(x=ax.copy(), y=ax.copy(), crs_epsg=32617, cell_m=CELL)


def _roads():
    # A horizontal road across the middle (y=5000) and a vertical one at x=8000.
    return RoadNetwork(
        lines=[
            LineString([(0, 5000), (10000, 5000)]),
            LineString([(8000, 0), (8000, 10000)]),
        ],
        names=["Main St", "Second Ave"],
        crs_epsg=32617,
    )


def _surface_with_hotspots(grid):
    # Baseline uncertainty 5.0; a reachable hot spot on Main St at (2000, 5000)
    # and an unreachable one out in a field at (5000, 2000).
    sig = np.full(grid.shape, 5.0)
    sig[10, 4] = 9.0  # x=2000, y=5000  -> on Main St
    sig[4, 10] = 9.5  # x=5000, y=2000  -> ~3 km from any road
    return sig


def test_top_suggestion_is_the_reachable_hotspot():
    grid = _grid()
    sig = _surface_with_hotspots(grid)
    out = suggest_targets(
        sig, grid, _roads(), top_n=5, max_road_dist_m=400.0, min_separation_m=1000.0
    )
    top = out[0]
    assert np.isclose(top.x, 2000.0)
    assert np.isclose(top.y, 5000.0)
    assert np.isclose(top.stddev, 9.0)
    assert top.road_name == "Main St"
    assert top.road_dist_m <= 400.0


def test_unreachable_hotspot_is_excluded():
    grid = _grid()
    sig = _surface_with_hotspots(grid)
    out = suggest_targets(
        sig, grid, _roads(), top_n=20, max_road_dist_m=400.0, min_separation_m=600.0
    )
    # The field hot spot's value (9.5) must never surface, and nothing should
    # land near (5000, 2000).
    assert all(not np.isclose(s.stddev, 9.5) for s in out)
    assert all(np.hypot(s.x - 5000.0, s.y - 2000.0) > 700.0 for s in out)


def test_all_suggestions_are_drivable():
    grid = _grid()
    sig = _surface_with_hotspots(grid)
    out = suggest_targets(sig, grid, _roads(), top_n=10, max_road_dist_m=300.0)
    assert out
    assert all(s.road_dist_m <= 300.0 for s in out)


def test_min_separation_enforced():
    grid = _grid()
    sig = _surface_with_hotspots(grid)
    sep = 1500.0
    out = suggest_targets(
        sig, grid, _roads(), top_n=10, max_road_dist_m=400.0, min_separation_m=sep
    )
    for i in range(len(out)):
        for j in range(i + 1, len(out)):
            d = np.hypot(out[i].x - out[j].x, out[i].y - out[j].y)
            assert d >= sep - 1.0


def test_top_n_capped_and_ranked():
    grid = _grid()
    sig = _surface_with_hotspots(grid)
    out = suggest_targets(
        sig, grid, _roads(), top_n=3, max_road_dist_m=400.0, min_separation_m=600.0
    )
    assert len(out) <= 3
    assert [s.rank for s in out] == list(range(1, len(out) + 1))
    # Ranked by descending uncertainty.
    assert all(out[i].stddev >= out[i + 1].stddev for i in range(len(out) - 1))


def test_empty_road_network_yields_no_suggestions():
    grid = _grid()
    sig = _surface_with_hotspots(grid)
    empty = RoadNetwork(lines=[], names=[], crs_epsg=32617)
    assert suggest_targets(sig, grid, empty) == []


def test_crs_mismatch_raises():
    grid = _grid()
    sig = _surface_with_hotspots(grid)
    wrong = RoadNetwork(
        lines=[LineString([(0, 5000), (10000, 5000)])],
        names=["Main St"],
        crs_epsg=4326,
    )
    with pytest.raises(ValueError):
        suggest_targets(sig, grid, wrong)


def test_nan_cells_ignored():
    grid = _grid()
    sig = _surface_with_hotspots(grid)
    sig[0, 0] = np.nan  # a nodata corner must not break quantile/selection
    out = suggest_targets(sig, grid, _roads(), top_n=5, max_road_dist_m=400.0)
    assert out
    assert all(np.isfinite(s.stddev) for s in out)


def test_geojson_structure_and_write(tmp_path):
    grid = _grid()
    sig = _surface_with_hotspots(grid)
    out = suggest_targets(sig, grid, _roads(), top_n=5, max_road_dist_m=400.0)
    fc = suggestions_to_geojson(out, metric="rsrp")
    assert fc["type"] == "FeatureCollection"
    assert fc["properties"]["count"] == len(out)
    f0 = fc["features"][0]
    assert f0["geometry"]["type"] == "Point"
    assert f0["properties"]["rank"] == 1
    assert f0["properties"]["stddev_unit"] == "dBm"
    # coordinates are lon/lat, not the projected metres
    lon, lat = f0["geometry"]["coordinates"]
    assert -180 <= lon <= 180 and -90 <= lat <= 90

    path = write_suggestions_geojson(tmp_path / "s.geojson", out, metric="rsrp")
    assert path.exists()
