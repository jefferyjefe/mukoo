"""Reproduce the FCC claims audit through a completely different stack.

``mukoo-claims`` reads the BDC GeoPackage with the Python standard library (no
GDAL) and does point-in-hex tests with Shapely's STRtree. That is a hand-written
path, and a hand-written path is exactly where a silent bug lives. This script
answers the same question with nothing in common with it:

    GDAL (``ogr2ogr``) loads the filed polygons into PostGIS, and the join is a
    SQL ``ST_Contains`` against the measurements table.

Different GeoPackage reader, different geometry engine, different spatial index,
different language for the predicate. If both paths report the same violation
counts, a bug would have to exist independently in both to survive — which is
the whole point of running it.

Nothing here imports ``mukoo_model``, SQLAlchemy, or Shapely, on purpose.

Usage::

    python docs/verify_claims_postgis.py --gpkg ~/Downloads/bdc_13_*.gpkg
    python docs/verify_claims_postgis.py --gpkg … --as-of 2026-07-22   # a snapshot
    python docs/verify_claims_postgis.py --gpkg … --json /tmp/verify.json

Requires the ``ogr2ogr`` binary on PATH (``brew install gdal`` /
``apt install gdal-bin``) and a PostGIS database holding the measurements.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Optional
from urllib.parse import unquote, urlparse

import psycopg2

DEFAULT_DATABASE_URL = "postgresql+psycopg2://mukoo:mukoo@localhost:5432/mukoo"

# In-vehicle mobile. BDC files mobile claims per environment, and drive data is
# in-vehicle, so this is the like-for-like comparison. (The column name really
# is missing its "e" in the FCC's own schema.)
DEFAULT_ENVIRONMENT = 1

# Staging table for the filed polygons. Dropped on exit unless --keep.
DEFAULT_TABLE = "bdc_claims_verify"


def _parse_url(url: str) -> dict:
    """Split a SQLAlchemy-style URL into libpq connection parts.

    The ``+psycopg2`` dialect suffix is SQLAlchemy's, not libpq's, so it has to
    come off before anything else can use the URL.
    """
    parsed = urlparse(url.replace("+psycopg2", "", 1))
    if parsed.scheme not in ("postgresql", "postgres"):
        raise ValueError(f"not a postgres URL: {url!r}")
    return {
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "dbname": (parsed.path or "/").lstrip("/") or "postgres",
        "user": unquote(parsed.username) if parsed.username else "",
        "password": unquote(parsed.password) if parsed.password else "",
    }


def _ogr_pg_target(parts: dict) -> str:
    """The ``PG:`` connection string ogr2ogr wants, from the same parts."""
    fields = [f"host={parts['host']}", f"port={parts['port']}", f"dbname={parts['dbname']}"]
    if parts["user"]:
        fields.append(f"user={parts['user']}")
    if parts["password"]:
        fields.append(f"password={parts['password']}")
    return "PG:" + " ".join(fields)


def measurement_bbox(conn, as_of: Optional[str]) -> tuple:
    """(minlon, minlat, maxlon, maxlat) of the measurements under test.

    Used to restrict what ogr2ogr loads. Restricting cannot change the answer:
    a hex that contains one of these points necessarily intersects their
    bounding box, so anything the filter drops could not have matched. It is the
    difference between loading ~20k polygons and all 1.5 million in the state
    file.

    ``as_of`` narrows *which* measurements are described, so it must never be
    allowed to narrow the load — see :func:`load_bbox`.
    """
    sql = """
        SELECT min(ST_X(geom)), min(ST_Y(geom)), max(ST_X(geom)), max(ST_Y(geom))
        FROM measurements
        WHERE rsrp IS NOT NULL
    """
    params: list = []
    if as_of:
        sql += " AND recorded_at < %s"
        params.append(as_of)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    if row is None or row[0] is None:
        raise SystemExit("error: no measurements with a non-null rsrp to verify")
    return tuple(float(v) for v in row)


def load_bbox(conn) -> tuple:
    """The bbox the claims are loaded for: *every* measurement, never a subset.

    Deliberately ignores ``--as-of``. Sizing the load to the snapshot under test
    makes the staging table silently wrong for any wider scope — and since the
    table survives across runs under ``--skip-load``, that error outlives the run
    that caused it. Loading the full extent once costs nothing extra and makes
    the table valid for every snapshot of this database.
    """
    return measurement_bbox(conn, None)


def _bbox_comment(bbox: tuple) -> str:
    return "mukoo-verify load bbox: " + ",".join(f"{v:.6f}" for v in bbox)


def record_load_bbox(conn, table: str, bbox: tuple) -> None:
    """Stamp the loaded extent onto the table so --skip-load can check it."""
    with conn.cursor() as cur:
        cur.execute(f"COMMENT ON TABLE {table} IS %s", (_bbox_comment(bbox),))


def check_loaded_bbox(conn, table: str, needed: tuple) -> None:
    """Refuse to reuse a staging table that does not cover ``needed``.

    A table loaded for a narrower bbox is missing hexes, and the join does not
    fail on a missing hex — it silently reports the point as outside any claim,
    which reads as a real finding. That is the one failure mode this whole
    script exists to rule out, so it is checked rather than trusted.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT obj_description(%s::regclass, 'pg_class')", (table,))
        row = cur.fetchone()
    stamp = row[0] if row else None
    prefix = "mukoo-verify load bbox: "
    if not stamp or not stamp.startswith(prefix):
        raise SystemExit(
            f"error: {table} carries no load-extent stamp, so --skip-load cannot "
            "confirm it covers these measurements. Re-run without --skip-load."
        )
    loaded = tuple(float(v) for v in stamp[len(prefix):].split(","))
    if not (
        loaded[0] <= needed[0]
        and loaded[1] <= needed[1]
        and loaded[2] >= needed[2]
        and loaded[3] >= needed[3]
    ):
        raise SystemExit(
            f"error: {table} was loaded for bbox {loaded}, which does not cover "
            f"the measurements' {needed}. Points outside it would be miscounted "
            "as unclaimed. Re-run without --skip-load."
        )


