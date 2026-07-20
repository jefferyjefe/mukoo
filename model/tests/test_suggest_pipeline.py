"""Tests for pipeline helpers that don't touch the DB or OSM."""

from __future__ import annotations

import numpy as np

from mukoo_model.kriging import Grid
from mukoo_model.suggest_pipeline import bounds_lonlat_of_grid, format_suggestions
from mukoo_model.suggest import Suggestion


def test_bounds_lonlat_covers_grid_extent():
    # A UTM 17N grid near Statesboro, GA -> a lon/lat box in the right ballpark.
    ax = np.arange(430000.0, 445000.0, 150.0)
    ay = np.arange(3585000.0, 3595000.0, 150.0)
    grid = Grid(x=ax, y=ay, crs_epsg=32617, cell_m=150.0)
    lon_min, lat_min, lon_max, lat_max = bounds_lonlat_of_grid(grid)
    assert lon_min < lon_max and lat_min < lat_max
    assert -82.5 < lon_min < -81.0
    assert 32.0 < lat_min < 33.0


def test_format_suggestions_empty():
    assert "No drivable" in format_suggestions([], metric="rsrp")


def test_format_suggestions_table():
    s = Suggestion(
        rank=1,
        lon=-81.74,
        lat=32.40,
        x=430000.0,
        y=3585000.0,
        stddev=8.1,
        road_name="River Rd",
        road_dist_m=42.0,
    )
    text = format_suggestions([s], metric="rsrp")
    assert "River Rd" in text
    assert "32.40" in text
