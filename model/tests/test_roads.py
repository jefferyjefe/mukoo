"""Tests for the pure-Shapely RoadNetwork (no osmnx, no network).

The tiling tests stub :func:`mukoo_model.roads.fetch_roads`, so they assert on
*which boxes would be fetched* without ever asking OSM for one; the one test
that covers :func:`fetch_roads` itself stubs the ``osmnx`` module. Nothing here
imports osmnx or touches the network.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import LineString

from mukoo_model import roads as roads_mod
from mukoo_model.roads import (
    DEFAULT_TILE_DEG,
    RoadNetwork,
    _EMPTY_TILE_MESSAGES,
    _normalize_name,
    fetch_roads,
    fetch_roads_tiles,
    road_tiles,
)


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


def _tile_area_km2(bounds) -> float:
    lon_min, lat_min, lon_max, lat_max = bounds
    mid = np.radians((lat_min + lat_max) / 2.0)
    return (lon_max - lon_min) * 111.32 * np.cos(mid) * (lat_max - lat_min) * 111.32


def test_road_tiles_snap_to_a_fixed_lattice():
    # Stable cache keys are the point: two nearby points on different runs must
    # ask for the same box, or every run re-downloads the same ground.
    a = road_tiles([-81.7312], [32.4187])
    b = road_tiles([-81.7290], [32.4201])
    assert a == b
    lon_min, lat_min, lon_max, lat_max = a[0]
    assert np.isclose(lon_max - lon_min, DEFAULT_TILE_DEG)
    assert np.isclose(lat_max - lat_min, DEFAULT_TILE_DEG)
    assert lon_min <= -81.7312 <= lon_max and lat_min <= 32.4187 <= lat_max


def test_road_tiles_follow_a_corridor_not_its_bounding_box():
    # A 150 km drive: the bounding box is enormous, the corridor is not. The
    # tiles must cost the corridor, which is the whole reason they exist.
    lons = np.linspace(-81.8, -80.1, 800)
    lats = np.linspace(32.10, 32.75, 800)
    tiles = road_tiles(lons, lats, buffer_m=250.0, max_tiles=None)
    fetched = sum(_tile_area_km2(t) for t in tiles)
    bbox = _tile_area_km2((-81.8, 32.10, -80.1, 32.75))
    assert fetched < 0.15 * bbox
    assert all(np.isclose(t[2] - t[0], DEFAULT_TILE_DEG) for t in tiles)


def test_road_tiles_buffer_reaches_across_the_tile_edge():
    # A candidate 100 m inside a tile edge can still have its nearest road just
    # over the line, so the neighbouring tile has to come too.
    edge_lon = -81.75 + 1e-6  # a hair east of a lattice boundary
    assert len(road_tiles([edge_lon], [32.42], buffer_m=0.0)) == 1
    assert len(road_tiles([edge_lon], [32.42], buffer_m=250.0)) == 2


def test_road_tiles_claim_the_tile_under_the_point_not_only_the_corners():
    # Bucketing only the buffer box's four corners leaves a hole in the middle
    # the moment the buffer approaches a tile: the tile the candidate is
    # standing in — the one certain to contain its nearest road — goes
    # unclaimed, so that road is never fetched and the candidate is dropped as
    # unreachable. max_road_dist_m and tile_deg are both free parameters, so
    # nothing but the shipped defaults keeps this rare.
    lon, lat = -81.7312, 32.4187
    own_tile = road_tiles([lon], [lat], buffer_m=0.0)[0]
    tiles = road_tiles([lon], [lat], buffer_m=8000.0, max_tiles=None)
    assert own_tile in tiles
    assert len(tiles) > 4  # a solid block of tiles, not four corners


def test_road_tiles_cover_every_point_of_the_buffer_box():
    # The invariant stated by the docstring, checked by sampling: no point of
    # the buffered box may fall outside every claimed tile.
    lon, lat, buffer_m = -81.7312, 32.4187, 8000.0
    tiles = road_tiles([lon], [lat], buffer_m=buffer_m, max_tiles=None)
    dlat = buffer_m / 111_320.0
    dlon = buffer_m / (111_320.0 * np.cos(np.radians(lat)))
    for plon in np.linspace(lon - dlon, lon + dlon, 25):
        for plat in np.linspace(lat - dlat, lat + dlat, 25):
            assert any(
                t[0] <= plon <= t[2] and t[1] <= plat <= t[3] for t in tiles
            ), f"({plon}, {plat}) is inside the buffer but in no claimed tile"


def test_road_tiles_budget_keeps_the_earliest_points():
    # Callers pass their points most-uncertain-first, so truncation must drop
    # the tail rather than an arbitrary slice.
    lons = np.arange(-82.0, -81.0, DEFAULT_TILE_DEG)
    lats = np.full(lons.shape, 32.4)
    tiles = road_tiles(lons, lats, max_tiles=4)
    assert len(tiles) == 4
    assert tiles == road_tiles(lons, lats, max_tiles=None)[:4]


def test_road_tiles_empty_input_fetches_nothing():
    assert road_tiles([], []) == []


def test_fetch_roads_tiles_unions_and_deduplicates(monkeypatch, tmp_path):
    # Ways straddling a tile edge come back from both sides; the union must
    # count them once, so len(roads) still means "distinct edges".
    shared = LineString([(0, 0), (100, 0)])
    calls = []

    def fake_fetch(bounds, crs_epsg, *, cache_dir, network_type="drive", refresh=False):
        calls.append(bounds)
        own = LineString([(0, len(calls) * 10.0), (100, len(calls) * 10.0)])
        return RoadNetwork(lines=[shared, own], names=["Shared", "Own"], crs_epsg=crs_epsg)

    monkeypatch.setattr(roads_mod, "fetch_roads", fake_fetch)
    tiles = [(-81.8, 32.4, -81.75, 32.45), (-81.75, 32.4, -81.7, 32.45)]
    network = fetch_roads_tiles(tiles, 32617, cache_dir=tmp_path)
    assert calls == tiles
    assert len(network) == 3  # one shared edge, plus one of each tile's own


# The three shapes osmnx 2.x uses for "this box has no drivable network". Only
# the first is a distinguishable type; the other two are bare ValueErrors raised
# by the two truncation passes graph_from_polygon makes over a 500 m-buffered
# download, and a rural tile beside a corridor — the ordinary case here — hits
# them. A fake merely *named* InsufficientResponseError proves nothing about
# them, which is how they got through the first time.
_INSUFFICIENT_RESPONSE = type("InsufficientResponseError", (ValueError,), {})
EMPTY_TILE_ERRORS = [
    _INSUFFICIENT_RESPONSE("No data elements in server response."),
    ValueError("Found no graph nodes within the requested polygon."),
    ValueError("Graph contains no edges."),
]


@pytest.mark.parametrize(
    "exc", EMPTY_TILE_ERRORS, ids=lambda e: type(e).__name__ + ": " + str(e)[:24]
)
def test_fetch_roads_tiles_caches_tiles_with_no_roads(exc, monkeypatch, tmp_path):
    # A rural tile with no drivable way is a normal answer, not a failure — it
    # must not kill the run, and it has to be remembered, or every run asks
    # Overpass again.
    def fake_fetch(bounds, crs_epsg, *, cache_dir, network_type="drive", refresh=False):
        if bounds[0] < -81.75:
            raise exc
        return RoadNetwork(
            lines=[LineString([(0, 0), (100, 0)])], names=["Main"], crs_epsg=crs_epsg
        )

    monkeypatch.setattr(roads_mod, "fetch_roads", fake_fetch)
    tiles = [(-81.8, 32.4, -81.75, 32.45), (-81.75, 32.4, -81.7, 32.45)]
    network = fetch_roads_tiles(tiles, 32617, cache_dir=tmp_path)
    assert len(network) == 1
    empty = tmp_path / "osm_roads_-81.8000_32.4000_-81.7500_32.4500.geojson"
    assert json.loads(empty.read_text())["features"] == []


def test_fetch_roads_tiles_survives_an_all_empty_run(monkeypatch, tmp_path):
    # Every tile empty is still not a crash: an empty network is a result the
    # suggester already knows how to report.
    def fake_fetch(bounds, crs_epsg, **kwargs):
        raise ValueError("Found no graph nodes within the requested polygon.")

    monkeypatch.setattr(roads_mod, "fetch_roads", fake_fetch)
    tiles = [(-81.8, 32.4, -81.75, 32.45), (-81.75, 32.4, -81.7, 32.45)]
    assert fetch_roads_tiles(tiles, 32617, cache_dir=tmp_path).is_empty


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("overpass is down"),
        ValueError("Graph contains no edges, probably"),  # near-miss, not the signal
        # A real bug in the graph pipeline, not an empty box.
        type("GraphSimplificationError", (ValueError,), {})("Impossible pattern"),
    ],
)
def test_fetch_roads_tiles_does_not_swallow_real_failures(exc, monkeypatch, tmp_path):
    def fake_fetch(bounds, crs_epsg, **kwargs):
        raise exc

    monkeypatch.setattr(roads_mod, "fetch_roads", fake_fetch)
    with pytest.raises(ValueError):
        fetch_roads_tiles([(-81.8, 32.4, -81.75, 32.45)], 32617, cache_dir=tmp_path)
    # A failed tile must not be cached as "empty", or the next run inherits the
    # hole without ever seeing the error.
    assert list(tmp_path.glob("osm_roads_*.geojson")) == []


def test_empty_tile_messages_match_the_installed_osmnx():
    # These strings are the whole contract: osmnx raises bare ValueErrors that
    # carry nothing else to match on, so a reworded message silently returns the
    # suggester to dying on rural tiles. Read as source text rather than
    # imported, because the test suite stays osmnx-free and network-free.
    spec = importlib.util.find_spec("osmnx")
    if spec is None or not spec.submodule_search_locations:  # pragma: no cover
        pytest.skip("osmnx is not installed")
    pkg = Path(list(spec.submodule_search_locations)[0])
    source = (pkg / "truncate.py").read_text() + (pkg / "convert.py").read_text()
    for message in _EMPTY_TILE_MESSAGES:
        assert f'"{message}"' in source, f"osmnx no longer says {message!r}"
    assert "class InsufficientResponseError(ValueError)" in (
        pkg / "_errors.py"
    ).read_text()


class _FakeEdges:
    """The bit of a GeoDataFrame that :func:`fetch_roads` actually touches."""

    def __init__(self, lines, names):
        self.geometry = lines
        self._names = names

    def __len__(self):
        return len(self.geometry)

    def get(self, key, default=None):
        return self._names if key == "name" else default


def test_fetch_roads_asks_osmnx_to_keep_seam_crossing_edges(monkeypatch, tmp_path):
    # With osmnx's defaults an edge crossing the box boundary is dropped by this
    # box *and* by its neighbour, and only the largest component of each box
    # survives. Over 26 km² tiles that erases whole rural stretches of the
    # corridor road at every seam. There is no way to observe this without a
    # live Overpass, so the arguments themselves are the assertion.
    captured = {}

    def graph_from_polygon(polygon, **kwargs):
        captured.update(kwargs)
        return "graph"

    def graph_to_gdfs(graph, nodes=False, edges=True):
        return _FakeEdges([LineString([(-81.78, 32.42), (-81.76, 32.43)])], ["Main St"])

    monkeypatch.setitem(
        sys.modules,
        "osmnx",
        type(
            "_FakeOsmnx",
            (),
            {
                "graph_from_polygon": staticmethod(graph_from_polygon),
                "graph_to_gdfs": staticmethod(graph_to_gdfs),
            },
        ),
    )
    network = fetch_roads((-81.8, 32.4, -81.75, 32.45), 32617, cache_dir=tmp_path)

    assert captured["truncate_by_edge"] is True
    assert captured["retain_all"] is True
    assert len(network) == 1 and network.names == ["Main St"]
