"""End-to-end route-suggestion pipeline: uncertainty surface -> roads -> targets.

Reads the kriging standard-deviation surface (from the exported GeoTIFF, or by
recomputing it from PostGIS), fetches the drivable road network around the cells
that could be driven to (cached), ranks drivable high-uncertainty locations, and
writes the suggestions as GeoJSON. The kriging model must have been run at least
once (so the surface exists) unless ``recompute=True``.

The road fetch deliberately does not follow the surface's footprint. That
footprint is the survey's bounding box, which one long drive stretched to
161 x 76 km of which almost nothing is supported; candidates are picked first
(they need no roads) and OSM is then asked only for the tiles they occupy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from pyproj import Transformer

import json

from .config import Config
from .data import WGS84_EPSG, load_rsrp_points
from .db import make_engine
from .kriging import Grid, make_grid
from .raster import load_grid_surface
from .roads import DEFAULT_MAX_TILES, fetch_roads_tiles, road_tiles
from .route import apply_visit_order, write_gpx
from .suggest import (
    DEFAULT_CANDIDATE_QUANTILE,
    DEFAULT_MAX_ROAD_DIST_M,
    DEFAULT_MIN_SEPARATION_M,
    DEFAULT_TOP_N,
    DEFAULT_WEAK_BIAS,
    Suggestion,
    candidate_cells,
    suggest_targets,
    write_suggestions_geojson,
)


@dataclass
class SuggestResult:
    suggestions: list
    output_path: Path
    gpx_path: Optional[Path]
    n_roads: int
    stddev_source: str  # "geotiff:<path>" or "recomputed"
    range_m: Optional[float]  # variogram range used for EVR scoring, if known
    n_road_tiles: int = 0  # OSM boxes actually fetched
    # Boxes the candidate cells cover, before the fetch budget was applied.
    # Larger than n_road_tiles means the run ranked its targets against only
    # part of the road network, which a reader has to be able to see.
    n_road_tiles_wanted: int = 0


def bounds_lonlat_of_grid(grid: Grid) -> tuple:
    """Lon/lat bbox covering the grid, from its four projected corners.

    UTM->lon/lat is not affine, so all four corners are transformed and the
    min/max taken, guaranteeing the box contains the whole grid extent.
    """
    west = grid.x[0] - grid.cell_m / 2.0
    east = grid.x[-1] + grid.cell_m / 2.0
    south = grid.y[0] - grid.cell_m / 2.0
    north = grid.y[-1] + grid.cell_m / 2.0
    to_lonlat = Transformer.from_crs(grid.crs_epsg, WGS84_EPSG, always_xy=True)
    xs = [west, west, east, east]
    ys = [south, north, south, north]
    lons, lats = to_lonlat.transform(xs, ys)
    return (float(min(lons)), float(min(lats)), float(max(lons)), float(max(lats)))


def _recompute_surfaces(
    config: Config, metric: str
) -> "tuple[np.ndarray, np.ndarray, Grid, Optional[float]]":
    """(stddev, mean, grid, range_m) refit from PostGIS.

    Uses the same model the main pipeline would (config's kriging mode,
    anisotropy, dead-zone floor, run dedupe, and support masking), so recomputed
    suggestions match what ``mukoo-krige`` exports rather than silently
    reverting to plain ordinary kriging over the whole bounding box.
    """
    # local: avoids import cycle risk
    from .pipeline import make_model_factory, support_radius_m

    engine = make_engine(config.database_url)
    cloud = load_rsrp_points(
        engine,
        metric=metric,
        none_floor=config.none_floor if metric == "rsrp" else None,
        dedupe_runs=config.dedupe_runs,
        min_session_rows=config.min_session_rows,
    )
    model = make_model_factory(
        config,
        cloud,
        kriging=config.kriging_mode,
        anisotropy_scaling=config.anisotropy[0],
        anisotropy_angle=config.anisotropy[1],
    )()
    model.fit(cloud)
    # Fit first, then grid: the support radius is read off the fitted variogram,
    # so there is nothing to mask against until the model exists.
    grid = make_grid(
        cloud,
        cell_m=config.cell_metres,
        support_radius_m=support_radius_m(model, config),
    )
    surface = model.predict_grid(grid)
    range_m = surface.variogram_params.get("range_m")
    return surface.stddev, surface.mean, surface.grid, range_m


def _range_from_report(report_path: Path) -> Optional[float]:
    """The fitted variogram range recorded by the last mukoo-krige run."""
    try:
        report = json.loads(Path(report_path).read_text())
        value = report.get("variogram", {}).get("range_m")
        return float(value) if value else None
    except (OSError, ValueError):
        return None


def run_suggest(
    config: Config,
    *,
    metric: str = "rsrp",
    stddev_tif: Optional[Path] = None,
    recompute: bool = False,
    top_n: int = DEFAULT_TOP_N,
    candidate_quantile: float = DEFAULT_CANDIDATE_QUANTILE,
    max_road_dist_m: float = DEFAULT_MAX_ROAD_DIST_M,
    min_separation_m: float = DEFAULT_MIN_SEPARATION_M,
    weak_bias: float = DEFAULT_WEAK_BIAS,
    cache_dir: Optional[Path] = None,
    refresh_roads: bool = False,
    network_type: str = "drive",
) -> SuggestResult:
    out_dir = Path(config.output_dir)
    if stddev_tif is None:
        stddev_tif = out_dir / f"{metric}_kriging_stddev.tif"
    mean_tif = Path(stddev_tif).with_name(
        Path(stddev_tif).name.replace("_stddev", "_mean")
    )
    cache_dir = Path(cache_dir) if cache_dir else out_dir / "osm_cache"

    # 1. Surfaces: prefer the exported GeoTIFFs (+ the report's variogram
    #    range for EVR scoring), else refit from PostGIS.
    mean: Optional[np.ndarray] = None
    if not recompute and Path(stddev_tif).exists():
        stddev, grid = load_grid_surface(stddev_tif)
        if mean_tif.exists():
            mean, _ = load_grid_surface(mean_tif)
        range_m = _range_from_report(
            out_dir / f"{metric}_kriging_report.json"
        )
        source = f"geotiff:{stddev_tif}"
    else:
        stddev, mean, grid, range_m = _recompute_surfaces(config, metric)
        source = "recomputed"

    # 2. Roads, but only around the cells that can actually become targets.
    #    Candidate selection needs no roads, so it goes first and bounds the
    #    fetch: the surface's own footprint is 12,000 km² of mostly-unsupported
    #    countryside, and asking OSM for all of it is what was costing 1.15 GB
    #    and a SIGKILL every refresh tick. Most-uncertain first, so the tile
    #    budget (if it ever bites) keeps the ground worth driving to.
    cell_x, cell_y, cell_sigma = candidate_cells(
        stddev, grid, candidate_quantile=candidate_quantile
    )
    order = np.argsort(cell_sigma)[::-1]
    to_lonlat = Transformer.from_crs(grid.crs_epsg, WGS84_EPSG, always_xy=True)
    lons, lats = to_lonlat.transform(cell_x[order], cell_y[order])
    #    The cover is computed whole and the budget applied here, so a run that
    #    exhausted it can say by how much: a truncated fetch ranks targets
    #    against a partial road network and that is a caveat, not a normal run.
    #    Only the fetching had to be bounded — the uncovered tail is a few
    #    hundred 4-tuples even for the whole survey rectangle.
    wanted_tiles = road_tiles(lons, lats, buffer_m=max_road_dist_m, max_tiles=None)
    tiles = wanted_tiles[:DEFAULT_MAX_TILES]
    roads = fetch_roads_tiles(
        tiles,
        grid.crs_epsg,
        cache_dir=cache_dir,
        network_type=network_type,
        refresh=refresh_roads,
    )

    # 3. Rank drivable high-uncertainty locations (EVR + weakness weighting
    #    when the inputs allow), then order them into a drive.
    suggestions = suggest_targets(
        stddev,
        grid,
        roads,
        top_n=top_n,
        candidate_quantile=candidate_quantile,
        max_road_dist_m=max_road_dist_m,
        min_separation_m=min_separation_m,
        range_m=range_m,
        mean=mean,
        weak_bias=weak_bias,
    )
    suggestions = apply_visit_order(suggestions)

    # 4. Export GeoJSON + GPX route.
    out_path = write_suggestions_geojson(
        out_dir / f"{metric}_drive_suggestions.geojson", suggestions, metric=metric
    )
    gpx_path = None
    if suggestions:
        gpx_path = write_gpx(
            out_dir / f"{metric}_drive_route.gpx", suggestions, metric=metric
        )
    return SuggestResult(
        suggestions=suggestions,
        output_path=out_path,
        gpx_path=gpx_path,
        n_roads=len(roads),
        stddev_source=source,
        range_m=range_m,
        n_road_tiles=len(tiles),
        n_road_tiles_wanted=len(wanted_tiles),
    )


def format_suggestions(suggestions: list, metric: str = "rsrp") -> str:
    """Render a ranked table of suggestions for the terminal.

    ``#`` is information rank, ``drv`` the suggested driving order (2-opt).
    """
    unit = "dBm" if metric in {"rsrp", "rsrq"} else "units"
    if not suggestions:
        return "No drivable high-uncertainty targets found."
    header = (
        f"{'#':>2}  {'drv':>3}  {'lat':>10}  {'lon':>11}  {'sigma/' + unit:>9}  "
        f"{'score':>8}  {'road_m':>6}  road"
    )
    lines = [header, "-" * len(header)]
    for s in suggestions:
        name = s.road_name if s.road_name else "(unnamed road)"
        drv = f"{s.visit_order}" if s.visit_order else "-"
        lines.append(
            f"{s.rank:>2}  {drv:>3}  {s.lat:>10.5f}  {s.lon:>11.5f}  "
            f"{s.stddev:>9.2f}  {s.score:>8.1f}  {s.road_dist_m:>6.0f}  {name}"
        )
    return "\n".join(lines)
