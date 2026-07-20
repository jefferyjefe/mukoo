"""Read point measurements from PostGIS and project them into metric space.

Kriging assumes an isotropic distance metric. Raw coordinates are geographic
(EPSG:4326, degrees), where one degree of longitude and one of latitude are not
the same ground distance, so we project every point into the local UTM zone
(metres) before any variogram is fitted. Everything downstream — variogram,
grid, GeoTIFF — lives in that projected CRS.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from pyproj import Transformer
from sqlalchemy import text
from sqlalchemy.engine import Engine

WGS84_EPSG = 4326


def utm_epsg_for(lon: float, lat: float) -> int:
    """Return the EPSG code of the UTM zone containing ``(lon, lat)``.

    32600 + zone for the northern hemisphere, 32700 + zone for the southern.
    """
    zone = int((lon + 180.0) // 6.0) + 1
    zone = min(max(zone, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


@dataclass(frozen=True)
class PointCloud:
    """Measurement points ready for kriging.

    ``x``/``y`` are metres in ``crs_epsg`` (a UTM zone); ``lon``/``lat`` are the
    original WGS84 coordinates, kept so results can be related back to the map.
    ``values`` is the modelled metric (RSRP, dBm). Coincident points have been
    collapsed to one averaged value — see :func:`load_rsrp_points`.

    ``session``/``cell`` are optional per-point labels (drive session id and
    serving cell id): session labels drive leave-one-session-out CV, cell ids
    drive tower estimation for the path-loss prior. After collapsing, a merged
    group keeps its first member's labels.
    """

    lon: np.ndarray
    lat: np.ndarray
    x: np.ndarray
    y: np.ndarray
    values: np.ndarray
    crs_epsg: int
    metric: str
    n_raw: int  # rows returned by the query, before collapsing duplicates
    session: np.ndarray | None = None  # (n,) str labels, or None
    cell: np.ndarray | None = None  # (n,) str labels ("" where null), or None

    @property
    def n(self) -> int:
        return int(self.values.shape[0])

    @property
    def n_merged(self) -> int:
        """How many rows were merged away when collapsing coincident points."""
        return self.n_raw - self.n

    def bounds_xy(self) -> tuple[float, float, float, float]:
        """(xmin, ymin, xmax, ymax) of the points, in projected metres."""
        return (
            float(self.x.min()),
            float(self.y.min()),
            float(self.x.max()),
            float(self.y.max()),
        )

    def bounds_lonlat(self) -> tuple[float, float, float, float]:
        """(lon_min, lat_min, lon_max, lat_max) of the points, in degrees."""
        return (
            float(self.lon.min()),
            float(self.lat.min()),
            float(self.lon.max()),
            float(self.lat.max()),
        )


def _collapse_coincident(
    x: np.ndarray,
    y: np.ndarray,
    values: np.ndarray,
    *,
    tol_m: float = 1.0,
    labels: "tuple[np.ndarray, ...]" = (),
) -> "tuple[np.ndarray, np.ndarray, np.ndarray, tuple[np.ndarray, ...]]":
    """Average the value at points sharing a projected location.

    Ordinary kriging builds a matrix of point-to-point covariances; two points
    at the same location produce identical rows and a singular system. GPS jitter
    means genuine repeats land within a metre or so, so we snap to a ``tol_m``
    grid, group, and average the metric within each occupied cell. Returns the
    representative coordinate (the group mean) and averaged value per group.

    ``labels`` are optional per-point label arrays (session, cell, ...) carried
    through the collapse; a merged group keeps its first member's label.
    """
    keys = np.stack(
        [np.round(x / tol_m).astype(np.int64), np.round(y / tol_m).astype(np.int64)],
        axis=1,
    )
    _, first_idx, inverse, counts = np.unique(
        keys, axis=0, return_index=True, return_inverse=True, return_counts=True
    )
    inverse = inverse.ravel()
    n_groups = counts.shape[0]
    if n_groups == x.shape[0]:
        return x, y, values, labels  # nothing coincident

    def _mean_by_group(a: np.ndarray) -> np.ndarray:
        sums = np.zeros(n_groups, dtype=np.float64)
        np.add.at(sums, inverse, a)
        return sums / counts

    kept_labels = tuple(lab[first_idx] for lab in labels)
    return _mean_by_group(x), _mean_by_group(y), _mean_by_group(values), kept_labels


def load_rsrp_points(
    engine: Engine,
    *,
    metric: str = "rsrp",
    where: str | None = None,
) -> PointCloud:
    """Load non-null ``metric`` measurements from PostGIS into a PointCloud.

    Only ``rsrp``/``rsrq``/``sinr`` are accepted for ``metric`` (they are the
    numeric signal columns); the value is parameter-free SQL identifier so it is
    validated against that allowlist rather than interpolated blindly. ``where``
    is an optional extra SQL predicate (e.g. a carrier or time filter), ANDed in.
    Rows are ordered by ``id`` so a run is reproducible.
    """
    allowed = {"rsrp", "rsrq", "sinr"}
    if metric not in allowed:
        raise ValueError(f"metric must be one of {sorted(allowed)}, got {metric!r}")

    predicate = f"{metric} IS NOT NULL"
    if where:
        predicate = f"({predicate}) AND ({where})"
    sql = text(
        f"SELECT ST_X(geom) AS lon, ST_Y(geom) AS lat, {metric} AS value, "
        f"session_id::text AS session, coalesce(cell_id, '') AS cell "
        f"FROM measurements WHERE {predicate} ORDER BY id"
    )

    with engine.connect() as conn:
        rows = conn.execute(sql).all()
    if not rows:
        raise ValueError(
            f"No measurements with non-null {metric}"
            + (f" matching {where!r}" if where else "")
        )

    lon = np.array([r.lon for r in rows], dtype=np.float64)
    lat = np.array([r.lat for r in rows], dtype=np.float64)
    values = np.array([float(r.value) for r in rows], dtype=np.float64)
    session = np.array([r.session for r in rows], dtype=object)
    cell = np.array([r.cell for r in rows], dtype=object)
    n_raw = lon.shape[0]

    crs_epsg = utm_epsg_for(float(lon.mean()), float(lat.mean()))
    transformer = Transformer.from_crs(WGS84_EPSG, crs_epsg, always_xy=True)
    x, y = transformer.transform(lon, lat)
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)

    x, y, values, (session, cell) = _collapse_coincident(
        x, y, values, labels=(session, cell)
    )

    # Re-derive lon/lat for the (possibly averaged) projected coordinates so the
    # two coordinate views stay consistent after collapsing duplicates.
    back = Transformer.from_crs(crs_epsg, WGS84_EPSG, always_xy=True)
    lon, lat = back.transform(x, y)

    return PointCloud(
        lon=np.asarray(lon, dtype=np.float64),
        lat=np.asarray(lat, dtype=np.float64),
        x=x,
        y=y,
        values=values,
        crs_epsg=crs_epsg,
        metric=metric,
        n_raw=n_raw,
        session=session,
        cell=cell,
    )
