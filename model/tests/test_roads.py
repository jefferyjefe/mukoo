"""Tests for the pure-Shapely RoadNetwork (no osmnx, no network)."""

from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import LineString

from mukoo_model.roads import RoadNetwork, _normalize_name


def _horizontal_road(y=0.0, x0=0.0, x1=100.0, name="Main St"):
    return RoadNetwork(
        lines=[LineString([(x0, y), (x1, y)])], names=[name], crs_epsg=32617
    )


def test_nearest_snaps_perpendicular_to_road():
    roads = _horizontal_road(y=0.0)
    near = roads.nearest(50.0, 10.0)
    assert near.index == 0
    assert np.isclose(near.distance_m, 10.0)
    assert np.isclose(near.point_x, 50.0)
    assert np.isclose(near.point_y, 0.0)
    assert near.name == "Main St"


def test_nearest_picks_the_closer_of_two_roads():
    roads = RoadNetwork(
        lines=[
            LineString([(0, 0), (100, 0)]),  # y=0
            LineString([(0, 100), (100, 100)]),  # y=100
        ],
        names=["South Rd", "North Rd"],
        crs_epsg=32617,
    )
    near = roads.nearest(50.0, 90.0)
    assert near.name == "North Rd"
    assert np.isclose(near.distance_m, 10.0)


def test_empty_network_reports_empty_and_raises_on_nearest():
    roads = RoadNetwork(lines=[], names=[], crs_epsg=32617)
    assert roads.is_empty
    assert len(roads) == 0
    with pytest.raises(ValueError):
        roads.nearest(0.0, 0.0)


def test_length_mismatch_rejected():
    with pytest.raises(ValueError):
        RoadNetwork(lines=[LineString([(0, 0), (1, 1)])], names=[], crs_epsg=32617)


def test_normalize_name_handles_list_and_missing():
    assert _normalize_name("River Rd") == "River Rd"
    assert _normalize_name(["River Rd", "Old River Rd"]) == "River Rd"
    assert _normalize_name(None) is None
    assert _normalize_name([]) is None
    assert _normalize_name("  ") is None


def test_normalize_name_handles_nan():
    # Unnamed OSM ways arrive as a float NaN (from pandas), or a stringified
    # "nan" once round-tripped through the JSON cache. Both mean "unnamed".
    assert _normalize_name(float("nan")) is None
    assert _normalize_name("nan") is None
    assert _normalize_name([float("nan"), "Back Rd"]) == "Back Rd"
