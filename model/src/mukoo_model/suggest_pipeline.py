"""End-to-end route-suggestion pipeline: uncertainty surface -> roads -> targets.

Reads the kriging standard-deviation surface (from the exported GeoTIFF, or by
recomputing it from PostGIS), fetches the drivable road network for its bounding
box from OSM (cached), ranks drivable high-uncertainty locations, and writes the
suggestions as GeoJSON. The kriging model must have been run at least once (so
the surface exists) unless ``recompute=True``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from pyproj import Transformer

from .config import Config
from .data import WGS84_EPSG, load_rsrp_points
from .db import make_engine
from .kriging import Grid, OrdinaryKrigingModel, make_grid
from .raster import load_grid_surface
from .roads import fetch_roads
from .suggest import (
    DEFAULT_CANDIDATE_QUANTILE,
    DEFAULT_MAX_ROAD_DIST_M,
    DEFAULT_MIN_SEPARATION_M,
    DEFAULT_TOP_N,
    Suggestion,
    suggest_targets,
    write_suggestions_geojson,
)


@dataclass
class SuggestResult:
    suggestions: list
    output_path: Path
    n_roads: int
    stddev_source: str  # "geotiff:<path>" or "recomputed"


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


def _recompute_stddev(config: Config, metric: str) -> tuple[np.ndarray, Grid]:
    engine = make_engine(config.database_url)
    cloud = load_rsrp_points(engine, metric=metric)
    model = OrdinaryKrigingModel(
        variogram_model=config.variogram_model, nlags=config.nlags
    ).fit(cloud)
    surface = model.predict_grid(make_grid(cloud, cell_m=config.cell_metres))
    return surface.stddev, surface.grid


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
    cache_dir: Optional[Path] = None,
    refresh_roads: bool = False,
    network_type: str = "drive",
) -> SuggestResult:
    out_dir = Path(config.output_dir)
    if stddev_tif is None:
        stddev_tif = out_dir / f"{metric}_kriging_stddev.tif"
    cache_dir = Path(cache_dir) if cache_dir else out_dir / "osm_cache"

    # 1. Uncertainty surface: prefer the exported GeoTIFF, else recompute.
    if not recompute and Path(stddev_tif).exists():
        stddev, grid = load_grid_surface(stddev_tif)
        source = f"geotiff:{stddev_tif}"
    else:
        stddev, grid = _recompute_stddev(config, metric)
        source = "recomputed"

    # 2. Roads for the surface's footprint (cached OSM fetch).
    bounds = bounds_lonlat_of_grid(grid)
    roads = fetch_roads(
        bounds,
        grid.crs_epsg,
        cache_dir=cache_dir,
        network_type=network_type,
        refresh=refresh_roads,
    )

    # 3. Rank drivable high-uncertainty locations.
    suggestions = suggest_targets(
        stddev,
        grid,
        roads,
        top_n=top_n,
        candidate_quantile=candidate_quantile,
        max_road_dist_m=max_road_dist_m,
        min_separation_m=min_separation_m,
    )

    # 4. Export GeoJSON.
    out_path = write_suggestions_geojson(
        out_dir / f"{metric}_drive_suggestions.geojson", suggestions, metric=metric
    )
    return SuggestResult(
        suggestions=suggestions,
        output_path=out_path,
        n_roads=len(roads),
        stddev_source=source,
    )


def format_suggestions(suggestions: list, metric: str = "rsrp") -> str:
    """Render a ranked table of suggestions for the terminal."""
    unit = "dBm" if metric in {"rsrp", "rsrq"} else "units"
    if not suggestions:
        return "No drivable high-uncertainty targets found."
    header = (
        f"{'#':>2}  {'lat':>10}  {'lon':>11}  {'sigma/' + unit:>9}  "
        f"{'road_m':>6}  road"
    )
    lines = [header, "-" * len(header)]
    for s in suggestions:
        name = s.road_name if s.road_name else "(unnamed road)"
        lines.append(
            f"{s.rank:>2}  {s.lat:>10.5f}  {s.lon:>11.5f}  "
            f"{s.stddev:>9.2f}  {s.road_dist_m:>6.0f}  {name}"
        )
    return "\n".join(lines)