def load_claims(gpkg: str, parts: dict, table: str, environment: int, bbox: tuple) -> None:
    """ogr2ogr the filed polygons for one environment into PostGIS."""
    if shutil.which("ogr2ogr") is None:
        raise SystemExit(
            "error: ogr2ogr not found on PATH — install GDAL "
            "(brew install gdal / apt install gdal-bin)"
        )
    # A small pad so a hex straddling the bbox edge is still loaded whole. The
    # predicate is unaffected either way; this only avoids relying on how
    # ogr2ogr clips at the boundary.
    pad = 0.02
    cmd = [
        "ogr2ogr",
        "-f", "PostgreSQL",
        _ogr_pg_target(parts),
        gpkg,
        "-nln", table,
        "-nlt", "POLYGON",
        "-lco", "GEOMETRY_NAME=geom",
        "-lco", "FID=fid",
        "-overwrite",
        "-where", f"environmnt = {int(environment)}",
        "-spat", str(bbox[0] - pad), str(bbox[1] - pad),
        str(bbox[2] + pad), str(bbox[3] + pad),
        "--config", "OGR_TRUNCATE", "YES",
    ]
    print(f"  loading claims via ogr2ogr (environmnt={environment})…", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit(f"error: ogr2ogr failed with exit {proc.returncode}")


def join_and_summarise(conn, table: str, as_of: Optional[str]) -> dict:
    """The measured-vs-claimed comparison, entirely in SQL."""
    where_time = " AND m.recorded_at < %(as_of)s" if as_of else ""
    params = {"as_of": as_of} if as_of else {}

    # DISTINCT ON keeps one row per measurement. H3 cells tile without overlap,
    # so this should never actually collapse anything; it is here so that if the
    # filing ever does contain overlapping claims, the count stays a count of
    # measurements rather than of measurement-hex pairs.
    conn.cursor().execute(f"ANALYZE {table}")
    sql = f"""
        WITH m AS (
            SELECT m.id, m.geom, m.rsrp::double precision AS rsrp
            FROM measurements m
            WHERE m.rsrp IS NOT NULL{where_time}
        ),
        j AS (
            SELECT DISTINCT ON (m.id)
                   m.id, m.rsrp,
                   c.minsignal::double precision AS claimed,
                   m.rsrp::double precision - c.minsignal::double precision AS gap
            FROM m
            JOIN {table} c ON ST_Contains(c.geom, m.geom)
            ORDER BY m.id, c.minsignal DESC
        )
        SELECT
            (SELECT count(*) FROM m)                                   AS total,
            (SELECT count(*) FROM j)                                   AS inside,
            (SELECT count(*) FROM j WHERE gap < 0)                     AS violations,
            (SELECT round(avg(gap)::numeric, 2) FROM j WHERE gap < 0)  AS avg_gap,
            (SELECT round(min(gap)::numeric, 2) FROM j WHERE gap < 0)  AS worst_gap
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        total, inside, violations, avg_gap, worst_gap = cur.fetchone()

        # Per-tier breakdown, so a shortfall concentrated in one claim level
        # cannot hide inside the headline.
        cur.execute(
            f"""
            WITH m AS (
                SELECT m.id, m.geom, m.rsrp::double precision AS rsrp
                FROM measurements m
                WHERE m.rsrp IS NOT NULL{where_time}
            ),
            j AS (
                SELECT DISTINCT ON (m.id)
                       m.id, m.rsrp,
                       c.minsignal::double precision AS claimed
                FROM m
                JOIN {table} c ON ST_Contains(c.geom, m.geom)
                ORDER BY m.id, c.minsignal DESC
            )
            SELECT claimed, count(*), count(*) FILTER (WHERE rsrp < claimed),
                   round(avg(rsrp)::numeric, 1)
            FROM j GROUP BY claimed ORDER BY claimed
            """,
            params,
        )
        tiers = [
            {
                "claimed_minsignal_dbm": float(c),
                "points": int(n),
                "below_claim": int(b),
                "avg_measured_dbm": float(a),
            }
            for c, n, b, a in cur.fetchall()
        ]

        # Boundary semantics. mukoo-claims uses Shapely `intersects` (a point on
        # a shared edge counts as inside); ST_Contains excludes the boundary. On
        # float coordinates the two should never disagree, but "should" is what
        # this script exists to stop trusting — so measure it instead.
        cur.execute(
            f"""
            WITH m AS (
                SELECT m.id, m.geom FROM measurements m
                WHERE m.rsrp IS NOT NULL{where_time}
            )
            SELECT count(*) FROM m
            WHERE EXISTS (SELECT 1 FROM {table} c WHERE ST_Intersects(c.geom, m.geom))
              AND NOT EXISTS (SELECT 1 FROM {table} c WHERE ST_Contains(c.geom, m.geom))
            """,
            params,
        )
        boundary_only = int(cur.fetchone()[0])

        cur.execute(f"SELECT count(*) FROM {table}")
        n_hexes = int(cur.fetchone()[0])

    return {
        "n_claimed_hexes_loaded": n_hexes,
        "points": {
            "total": int(total),
            "inside_claimed_hex": int(inside),
            "outside_any_claim": int(total) - int(inside),
        },
        "violations": {
            "count": int(violations),
            "share_of_all": round(violations / total, 4) if total else None,
            "share_of_inside": round(violations / inside, 4) if inside else None,
            "avg_gap_db": float(avg_gap) if avg_gap is not None else None,
            "worst_gap_db": float(worst_gap) if worst_gap is not None else None,
        },
        "by_claimed_minsignal": tiers,
        "boundary_only_points": boundary_only,
    }


def print_report(result: dict, as_of: Optional[str]) -> None:
    p, v = result["points"], result["violations"]
    scope = f"recorded before {as_of}" if as_of else "all measurements"
    print()
    print(f"  Claimed hexes loaded : {result['n_claimed_hexes_loaded']:,}")
    print(f"  Scope                : {scope}")
    print()
    print(f"  Measurements         : {p['total']:,}")
    print(f"    inside a claim     : {p['inside_claimed_hex']:,}")
    print(f"    outside any claim  : {p['outside_any_claim']:,}")
    print()
    print(f"  Violations           : {v['count']:,}")
    # Percentages are computed from the counts, not from the rounded shares in
    # the JSON: rounding to 4dp and then again to 1dp turns 82.85% into 82.8%.
    if p["total"]:
        print(f"    share of all       : {100.0 * v['count'] / p['total']:.1f}%")
    if p["inside_claimed_hex"]:
        print(f"    share of in-claim  : {100.0 * v['count'] / p['inside_claimed_hex']:.1f}%")
    print(f"    avg gap            : {v['avg_gap_db']} dB")
    print(f"    worst gap          : {v['worst_gap_db']} dB")
    print()
    print("  Claimed min   Points   Below   Avg measured")
    for t in result["by_claimed_minsignal"]:
        print(
            f"  {t['claimed_minsignal_dbm']:>10.0f}   "
            f"{t['points']:>6,}   {t['below_claim']:>5,}   "
            f"{t['avg_measured_dbm']:>10.1f} dBm"
        )
    print()
    n = result["boundary_only_points"]
    verdict = "none — boundary semantics do not affect this result" if n == 0 else f"{n:,}"
    print(f"  Points on a hex boundary (ST_Intersects but not ST_Contains): {verdict}")


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify_claims_postgis",
        description="Reproduce the FCC claims audit via ogr2ogr -> PostGIS -> ST_Contains.",
    )
    parser.add_argument("--gpkg", required=True, help="path to the BDC mobile H3 GeoPackage")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        help="PostGIS URL holding the measurements (default: $DATABASE_URL)",
    )
    parser.add_argument(
        "--environment",
        type=int,
        default=DEFAULT_ENVIRONMENT,
        help="BDC environmnt code: 1 = in-vehicle mobile (default), 0 = outdoor stationary",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        metavar="DATE",
        help="only measurements recorded strictly before this timestamp, "
        "for reproducing an earlier snapshot of the dataset",
    )
    parser.add_argument("--table", default=DEFAULT_TABLE, help="staging table name")
    parser.add_argument(
        "--skip-load",
        action="store_true",
        help="reuse an already-loaded staging table instead of re-running ogr2ogr",
    )
    parser.add_argument("--keep", action="store_true", help="do not drop the staging table")
    parser.add_argument("--json", dest="json_path", default=None, help="also write the result as JSON")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.gpkg):
        print(f"error: no such GeoPackage: {args.gpkg}", file=sys.stderr)
        return 1

    parts = _parse_url(args.database_url)
    conn = psycopg2.connect(**{k: v for k, v in parts.items() if v})
    conn.autocommit = True
    try:
        print(f"  Database             : {parts['dbname']} on {parts['host']}:{parts['port']}")
        scope_bbox = measurement_bbox(conn, args.as_of)
        full_bbox = load_bbox(conn)
        print(
            f"  Measurement bbox     : {scope_bbox[0]:.4f},{scope_bbox[1]:.4f}"
            f" .. {scope_bbox[2]:.4f},{scope_bbox[3]:.4f}"
        )
        if args.skip_load:
            check_loaded_bbox(conn, args.table, scope_bbox)
        else:
            load_claims(args.gpkg, parts, args.table, args.environment, full_bbox)
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE INDEX IF NOT EXISTS ix_{args.table}_geom "
                    f"ON {args.table} USING gist (geom)"
                )
            record_load_bbox(conn, args.table, full_bbox)
        result = join_and_summarise(conn, args.table, args.as_of)
        result["source"] = {
            "gpkg": os.path.abspath(args.gpkg),
            "environment": args.environment,
            "as_of": args.as_of,
            "method": "ogr2ogr -> PostGIS -> ST_Contains",
        }
        print_report(result, args.as_of)
        if args.json_path:
            with open(args.json_path, "w") as fh:
                json.dump(result, fh, indent=2)
            print(f"\n  wrote {args.json_path}")
    finally:
        if not args.keep:
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {args.table}")
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
