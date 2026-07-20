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
    score: float = 0.0  # selection score (EVR mass when range known, else sigma)
    visit_order: Optional[int] = None  # 1-based position in the suggested route


# Defaults are deliberately conservative; the CLI exposes all of them.
DEFAULT_TOP_N = 10
DEFAULT_CANDIDATE_QUANTILE = 0.70
DEFAULT_MAX_ROAD_DIST_M = 250.0
DEFAULT_MIN_SEPARATION_M = 1200.0
# How strongly predicted-weak coverage boosts a target's score (0 = off).
# 1.0 doubles the score where prediction saturates "weak" vs. "strong".
DEFAULT_WEAK_BIAS = 0.5


def expected_reduction_scores(
    stddev: np.ndarray,
    grid: Grid,
    cand_x: np.ndarray,
    cand_y: np.ndarray,
    *,
    range_m: float,
) -> np.ndarray:
    """Score candidates by the uncertainty *mass* a measurement would inform.

    A cheap proxy for expected variance reduction: a new reading at ``s``
    informs every cell within the variogram's correlation range, in proportion
    to the covariance decay. So score(s) = Σ_cells C(d)·σ²_cell, with
    C(d) = exp(−3d/range) (the exponential model's effective-range decay).
    Driving where uncertainty is high *and* surrounded by more uncertain area
    beats an equally-uncertain but isolated cell — which raw σ ranking misses.
    """
    xx, yy = np.meshgrid(grid.x, grid.y)
    cx = xx.ravel()
    cy = yy.ravel()
    var = np.nan_to_num(stddev.ravel() ** 2, nan=0.0)
    # (n_cand, n_cells) distances; ~600×6.4k doubles ≈ 30 MB — fine in one shot.
    d = np.sqrt(
        (np.asarray(cand_x)[:, None] - cx[None, :]) ** 2
        + (np.asarray(cand_y)[:, None] - cy[None, :]) ** 2
    )
    decay = np.exp(-3.0 * d / float(range_m))
    decay[d > range_m] = 0.0  # beyond the range a reading tells you ~nothing
    return decay @ var


def weakness_weight(pred_dbm: np.ndarray, *, weak_bias: float) -> np.ndarray:
    """Weight ∈ [1, 1+weak_bias] that upweights predicted-weak coverage.

    Ramps linearly from 1.0 at −95 dBm (comfortably strong) to 1+weak_bias at
    −120 dBm (unusable): verifying uncertainty where coverage is probably bad
    matters more than where it is probably fine.
    """
    w = np.clip((-95.0 - np.asarray(pred_dbm, dtype=np.float64)) / 25.0, 0.0, 1.0)
    return 1.0 + weak_bias * w


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
    range_m: Optional[float] = None,
    mean: Optional[np.ndarray] = None,
    weak_bias: float = 0.0,
) -> list[Suggestion]:
    """Rank drivable high-uncertainty locations; return the top ``top_n``.

    ``stddev`` is the south-up kriging standard-deviation grid (row 0 = south),
    aligned to ``grid``. ``roads`` must be in the same CRS as ``grid``.

    Scoring: with ``range_m`` (the fitted variogram range) each reachable
    candidate is scored by :func:`expected_reduction_scores` — the σ² mass its
    measurement would inform — instead of raw point σ; without it, raw σ is the
    score (back-compatible). With ``mean`` and ``weak_bias`` > 0 the score is
    multiplied by :func:`weakness_weight`, favouring probably-bad coverage.

    Selection is greedy and overlap-aware: after each pick, remaining
    candidates lose the score share already covered by the picked point
    (1 − C(d)), so the list spreads to *informationally* distinct spots; the
    hard ``min_separation_m`` floor still applies. Returns fewer than
    ``top_n`` if reachable, well-spread targets run out.
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

    # Snap every candidate cell onto the nearest road; keep the reachable ones.
    sigmas: list = []
    nears: list = []
    for k in candidate_idx:
        near = roads.nearest(float(flat_x[k]), float(flat_y[k]))
        if near.distance_m > max_road_dist_m:
            continue
        on_road_sigma = _sample_nearest(stddev, grid, near.point_x, near.point_y)
        if not np.isfinite(on_road_sigma):
            on_road_sigma = float(flat_s[k])
        sigmas.append(on_road_sigma)
        nears.append(near)
    if not nears:
        return []

    cand_x = np.array([nr.point_x for nr in nears])
    cand_y = np.array([nr.point_y for nr in nears])
    sigma_arr = np.array(sigmas)

    # Base score: EVR mass when the variogram range is known, else raw sigma.
    if range_m is not None and range_m > 0:
        scores = expected_reduction_scores(
            stddev, grid, cand_x, cand_y, range_m=range_m
        )
    else:
        scores = sigma_arr.copy()
    if mean is not None and weak_bias > 0.0:
        pred = np.array(
            [_sample_nearest(mean, grid, px, py) for px, py in zip(cand_x, cand_y)]
        )
        pred = np.where(np.isfinite(pred), pred, -95.0)  # unknown -> neutral
        scores = scores * weakness_weight(pred, weak_bias=weak_bias)

    # Greedy pick with overlap discount + hard separation floor.
    min_sep_sq = min_separation_m**2
    remaining = scores.astype(np.float64).copy()
    chosen: list = []
    for _ in range(min(top_n, remaining.shape[0])):
        order = np.argsort(remaining)[::-1]
        pick = -1
        for j in order:
            if remaining[j] <= 0:
                break
            if all(
                (cand_x[j] - cand_x[c]) ** 2 + (cand_y[j] - cand_y[c]) ** 2
                >= min_sep_sq
                for c in (i for i, *_ in chosen)
            ):
                pick = int(j)
                break
        if pick < 0:
            break
        chosen.append((pick, float(scores[pick]), float(remaining[pick])))
        if range_m is not None and range_m > 0:
            # what the pick now covers is no longer novel for the others.
            d = np.sqrt((cand_x - cand_x[pick]) ** 2 + (cand_y - cand_y[pick]) ** 2)
            remaining *= 1.0 - np.exp(-3.0 * d / float(range_m))
        remaining[pick] = -np.inf

    to_lonlat = Transformer.from_crs(grid.crs_epsg, WGS84_EPSG, always_xy=True)
    suggestions = []
    for rank, (idx, score, _) in enumerate(chosen, start=1):
        near = nears[idx]
        lon, lat = to_lonlat.transform(near.point_x, near.point_y)
        suggestions.append(
            Suggestion(
                rank=rank,
                lon=float(lon),
                lat=float(lat),
                x=near.point_x,
                y=near.point_y,
                stddev=float(sigma_arr[idx]),
                road_name=near.name,
                road_dist_m=near.distance_m,
                score=score,
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
                "score": round(s.score, 3),
                "visit_order": s.visit_order,
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
