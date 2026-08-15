"""Tests for pipeline helpers that don't touch the DB or OSM.

``run_suggest`` is exercised with the GeoTIFF read and the OSM fetch stubbed
out, which is what lets us assert on the boxes it *would* ask Overpass for.
"""

from __future__ import annotations

import numpy as np
from shapely.geometry import LineString

from mukoo_model import roads as roads_mod
from mukoo_model import suggest_pipeline
from mukoo_model.config import Config
from mukoo_model.kriging import Grid
from mukoo_model.roads import DEFAULT_MAX_TILES, DEFAULT_TILE_DEG, RoadNetwork
from mukoo_model.suggest_pipeline import (
    SuggestResult,
    bounds_lonlat_of_grid,
    format_suggestions,
    run_suggest,
)
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


# A stretched survey: 90 x 75 km of grid, of which only a thin east-west
# corridor was ever predicted — the shape one 150 km drive leaves behind.
CORRIDOR_ROW = 150
CORRIDOR_Y = 3585000.0 + CORRIDOR_ROW * 150.0


def _stretched_surface():
    grid = Grid(
        x=np.arange(430000.0, 430000.0 + 600 * 150.0, 150.0),
        y=np.arange(3585000.0, 3585000.0 + 500 * 150.0, 150.0),
        cell_m=150.0,
        crs_epsg=32617,
    )
    stddev = np.full(grid.shape, np.nan)
    band = slice(CORRIDOR_ROW - 2, CORRIDOR_ROW + 3)
    stddev[band, :] = np.random.RandomState(0).uniform(4.0, 9.0, (5, grid.shape[1]))
    return stddev, grid


def _tile_area_km2(bounds) -> float:
    lon_min, lat_min, lon_max, lat_max = bounds
    mid = np.radians((lat_min + lat_max) / 2.0)
    return (lon_max - lon_min) * 111.32 * np.cos(mid) * (lat_max - lat_min) * 111.32


def test_run_suggest_fetches_roads_only_around_candidate_cells(monkeypatch, tmp_path):
    # The regression this whole change exists for: the fetch used to cover the
    # grid's bounding rectangle, which one long drive had inflated to ~12,000
    # km2 of mostly-unsupported countryside, and the refresh agent died on it.
    stddev, grid = _stretched_surface()
    fetched = []

    def fake_fetch(bounds, crs_epsg, *, cache_dir, network_type="drive", refresh=False):
        fetched.append(bounds)
        return RoadNetwork(
            lines=[LineString([(430000.0, CORRIDOR_Y), (520000.0, CORRIDOR_Y)])],
            names=["Corridor Rd"],
            crs_epsg=crs_epsg,
        )

    monkeypatch.setattr(roads_mod, "fetch_roads", fake_fetch)
    monkeypatch.setattr(
        suggest_pipeline, "load_grid_surface", lambda path: (stddev, grid)
    )
    stddev_tif = tmp_path / "rsrp_kriging_stddev.tif"
    stddev_tif.touch()

    result = run_suggest(Config(output_dir=tmp_path), stddev_tif=stddev_tif)

    assert fetched, "expected at least one road fetch"
    # No single request is ever bigger than one lattice tile, however big the
    # grid gets, and the budget caps how many of them a run may make.
    assert all(
        b[2] - b[0] <= DEFAULT_TILE_DEG + 1e-9 and b[3] - b[1] <= DEFAULT_TILE_DEG + 1e-9
        for b in fetched
    )
    assert len(fetched) == result.n_road_tiles <= DEFAULT_MAX_TILES
    # This run fitted inside the budget, so it must not claim otherwise.
    assert result.n_road_tiles_wanted == result.n_road_tiles
    # And the total ground asked for follows the corridor, not the rectangle.
    rectangle = _tile_area_km2(bounds_lonlat_of_grid(grid))
    assert sum(_tile_area_km2(b) for b in fetched) < 0.2 * rectangle
    # The one road came back from every tile; the union counts it once.
    assert result.n_roads == 1


