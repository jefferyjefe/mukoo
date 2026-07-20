"""The drivable road network for a bounding box, from OpenStreetMap.

There is no roads table in the database yet, so we fetch the ``drive`` network
from OSM via osmnx and cache it to disk (GeoJSON, EPSG:4326) keyed by the
bounding box — one network call, reused on every later run.

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

from pyproj import Transformer
from shapely import STRtree
from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import nearest_points, transform as shp_transform

from .data import WGS84_EPSG


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
        graph = ox.graph_from_polygon(polygon, network_type=network_type)
        edges = ox.graph_to_gdfs(graph, nodes=False, edges=True)
        lines_lonlat = list(edges.geometry)
        names = [_normalize_name(n) for n in edges.get("name", [None] * len(edges))]
        _write_cache(cache_file, lines_lonlat, names)

    transformer = Transformer.from_crs(WGS84_EPSG, crs_epsg, always_xy=True)
    lines = [_project_line(line, transformer) for line in lines_lonlat]
    names = [_normalize_name(n) for n in names]
    return RoadNetwork(lines=lines, names=names, crs_epsg=crs_epsg)
