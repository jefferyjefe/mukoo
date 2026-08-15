"""The drivable road network for a bounding box, from OpenStreetMap.

There is no roads table in the database yet, so we fetch the ``drive`` network
from OSM via osmnx and cache it to disk (GeoJSON, EPSG:4326) keyed by the
bounding box — one network call, reused on every later run.

For anything bigger than a compact survey, ask for the ground you need rather
than one box around all of it: :func:`road_tiles` buckets points onto a fixed
lon/lat lattice and :func:`fetch_roads_tiles` unions the occupied tiles. Both
are built on the same per-bbox :func:`fetch_roads` and cache files.

:class:`RoadNetwork` itself is pure Shapely (no osmnx, no network): it holds the
road lines already projected into a metric CRS and answers "what road is nearest
to this point, and where on it?" via an STRtree. That keeps the route-suggestion
logic — and its tests — free of any OSM dependency; only :func:`fetch_roads`
touches the network.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
from pyproj import Transformer
from shapely import STRtree
from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import nearest_points, transform as shp_transform

from .data import WGS84_EPSG

# Tiles are cut from a fixed lon/lat lattice rather than drawn around wherever
# today's candidates happen to land, so a patch of ground keeps the same cache
# key from run to run. Extending the survey then costs only the newly touched
# tiles instead of invalidating one giant bbox and re-downloading everything.
DEFAULT_TILE_DEG = 0.05  # ~5.5 x 4.7 km at 32 N: ~26 km2 per Overpass call
# Ceiling on one run's fetch, ~2500 km2. Well above any real supported region,
# but far below the 12,000 km2 rectangle that was getting the refresh agent
# SIGKILLed, so a surface that somehow arrives unmasked degrades instead of dying.
DEFAULT_MAX_TILES = 96

_METRES_PER_DEG_LAT = 111_320.0


@dataclass
class NearestRoad:
    """Result of snapping a point onto the closest road."""

    index: int
    distance_m: float
    point_x: float  # snapped location, projected metres
    point_y: float
    name: Optional[str]


@dataclass
class RoadNetwork:
    """Road centrelines in a projected (metric) CRS, with nearest-road lookup.

    ``lines`` are Shapely LineStrings in ``crs_epsg`` metres; ``names`` are the
    aligned OSM road names (``None`` where unnamed). Construct directly from
    Shapely lines in tests; use :func:`fetch_roads` for the real OSM network.
    """

    lines: list
    names: list
    crs_epsg: int
    _tree: STRtree = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if len(self.lines) != len(self.names):
            raise ValueError("lines and names must be the same length")
        self._tree = STRtree(self.lines) if self.lines else None

    def __len__(self) -> int:
        return len(self.lines)

    @property
    def is_empty(self) -> bool:
        return len(self.lines) == 0

    def nearest(self, x: float, y: float) -> NearestRoad:
        """Nearest road to ``(x, y)`` and the point on it closest to ``(x, y)``.

        Raises if the network is empty — callers should guard on ``is_empty``.
        """
        if self._tree is None:
            raise ValueError("road network is empty")
        p = Point(x, y)
        idx = int(self._tree.nearest(p))
        line = self.lines[idx]
        snapped = nearest_points(p, line)[1]
        return NearestRoad(
            index=idx,
            distance_m=float(p.distance(line)),
            point_x=float(snapped.x),
            point_y=float(snapped.y),
            name=self.names[idx],
        )


def _is_missing(v) -> bool:
    """True for None or a float NaN (how pandas/OSM represent an absent name)."""
    return v is None or (isinstance(v, float) and math.isnan(v))


def _normalize_name(raw) -> Optional[str]:
    """OSM ``name`` can be a str, a list (ways with several names), NaN, or missing."""
    if _is_missing(raw):
        return None
    if isinstance(raw, list):
        raw = next((v for v in raw if not _is_missing(v)), None)
        if _is_missing(raw):
            return None
    text = str(raw).strip()
    # Guard against a NaN that reached us already stringified (e.g. from cache).
    if not text or text.lower() == "nan":
        return None
    return text


def _project_line(line: LineString, transformer: Transformer) -> LineString:
    return shp_transform(lambda xs, ys: transformer.transform(xs, ys), line)


def _cache_path(cache_dir: Path, bounds_lonlat: tuple) -> Path:
    lon_min, lat_min, lon_max, lat_max = bounds_lonlat
    key = f"{lon_min:.4f}_{lat_min:.4f}_{lon_max:.4f}_{lat_max:.4f}"
    return Path(cache_dir) / f"osm_roads_{key}.geojson"


def _write_cache(path: Path, lines_lonlat, names) -> None:
    features = [
        {
            "type": "Feature",
            "geometry": mapping(line),
            "properties": {"name": name},
        }
        for line, name in zip(lines_lonlat, names)
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features})
    )


def _read_cache(path: Path):
    data = json.loads(Path(path).read_text())
    lines, names = [], []
    for feat in data["features"]:
        lines.append(shape(feat["geometry"]))
        names.append(feat["properties"].get("name"))
    return lines, names


def fetch_roads(
    bounds_lonlat: tuple,
    crs_epsg: int,
    *,
    cache_dir: Path,
    network_type: str = "drive",
    refresh: bool = False,
) -> RoadNetwork:
    """Fetch (or load from cache) the drivable roads over ``bounds_lonlat``.

    ``bounds_lonlat`` is ``(lon_min, lat_min, lon_max, lat_max)``. Roads are
    cached as EPSG:4326 GeoJSON and returned projected into ``crs_epsg`` (the
    metric CRS the uncertainty surface lives in). ``refresh=True`` forces a
    re-fetch. osmnx is imported lazily so importing this module — and running the
    tests — never requires it.

    The fetch is deliberately seam-tolerant: a way crossing the box edge comes
    back whole, and disconnected pieces of network are kept, so cutting a region
    into boxes and unioning the results (:func:`fetch_roads_tiles`) returns what
    one box over the whole region would have. See the comment below for why the
    osmnx defaults cannot be used for that.
    """
    cache_dir = Path(cache_dir)
    cache_file = _cache_path(cache_dir, bounds_lonlat)

    if cache_file.exists() and not refresh:
        lines_lonlat, names = _read_cache(cache_file)
    else:
        import osmnx as ox  # lazy: only needed for a live fetch
        from shapely.geometry import box

        lon_min, lat_min, lon_max, lat_max = bounds_lonlat
        polygon = box(lon_min, lat_min, lon_max, lat_max)
        # Both osmnx defaults quietly delete roads once a region is fetched as
        # many small boxes instead of one big one. truncate_by_edge=False drops
        # every edge whose far endpoint lies outside the box — and the box on
        # the other side of that seam drops it for exactly the same reason, so
        # the way comes back from neither. Simplified rural edges run kilometres
        # between intersections, so one seam can erase a multi-km stretch of the
        # corridor road and leave a candidate beside it with no road in reach.
        # retain_all=False keeps only the largest weakly connected component,
        # which over a 26 km² tile throws away far more (every lane, loop and
        # spur that happens to enter the tile detached) than it did over one
        # 12,000 km² box. Overlapping tiles then return the same way twice,
        # which is what the wkb dedupe in fetch_roads_tiles is for: duplicates
        # are cheap, holes at every seam are not.
        graph = ox.graph_from_polygon(
            polygon,
            network_type=network_type,
            truncate_by_edge=True,
            retain_all=True,
        )
        edges = ox.graph_to_gdfs(graph, nodes=False, edges=True)
        lines_lonlat = list(edges.geometry)
        names = [_normalize_name(n) for n in edges.get("name", [None] * len(edges))]
        _write_cache(cache_file, lines_lonlat, names)

    transformer = Transformer.from_crs(WGS84_EPSG, crs_epsg, always_xy=True)
    lines = [_project_line(line, transformer) for line in lines_lonlat]
    names = [_normalize_name(n) for n in names]
    return RoadNetwork(lines=lines, names=names, crs_epsg=crs_epsg)


def road_tiles(
    lons,
    lats,
    *,
    buffer_m: float = 0.0,
    tile_deg: float = DEFAULT_TILE_DEG,
    max_tiles: Optional[int] = DEFAULT_MAX_TILES,
) -> "list[tuple]":
    """The lattice tiles covering these lon/lat points, each buffered by ``buffer_m``.

    Fetching the bounding box of a set of points stops working the moment the
    survey becomes a corridor: 900 readings strung along one 150 km drive have a
    161 x 76 km bounding box, and the roads that matter occupy a tiny sliver of
    those 12,000 km². Shrinking the box does not help — the box *is* the
    problem. Bucketing the points onto a lattice and fetching only the occupied
    tiles follows the corridor's actual shape instead.

    Each point claims every tile its ``buffer_m`` box overlaps — including the
    tile it is standing in and any tile the box spans entirely — so a candidate
    sitting just inside a tile edge still sees the road on the other side of it.
    Tiles come back in first-seen order of ``lons``/``lats``; when ``max_tiles``
    truncates, a caller that passed its points most-important-first keeps the
    ground that matters most.
    """
    lons = np.asarray(lons, dtype=np.float64).ravel()
    lats = np.asarray(lats, dtype=np.float64).ravel()
    if lons.size == 0:
        return []
    buf_lat = float(buffer_m) / _METRES_PER_DEG_LAT
    # A degree of longitude is shorter the further from the equator, so the
    # east-west buffer has to be widened per point rather than shared.
    cos_lat = np.maximum(np.cos(np.radians(lats)), 1e-6)
    buf_lon = float(buffer_m) / (_METRES_PER_DEG_LAT * cos_lat)

    # Walk each point's tile span rather than bucketing the buffer box's four
    # corners. The corners alone are only the full cover while the box stays
    # inside one tile of its own: as buffer_m approaches tile_deg the corners
    # straddle the tile the point is standing in without ever claiming it —
    # precisely the tile guaranteed to hold that candidate's nearest road. Both
    # max_road_dist_m and tile_deg are free parameters, so the invariant "every
    # covered tile is claimed" has to hold by construction, not by defaults.
    kx0 = np.floor((lons - buf_lon) / tile_deg).astype(np.int64)
    kx1 = np.floor((lons + buf_lon) / tile_deg).astype(np.int64)
    ky0 = np.floor((lats - buf_lat) / tile_deg).astype(np.int64)
    ky1 = np.floor((lats + buf_lat) / tile_deg).astype(np.int64)
    span_x = kx1 - kx0
    span_y = ky1 - ky0
    off_x = np.arange(int(span_x.max()) + 1, dtype=np.int64)
    off_y = np.arange(int(span_y.max()) + 1, dtype=np.int64)
    # Points at different latitudes span different numbers of tiles (a degree of
    # longitude shrinks northwards), so the block is cut for the widest span and
    # the offsets past each point's own span are masked back off.
    covered = (off_x[None, :, None] <= span_x[:, None, None]) & (
        off_y[None, None, :] <= span_y[:, None, None]
    )
    tile_x = np.broadcast_to((kx0[:, None] + off_x)[:, :, None], covered.shape)
    tile_y = np.broadcast_to((ky0[:, None] + off_y)[:, None, :], covered.shape)
    # Point-major order, which is what makes "first seen" mean "claimed by the
    # earliest — most uncertain — point".
    keys = np.stack([tile_x[covered], tile_y[covered]], axis=1)
    _, first_seen = np.unique(keys, axis=0, return_index=True)
    order = np.sort(first_seen)
    if max_tiles is not None:
        order = order[: int(max_tiles)]
    return [
        (
            float(kx * tile_deg),
            float(ky * tile_deg),
            float((kx + 1) * tile_deg),
            float((ky + 1) * tile_deg),
        )
        for kx, ky in keys[order]
    ]


# osmnx has three ways of saying "there is no drivable network in this box", and
# only one of them is a distinguishable exception type. ``graph_from_polygon``
# downloads a 500 m-buffered polygon, truncates the graph to it, keeps the
# components it wants, simplifies, then truncates again to the box actually
# asked for — and each truncation can empty the graph out with a bare
# ``ValueError`` carrying nothing but a message. A rural tile whose only ways
# lie in the buffer ring is the ordinary shape of ground beside a corridor, not
# an edge case, so matching those messages is unavoidable; catching ValueError
# wholesale is not an option, because it would also swallow HTTP failures and
# bad arguments and let them silently shrink the network into bad suggestions.
_EMPTY_TILE_MESSAGES = frozenset(
    {
        # osmnx/truncate.py, truncate_graph_polygon: nothing survived the cut.
        "Found no graph nodes within the requested polygon.",
        # osmnx/convert.py, graph_to_gdfs: the survivors are isolated nodes.
        "Graph contains no edges.",
        "Graph contains no nodes.",
    }
)


def _is_empty_tile_error(exc: BaseException) -> bool:
    """True when ``exc`` is osmnx reporting an empty box rather than a failure.

    ``InsufficientResponseError`` (Overpass answered, with nothing in it) is
    matched by name over the exception's bases rather than by identity, so that
    importing this module never imports osmnx and a subclass still counts.
    """
    if any(cls.__name__ == "InsufficientResponseError" for cls in type(exc).__mro__):
        return True
    return str(exc).strip() in _EMPTY_TILE_MESSAGES


def fetch_roads_tiles(
    tiles: "list[tuple]",
    crs_epsg: int,
    *,
    cache_dir: Path,
    network_type: str = "drive",
    refresh: bool = False,
) -> RoadNetwork:
    """One :class:`RoadNetwork` from several tile fetches, deduplicated.

    Each tile is an ordinary :func:`fetch_roads` call, so tiles cache
    individually under the usual per-bbox filename and peak memory is one small
    tile's worth of OSM rather than the whole survey rectangle's. Because
    :func:`fetch_roads` asks osmnx to keep boundary-crossing edges and every
    component, a way straddling a seam comes back whole from both of its tiles
    instead of being dropped by both; the duplicate that creates is removed
    here by geometry, so ``len(roads)`` still counts distinct edges.

    A tile with no drivable roads in it is a normal answer for rural ground, not
    a failure: it is cached empty (so the next run does not ask Overpass again)
    and skipped. Every other error propagates, because a genuine fetch failure
    must not quietly shrink the network into worse suggestions.
    """
    lines: list = []
    names: list = []
    seen: set = set()
    for bounds in tiles:
        try:
            tile_roads = fetch_roads(
                bounds,
                crs_epsg,
                cache_dir=cache_dir,
                network_type=network_type,
                refresh=refresh,
            )
        except ValueError as exc:
            if not _is_empty_tile_error(exc):
                raise
            _write_cache(_cache_path(Path(cache_dir), bounds), [], [])
            continue
        for line, name in zip(tile_roads.lines, tile_roads.names):
            key = line.wkb
            if key in seen:
                continue
            seen.add(key)
            lines.append(line)
            names.append(name)
    return RoadNetwork(lines=lines, names=names, crs_epsg=crs_epsg)