def test_run_suggest_targets_stay_in_the_supported_corridor(monkeypatch, tmp_path):
    stddev, grid = _stretched_surface()

    def fake_fetch(bounds, crs_epsg, *, cache_dir, network_type="drive", refresh=False):
        return RoadNetwork(
            lines=[LineString([(430000.0, CORRIDOR_Y), (520000.0, CORRIDOR_Y)])],
            names=["Corridor Rd"],
            crs_epsg=crs_epsg,
        )

    monkeypatch.setattr(roads_mod, "fetch_roads", fake_fetch)
    monkeypatch.setattr(
        suggest_pipeline, "load_grid_surface", lambda path: (stddev, grid)
    )
    stddev_tif = tmp_path / "rsrp_kriging_stddev.tif"
    stddev_tif.touch()

    result = run_suggest(Config(output_dir=tmp_path), stddev_tif=stddev_tif)

    assert result.suggestions
    assert all(np.isfinite(s.stddev) for s in result.suggestions)
    assert all(np.isclose(s.y, CORRIDOR_Y) for s in result.suggestions)
    assert result.output_path.exists() and result.gpx_path.exists()


def test_run_suggest_with_no_finite_cells_fetches_nothing(monkeypatch, tmp_path):
    grid = Grid(
        x=np.arange(430000.0, 445000.0, 150.0),
        y=np.arange(3585000.0, 3595000.0, 150.0),
        cell_m=150.0,
        crs_epsg=32617,
    )
    stddev = np.full(grid.shape, np.nan)

    def fail_fetch(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("no candidates means no reason to ask OSM anything")

    monkeypatch.setattr(roads_mod, "fetch_roads", fail_fetch)
    monkeypatch.setattr(
        suggest_pipeline, "load_grid_surface", lambda path: (stddev, grid)
    )
    stddev_tif = tmp_path / "rsrp_kriging_stddev.tif"
    stddev_tif.touch()

    result = run_suggest(Config(output_dir=tmp_path), stddev_tif=stddev_tif)
    assert result.suggestions == []
    assert result.n_road_tiles == 0


def test_run_suggest_reports_when_the_tile_budget_truncated_the_fetch(
    monkeypatch, tmp_path
):
    # A capped run ranks its targets against part of the road network: the tiles
    # it never fetched hold roads that were never considered. That has to be
    # visible in the result, not inferred from a bare tile count.
    stddev, grid = _stretched_surface()
    fetched = []

    def fake_fetch(bounds, crs_epsg, *, cache_dir, network_type="drive", refresh=False):
        fetched.append(bounds)
        return RoadNetwork(
            lines=[LineString([(430000.0, CORRIDOR_Y), (520000.0, CORRIDOR_Y)])],
            names=["Corridor Rd"],
            crs_epsg=crs_epsg,
        )

    monkeypatch.setattr(roads_mod, "fetch_roads", fake_fetch)
    monkeypatch.setattr(
        suggest_pipeline, "load_grid_surface", lambda path: (stddev, grid)
    )
    monkeypatch.setattr(suggest_pipeline, "DEFAULT_MAX_TILES", 3)
    stddev_tif = tmp_path / "rsrp_kriging_stddev.tif"
    stddev_tif.touch()

    result = run_suggest(Config(output_dir=tmp_path), stddev_tif=stddev_tif)
    assert len(fetched) == result.n_road_tiles == 3
    assert result.n_road_tiles_wanted > result.n_road_tiles


def _stub_result(tmp_path, *, n_road_tiles, n_road_tiles_wanted):
    return SuggestResult(
        suggestions=[],
        output_path=tmp_path / "rsrp_drive_suggestions.geojson",
        gpx_path=None,
        n_roads=12,
        stddev_source=f"geotiff:{tmp_path}",
        range_m=None,
        n_road_tiles=n_road_tiles,
        n_road_tiles_wanted=n_road_tiles_wanted,
    )


def test_cli_flags_a_run_that_hit_the_tile_cap(monkeypatch, capsys, tmp_path):
    from mukoo_model import cli_suggest

    monkeypatch.setattr(
        cli_suggest,
        "run_suggest",
        lambda config, **kw: _stub_result(tmp_path, n_road_tiles=96,
                                          n_road_tiles_wanted=210),
    )
    assert cli_suggest.main(["--out-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "TILE CAP HIT" in out
    assert "96" in out and "210" in out


def test_cli_stays_quiet_when_the_fetch_was_complete(monkeypatch, capsys, tmp_path):
    from mukoo_model import cli_suggest

    monkeypatch.setattr(
        cli_suggest,
        "run_suggest",
        lambda config, **kw: _stub_result(tmp_path, n_road_tiles=40,
                                          n_road_tiles_wanted=40),
    )
    assert cli_suggest.main(["--out-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "TILE CAP" not in out
    assert "40 tile(s)" in out


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
