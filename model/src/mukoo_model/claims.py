"""FCC claimed-coverage vs measured signal: is the carrier's filing true here?

Carriers file mobile-coverage claims with the FCC (Broadband Data Collection)
as H3 hexagons, each carrying ``minsignal`` — the minimum RSRP the carrier
claims a user experiences anywhere in that hex. Our drives measure what a phone
actually saw. This module joins the two: for every measurement inside a claimed
hex, ``gap = measured_rsrp − claimed_minsignal``. A negative gap is the
carrier's own filing contradicted by ground truth at that exact spot.

The BDC GeoPackage is read directly with the stdlib ``sqlite3`` module (a
GeoPackage *is* a SQLite file) plus a small parser for the GeoPackage geometry
blob header — no GDAL/fiona dependency. Only the standard per-provider mobile
H3 schema is assumed: ``minsignal``, ``environmnt``, ``h3_res9_id`` and a
geometry column registered in ``gpkg_geometry_columns``.

``environmnt`` matters: 1 = in-vehicle mobile (what drive data measures),
0 = outdoor stationary. Compare like with like — the default keeps only 1.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import shapely
from shapely import STRtree

# GeoPackage geometry blob header: envelope-indicator -> envelope byte length.
_ENVELOPE_BYTES = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}


def parse_gpkg_geometry(blob: bytes) -> "tuple[Optional[bytes], Optional[tuple]]":
    """Split a GeoPackage geometry blob into ``(wkb, envelope)``.

    ``envelope`` is ``(minx, miny, maxx, maxy)`` when the header carries one,
    else None. Returns ``(None, None)`` for an empty geometry. Raises
    ``ValueError`` on anything that is not a GPKG geometry blob.
    """
    if blob is None or len(blob) < 8 or bytes(blob[:2]) != b"GP":
        raise ValueError("not a GeoPackage geometry blob")
    flags = blob[3]
    little = bool(flags & 0x01)
    if flags & 0x10:  # empty-geometry flag
        return None, None
    env_indicator = (flags >> 1) & 0x07
    if env_indicator not in _ENVELOPE_BYTES:
        raise ValueError(f"invalid envelope indicator {env_indicator}")
    env_len = _ENVELOPE_BYTES[env_indicator]
    envelope = None
    if env_len:
        order = "<" if little else ">"
        vals = struct.unpack_from(f"{order}{env_len // 8}d", blob, 8)
        # header order is minx, maxx, miny, maxy (x pair first, then y pair)
        envelope = (vals[0], vals[2], vals[1], vals[3])
    return bytes(blob[8 + env_len :]), envelope


@dataclass(frozen=True)
class ClaimedHexes:
    """Claimed-coverage hexes from one BDC filing, filtered and parsed."""

    polygons: list  # shapely polygons, EPSG:4326
    minsignal: np.ndarray  # (n,) claimed minimum RSRP per hex, dBm
    h3_ids: list  # (n,) H3 cell ids (str)
    environment: int
    gpkg_path: str
    n_scanned: int  # feature rows examined before filtering

    @property
    def n(self) -> int:
        return len(self.polygons)


def _feature_table(conn: sqlite3.Connection) -> "tuple[str, str]":
    """The (table, geometry column) of the GeoPackage's single feature layer."""
    row = conn.execute(
        "SELECT c.table_name, g.column_name FROM gpkg_contents c "
        "JOIN gpkg_geometry_columns g ON g.table_name = c.table_name "
        "WHERE c.data_type = 'features'"
    ).fetchone()
    if row is None:
        raise ValueError("no feature layer found in GeoPackage")
    return row[0], row[1]


