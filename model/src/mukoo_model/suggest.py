"""Active-learning route suggestions from a kriging uncertainty surface.

Where should the next drive go to shrink the model's uncertainty the most? The
kriging *standard deviation* surface answers "where is the model least sure",
but a suggestion is only useful if you can drive there. So we:

1. take the grid cells whose uncertainty is in the top ``candidate_quantile``;
2. snap each onto the nearest road and drop any with no road within
   ``max_road_dist_m`` (uncertain but unreachable — e.g. mid-field);
3. rank the reachable ones by the uncertainty *at the on-road point* (that is
   what a drive there would actually reduce);
4. greedily pick the top ``top_n`` while enforcing a ``min_separation_m`` gap, so
   the list spreads out instead of clustering in one hot corner — each drive
   then adds distinct information.

This is decision support: it proposes targets, ranked, with the road they land
on. The output is a GeoJSON of points for a human to choose from.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from pyproj import Transformer

from .data import WGS84_EPSG
from .kriging import Grid
from .roads import RoadNetwork


@dataclass(frozen=True)
class Suggestion:
    """One suggested drive target, snapped onto a real road."""

    rank: int
    lon: float
    lat: float
    x: float  # projected metres (surface CRS)
    y: float
    stddev: float  # kriging 1-sigma uncertainty at the on-road point
    road_name: Optional[str]
    road_dist_m: float  # how far the high-uncertainty cell was from the road


# Defaults are deliberately conservative; the CLI exposes all of them.
DEFAULT_TOP_N = 10
DEFAULT_CANDIDATE_QUANTILE = 0.70
DEFAULT_MAX_ROAD_DIST_M = 250.0
DEFAULT_MIN_SEPARATION_M = 1200.0


def _sample_nearest(stddev: np.ndarray, grid: Grid, x: float, y: float) -> float:
    """Uncertainty value at the grid cell nearest to ``(x, y)`` (NaN if empty)."""
    j = int(round((x - grid.x[0]) / grid.cell_m))
    i = int(round((y - grid.y[0]) / grid.cell_m))
    j = min(max(j, 0), grid.x.shape[0] - 1)
    i = min(max(i, 0), grid.y.shape[0] - 1)
    return float(stddev[i, j])


def suggest_targets(
    stddev: np.ndarray,
    grid: Grid,
    roads: RoadNetwork,
    *,
    top_n: int = DEFAULT_TOP_N,
    candidate_quantile: float = DEFAULT_CANDIDATE_QUANTILE,
    max_road_dist_m: float = DEFAULT_MAX_ROAD_DIST_M,
    min_separation_m: float = DEFAULT_MIN_SEPARATION_M,
) -> list[Suggestion]:
    """Rank drivable high-uncertainty locations; return the top ``top_n``.

    ``stddev`` is the south-up kriging standard-deviation grid (row 0 = south),
    aligned to ``grid``. ``roads`` must be in the same CRS as ``grid``. Returns
    fewer than ``top_n`` if the separation constraint or a sparse road network
    leaves too few reachable, well-spread targets.
    """
    if roads.is_empty:
        return []
    if roads.crs_epsg != grid.crs_epsg:
        raise ValueError(
            f"roads CRS EPSG:{roads.crs_epsg} != grid CRS EPSG:{grid.crs_epsg}"
        )
    if not 0.0 <= candidate_quantile < 1.0:
        raise ValueError("candidate_quantile must be in [0, 1)")

    xx, yy = np.meshgrid(grid.x, grid.y)  # (nrows, ncols), matches stddev
    flat_x = xx.ravel()
    flat_y = yy.ravel()
    flat_s = stddev.ravel()

    finite = np.isfinite(flat_s)
    if not finite.any():
        return []
    threshold = float(np.quantile(flat_s[finite], candidate_quantile))
    candidate_idx = np.where(finite & (flat_s >= threshold))[0]

    # Snap every candidate cell onto the nearest road; keep the reachable ones,
    # scoring each by the uncertainty at its on-road point.
    scored = []
    for k in candidate_idx:
        near = roads.nearest(float(flat_x[k]), float(flat_y[k]))
        if near.distance_m > max_road_dist_m:
            continue
        on_road_sigma = _sample_nearest(stddev, grid, near.point_x, near.point_y)
        if not np.isfinite(on_road_sigma):
            on_road_sigma = float(flat_s[k])
        scored.append((on_road_sigma, near))

    if not scored:
        return []
    scored.sort(key=lambda t: t[0], reverse=True)

    # Greedy: take the most uncertain first, suppress anything within the
    # separation radius, so suggestions spread across the map.
    min_sep_sq = min_separation_m**2
    chosen: list = []
    for sigma, near in scored:
        if any(
            (near.point_x - c.point_x) ** 2 + (near.point_y - c.point_y) ** 2
            < min_sep_sq
            for _, c in chosen
        ):
            continue
        chosen.append((sigma, near))
        if len(chosen) >= top_n:
            break

    to_lonlat = Transformer.from_crs(grid.crs_epsg, WGS84_EPSG, always_xy=True)
    suggestions = []
    for rank, (sigma, near) in enumerate(chosen, start=1):
        lon, lat = to_lonlat.transform(near.point_x, near.point_y)
        suggestions.append(
            Suggestion(
                rank=rank,
                lon=float(lon),
                lat=float(lat),
                x=near.point_x,
                y=near.point_y,
                stddev=float(sigma),
                road_name=near.name,
                road_dist_m=near.distance_m,
            )
        )
    return suggestions


def suggestions_to_geojson(suggestions: list, *, metric: str = "rsrp") -> dict:
    """A FeatureCollection of ranked drive targets (points), most-uncertain first."""
    unit = "dBm" if metric in {"rsrp", "rsrq"} else metric
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [s.lon, s.lat]},
            "properties": {
                "rank": s.rank,
                "metric": metric,
                "stddev": round(s.stddev, 3),
                "stddev_unit": unit,
                "road_name": s.road_name,
                "road_distance_m": round(s.road_dist_m, 1),
            },
        }
        for s in suggestions
    ]
    return {
        "type": "FeatureCollection",
        "properties": {
            "description": (
                f"Active-learning drive suggestions: locations where driving "
                f"most reduces {metric} kriging uncertainty. Ranked, on-road."
            ),
            "metric": metric,
            "count": len(suggestions),
        },
        "features": features,
    }


def write_suggestions_geojson(
    path: Path, suggestions: list, *, metric: str = "rsrp"
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(suggestions_to_geojson(suggestions, metric=metric), indent=2))
    return path
