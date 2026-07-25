"""Read point measurements from PostGIS and project them into metric space.

Kriging assumes an isotropic distance metric. Raw coordinates are geographic
(EPSG:4326, degrees), where one degree of longitude and one of latitude are not
the same ground distance, so we project every point into the local UTM zone
(metres) before any variogram is fitted. Everything downstream — variogram,
grid, GeoTIFF — lives in that projected CRS.

Two kinds of duplicate are collapsed here, for different reasons:

- **Coincident points** (:func:`_collapse_coincident`) — two rows at the same
  projected location make the kriging system singular. Purely numerical.
- **Latched modem readings** (``dedupe_runs``) — the logger samples every ~3.5 s
  but the modem refreshes its signal report far more slowly, so one measurement
  is re-read several times in a row. These rows are not independent
  observations; feeding all of them to a variogram overstates the sample size
  and biases the short-lag structure. Statistical, and off by default.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from pyproj import Transformer
from sqlalchemy import text
from sqlalchemy.engine import Engine

WGS84_EPSG = 4326

# Keeps only the first row of each run of consecutive samples that re-report one
# modem reading within a drive session — see ``dedupe_runs`` in
# :func:`load_rsrp_points`.
#
# A row is a re-read of its predecessor when EITHER:
#   * the modem stamped both with the same ``modem_reported_at`` — the modem
#     itself saying "this is the same measurement", which is why that column
#     exists (schema 0002); or
#   * the whole signal triple (rsrp, rsrq, sinr) is unchanged — the older,
#     inferential test, and the only one available for rows recorded before the
#     logger reported a modem timestamp.
#
# Taking either is deliberately the more aggressive reading, and it degrades
# safely in both directions: with no modem timestamp (every row predating 0002)
# the first test never fires and this reduces exactly to the value comparison,
# and if a modem restamps a latched value on every poll the first test simply
# never matches. Neither case can make the filter keep fewer rows than the value
# test alone would.
#
# Notes on the shape of this query:
# * The window runs over *every* row of the session, before any metric or
#   caller predicate is applied. A run is a property of the raw time series, so
#   which rows a later filter happens to keep must not change where runs begin.
# * Ordered by ``recorded_at`` (the phone's clock), not ``id``: ``id`` is upload
#   order, and store-and-forward means a session can be uploaded long after it
#   was driven, interleaved with others.
# * ``IS NOT DISTINCT FROM`` so that NULL == NULL counts as unchanged; a
#   dead-zone stretch (null metric) collapses like any other repeated reading.
# * ``m.*`` passes every original column through, so the outer query's
#   predicates — including a caller-supplied ``where`` — still see the full
#   table. The subquery is aliased ``measurements`` for the same reason.
_RUN_START_SUBQUERY = """(
    SELECT * FROM (
        SELECT m.*,
               lag(m.session_id)        OVER w AS _prev_session,
               lag(m.rsrp)              OVER w AS _prev_rsrp,
               lag(m.rsrq)              OVER w AS _prev_rsrq,
               lag(m.sinr)              OVER w AS _prev_sinr,
               lag(m.modem_reported_at) OVER w AS _prev_modem
        FROM measurements m
        WINDOW w AS (PARTITION BY m.session_id ORDER BY m.recorded_at, m.id)
    ) s
    WHERE s._prev_session IS NULL
       OR NOT (
              (s.modem_reported_at IS NOT NULL
               AND s._prev_modem IS NOT NULL
               AND s.modem_reported_at = s._prev_modem)
           OR (s.rsrp IS NOT DISTINCT FROM s._prev_rsrp
               AND s.rsrq IS NOT DISTINCT FROM s._prev_rsrq
               AND s.sinr IS NOT DISTINCT FROM s._prev_sinr)
          )
) AS measurements"""


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
    # Rows matching the predicate *before* the consecutive-run filter dropped
    # latched re-reads; None when that filter was not applied.
    n_before_dedupe: int | None = None

    @property
    def n(self) -> int:
        return int(self.values.shape[0])

    @property
    def n_merged(self) -> int:
        """How many rows were merged away when collapsing coincident points."""
        return self.n_raw - self.n

    @property
    def n_dedupe_dropped(self) -> int:
        """Rows dropped as latched re-reads (0 when dedupe was off)."""
        if self.n_before_dedupe is None:
            return 0
        return self.n_before_dedupe - self.n_raw

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
    none_floor: float | None = None,
    dedupe_runs: bool = False,
) -> PointCloud:
    """Load non-null ``metric`` measurements from PostGIS into a PointCloud.

    Only ``rsrp``/``rsrq``/``sinr`` are accepted for ``metric`` (they are the
    numeric signal columns); the value is parameter-free SQL identifier so it is
    validated against that allowlist rather than interpolated blindly. ``where``
    is an optional extra SQL predicate (e.g. a carrier or time filter), ANDed in.
    Rows are ordered by ``id`` so a run is reproducible.

    ``none_floor`` (RSRP only) additionally loads dead-zone rows —
    ``network_type = 'none'``, where the metric is null because there was
    nothing to measure — at that floor value (dBm). A dead zone is a *censored*
    reading ("at or below anything a phone can report"), so a floor slightly
    under the device's weakest real report keeps no-coverage areas from being
    interpolated as ordinary gaps between healthy readings.

    ``dedupe_runs`` keeps only the first sample of each run of consecutive rows
    that re-report one modem reading within a session — identified by a shared
    ``modem_reported_at`` where the logger recorded one, otherwise by an
    unchanged ``(rsrp, rsrq, sinr)`` triple.
    The logger samples faster than the modem refreshes its signal report, so
    such a run is one measurement re-read, not several observations. The *first*
    sample is the representative because the modem's averaging window closes at
    or before the moment the value first appears — the reading's true location
    is at, or slightly behind, the run's start, never ahead of it. (Taking the
    run's midpoint would displace a value forward by up to half the run's
    length, which on real drive data reaches several hundred metres.) Keeping
    the first is also append-stable: a later upload never moves an existing
    representative.
    """
    allowed = {"rsrp", "rsrq", "sinr"}
    if metric not in allowed:
        raise ValueError(f"metric must be one of {sorted(allowed)}, got {metric!r}")
    if none_floor is not None and metric != "rsrp":
        raise ValueError("none_floor only applies to metric='rsrp'")

    predicate = f"{metric} IS NOT NULL"
    value_expr = metric
    if none_floor is not None:
        predicate = f"({metric} IS NOT NULL OR network_type = 'none')"
        value_expr = f"coalesce({metric}, {float(none_floor)})"
    if where:
        predicate = f"({predicate}) AND ({where})"
    source = _RUN_START_SUBQUERY if dedupe_runs else "measurements"
    sql = text(
        f"SELECT ST_X(geom) AS lon, ST_Y(geom) AS lat, {value_expr} AS value, "
        f"session_id::text AS session, coalesce(cell_id, '') AS cell "
        f"FROM {source} WHERE {predicate} ORDER BY id"
    )

    with engine.connect() as conn:
        rows = conn.execute(sql).all()
        # Same predicate, no run filter: how many rows the dedupe stood down.
        n_before_dedupe = (
            int(
                conn.execute(
                    text(f"SELECT count(*) FROM measurements WHERE {predicate}")
                ).scalar_one()
            )
            if dedupe_runs
            else None
        )
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
        n_before_dedupe=n_before_dedupe,
    )