def read_claimed_hexes(
    gpkg_path: Path,
    *,
    environment: int = 1,
    bbox: "Optional[tuple[float, float, float, float]]" = None,
) -> ClaimedHexes:
    """Load claimed hexes from a BDC GeoPackage, filtered before parsing.

    ``bbox`` is ``(lon_min, lat_min, lon_max, lat_max)``; hexes whose envelope
    misses it are skipped without WKB parsing (statewide filings are ~1.5M
    hexes — the header envelope check keeps a load to a few seconds). Rows are
    kept only for the requested ``environmnt``.
    """
    gpkg_path = Path(gpkg_path)
    if not gpkg_path.is_file():
        raise ValueError(f"GeoPackage not found: {gpkg_path}")
    # A GeoPackage is a SQLite database; open read-only.
    conn = sqlite3.connect(f"file:{gpkg_path}?mode=ro", uri=True)
    try:
        table, geom_col = _feature_table(conn)
        cols = {r[1].lower() for r in conn.execute(f'PRAGMA table_info("{table}")')}
        for needed in ("minsignal", "environmnt", "h3_res9_id"):
            if needed not in cols:
                raise ValueError(
                    f"column {needed!r} not in layer {table!r} — not a BDC "
                    f"mobile H3 filing? (columns: {sorted(cols)})"
                )

        polygons: list = []
        minsignal: list = []
        h3_ids: list = []
        n_scanned = 0
        cursor = conn.execute(
            f'SELECT "{geom_col}", minsignal, h3_res9_id FROM "{table}" '
            f"WHERE environmnt = ?",
            (environment,),
        )
        for blob, minsig, h3 in cursor:
            n_scanned += 1
            wkb, envelope = parse_gpkg_geometry(blob)
            if wkb is None:
                continue
            if bbox is not None and envelope is not None:
                minx, miny, maxx, maxy = envelope
                if (
                    maxx < bbox[0]
                    or minx > bbox[2]
                    or maxy < bbox[1]
                    or miny > bbox[3]
                ):
                    continue
            geom = shapely.from_wkb(wkb)
            if bbox is not None and envelope is None:
                gminx, gminy, gmaxx, gmaxy = geom.bounds
                if (
                    gmaxx < bbox[0]
                    or gminx > bbox[2]
                    or gmaxy < bbox[1]
                    or gminy > bbox[3]
                ):
                    continue
            polygons.append(geom)
            minsignal.append(float(minsig))
            h3_ids.append(str(h3))
    finally:
        conn.close()

    return ClaimedHexes(
        polygons=polygons,
        minsignal=np.asarray(minsignal, dtype=np.float64),
        h3_ids=h3_ids,
        environment=environment,
        gpkg_path=str(gpkg_path),
        n_scanned=n_scanned,
    )


def match_points_to_hexes(
    lon: np.ndarray, lat: np.ndarray, hexes: ClaimedHexes
) -> np.ndarray:
    """Index of the claimed hex containing each point, −1 where unclaimed.

    Containment is ``intersects`` (boundary counts as inside, matching the
    PostGIS ``ST_Intersects`` convention); a point exactly on a shared hex edge
    keeps its first match.
    """
    n = int(np.asarray(lon).shape[0])
    idx = np.full(n, -1, dtype=np.int64)
    if hexes.n == 0 or n == 0:
        return idx
    points = shapely.points(np.asarray(lon), np.asarray(lat))
    tree = STRtree(hexes.polygons)
    point_i, hex_i = tree.query(points, predicate="intersects")
    for p, h in zip(point_i, hex_i):
        if idx[p] == -1:
            idx[p] = h
    return idx


@dataclass(frozen=True)
class ClaimsResult:
    """The measured-vs-claimed comparison over one set of points."""

    hexes: ClaimedHexes
    lon: np.ndarray
    lat: np.ndarray
    rsrp: np.ndarray
    measurement_id: np.ndarray  # DB ids, aligned with the point arrays
    hex_index: np.ndarray  # per point, −1 = outside every claimed hex
    claimed: np.ndarray  # per point claimed minsignal (nan outside)
    gap: np.ndarray  # rsrp − claimed (nan outside); negative = violation

    @property
    def inside(self) -> np.ndarray:
        return self.hex_index >= 0

    @property
    def violation(self) -> np.ndarray:
        """Inside a claimed hex AND measured below the claimed minimum."""
        return self.inside & (self.gap < 0)

    def summary(self) -> dict:
        inside = self.inside
        viol = self.violation
        n = int(inside.shape[0])
        n_inside = int(inside.sum())
        n_viol = int(viol.sum())

        by_tier = []
        for tier in sorted(set(self.claimed[inside].tolist())):
            m = inside & (self.claimed == tier)
            by_tier.append(
                {
                    "claimed_minsignal_dbm": tier,
                    "points": int(m.sum()),
                    "violations": int((m & viol).sum()),
                    "avg_measured_dbm": round(float(self.rsrp[m].mean()), 1),
                }
            )

        worst = None
        if n_viol:
            w = int(np.nanargmin(np.where(viol, self.gap, np.nan)))
            worst = {
                "measurement_id": int(self.measurement_id[w]),
                "lat": round(float(self.lat[w]), 6),
                "lon": round(float(self.lon[w]), 6),
                "measured_rsrp_dbm": float(self.rsrp[w]),
                "claimed_minsignal_dbm": float(self.claimed[w]),
                "gap_db": float(self.gap[w]),
                "h3_res9_id": self.hexes.h3_ids[int(self.hex_index[w])],
            }

        return {
            "gpkg": self.hexes.gpkg_path,
            "environment": self.hexes.environment,
            "n_claimed_hexes_in_area": self.hexes.n,
            "points": {
                "total": n,
                "inside_claimed_hex": n_inside,
                "outside_any_claim": n - n_inside,
            },
            "violations": {
                "count": n_viol,
                "share_of_inside": round(n_viol / n_inside, 4) if n_inside else None,
                "avg_gap_db": (
                    round(float(self.gap[viol].mean()), 2) if n_viol else None
                ),
                "worst_gap_db": (
                    round(float(self.gap[viol].min()), 2) if n_viol else None
                ),
                "worst_point": worst,
            },
            "by_claimed_minsignal": by_tier,
        }

    def violations_geojson(self) -> dict:
        """FeatureCollection of every violating measurement, worst first."""
        order = np.argsort(np.where(self.violation, self.gap, np.inf))
        features = []
        for i in order:
            if not self.violation[i]:
                break
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [float(self.lon[i]), float(self.lat[i])],
                    },
                    "properties": {
                        "measurement_id": int(self.measurement_id[i]),
                        "measured_rsrp_dbm": float(self.rsrp[i]),
                        "claimed_minsignal_dbm": float(self.claimed[i]),
                        "gap_db": float(self.gap[i]),
                        "h3_res9_id": self.hexes.h3_ids[int(self.hex_index[i])],
                    },
                }
            )
        return {
            "type": "FeatureCollection",
            "properties": {
                "description": (
                    "Measurements whose RSRP is below the carrier's FCC-filed "
                    "claimed minimum for the hex they were taken in."
                ),
                "environment": self.hexes.environment,
                "count": len(features),
            },
            "features": features,
        }


def evaluate_claims(
    lon: np.ndarray,
    lat: np.ndarray,
    rsrp: np.ndarray,
    measurement_id: np.ndarray,
    hexes: ClaimedHexes,
) -> ClaimsResult:
    """Join measured points to claimed hexes and compute the per-point gap."""
    lon = np.asarray(lon, dtype=np.float64)
    lat = np.asarray(lat, dtype=np.float64)
    rsrp = np.asarray(rsrp, dtype=np.float64)
    measurement_id = np.asarray(measurement_id, dtype=np.int64)

    hex_index = match_points_to_hexes(lon, lat, hexes)
    claimed = np.full(lon.shape, np.nan)
    matched = hex_index >= 0
    if matched.any():
        claimed[matched] = hexes.minsignal[hex_index[matched]]
    gap = rsrp - claimed  # nan where unclaimed
    return ClaimsResult(
        hexes=hexes,
        lon=lon,
        lat=lat,
        rsrp=rsrp,
        measurement_id=measurement_id,
        hex_index=hex_index,
        claimed=claimed,
        gap=gap,
    )


def write_claims_outputs(
    out_dir: Path, result: ClaimsResult, *, prefix: str = "claims"
) -> "dict[str, Path]":
    """Write the JSON report and the violations GeoJSON. Returns {name: path}."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"{prefix}_report.json"
    geojson_path = out_dir / f"{prefix}_violations.geojson"
    report_path.write_text(json.dumps(result.summary(), indent=2))
    geojson_path.write_text(json.dumps(result.violations_geojson(), indent=2))
    return {"report": report_path, "violations": geojson_path}


def plain_language_summary(result: ClaimsResult) -> str:
    """The headline paragraph a human should read first."""
    s = result.summary()
    pts = s["points"]
    v = s["violations"]
    if pts["inside_claimed_hex"] == 0:
        return (
            f"None of the {pts['total']} measurements fall inside a claimed "
            f"hex (environment={s['environment']}) — nothing to compare."
        )
    lines = [
        f"{pts['total']} measurements; {pts['inside_claimed_hex']} inside a "
        f"claimed hex, {pts['outside_any_claim']} outside any claim "
        f"(environment={s['environment']}).",
        f"{v['count']} of {pts['inside_claimed_hex']} in-claim points "
        f"({v['share_of_inside']:.1%}) measured BELOW the filed minimum "
        f"signal for their hex.",
    ]
    if v["count"]:
        worst = v["worst_point"]
        lines.append(
            f"Average shortfall {v['avg_gap_db']:.1f} dB; worst "
            f"{v['worst_gap_db']:.1f} dB at ({worst['lat']:.5f}, "
            f"{worst['lon']:.5f}) — measured {worst['measured_rsrp_dbm']:.0f} "
            f"dBm where the filing claims ≥ "
            f"{worst['claimed_minsignal_dbm']:.0f} dBm "
            f"(hex {worst['h3_res9_id']})."
        )
    return "\n".join(lines)
