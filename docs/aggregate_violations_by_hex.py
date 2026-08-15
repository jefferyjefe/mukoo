"""Aggregate the claim-violation evidence to the FCC hex level for publishing.

The repo publishes *where the carrier's filing was contradicted*, not where the
vehicle went. This turns the per-measurement audit into one feature per H3 res-9
hex: the carrier's own claimed-coverage cell (public geography from the BDC
filing), carrying how many measurements fell in it, how many came in below the
filed minimum, and the size of the gap. No individual GPS coordinates, no
timestamps, no per-point ids — a reader learns which claimed cells failed and by
how much, but cannot reconstruct the drive path.

Geometry is the H3 hex polygon straight from the BDC GeoPackage, so the only
coordinates in the output are the FCC's fixed hex boundaries.

Hexes holding fewer than ``--min-measurements`` readings are withheld from the
file: every per-hex figure here is a rate or a mean, and on one or two readings
neither is one (see :data:`DEFAULT_MIN_MEASUREMENTS`). That floor governs what is
*published*; the audit totals in the header, and claims_report.json, are computed
over every measurement regardless.

Usage (after ``mukoo-claims`` has produced its report)::

    pip install -e 'model[viz]'
    python docs/aggregate_violations_by_hex.py            # defaults from ~/mukoo
    python docs/aggregate_violations_by_hex.py --gpkg path/to/bdc_*.gpkg
    python docs/aggregate_violations_by_hex.py --as-of 2026-07-22   # a snapshot

Writes verizon_claim_violations_by_hex.geojson at the repo root and aborts if its
totals disagree with claims_report.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import geopandas as gpd
from shapely.geometry import mapping
from sqlalchemy import create_engine

from mukoo_model.config import DEFAULT_DATABASE_URL, DEFAULT_OUTPUT_DIR

REPO_ROOT = Path(__file__).resolve().parent.parent

# Smallest number of measurements a hex needs before its per-hex figures are
# published. This is a statistical floor, not a privacy one.
#
# Every published per-hex number is a *rate* or a *mean* — violation_share,
# avg_measured_dbm, avg_gap_db. At n=1 a share can only be 0 or 1, and the mean
# is a single reading: there is no way to tell a genuine coverage shortfall from
# one momentary fade, yet the feature reads with the same authority as a hex
# holding 73 measurements. At n=2 the share is still quantised to 0, 0.5, 1.
# Three is the smallest n at which the share can take an intermediate value and
# at which one anomalous reading cannot drive it to 1.0 on its own.
#
# The dropped hexes are not dropped from the audit — the headline counts come
# from claims_report.json, which is computed over every measurement. They are
# withheld from the *per-hex* file, where a rate on n<3 would be over-read.
DEFAULT_MIN_MEASUREMENTS = 3


def round_coords(geom, ndigits=6):
    """Round a geometry's coordinates (public hex boundaries) for a tidy file."""
    gj = mapping(geom)

    def _round(seq):
        return [
            _round(x) if isinstance(x, (list, tuple)) else round(x, ndigits)
            for x in seq
        ]

    gj["coordinates"] = _round(gj["coordinates"])
    return gj


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--report", type=Path,
                        default=DEFAULT_OUTPUT_DIR / "claims_report.json")
    parser.add_argument("--gpkg", type=Path, default=None,
                        help="BDC GeoPackage (default: the one the report used)")
    parser.add_argument("--out", type=Path,
                        default=REPO_ROOT / "verizon_claim_violations_by_hex.geojson")
    parser.add_argument("--min-measurements", type=int,
                        default=DEFAULT_MIN_MEASUREMENTS, metavar="N",
                        help="publish a hex only if it holds at least N "
                             f"measurements (default: {DEFAULT_MIN_MEASUREMENTS}); "
                             "1 publishes every hex")
    parser.add_argument("--as-of", default=None, metavar="TIMESTAMP",
                        help="only measurements recorded strictly before this, "
                             "to reproduce the snapshot claims_report.json "
                             "describes when the table has grown since")
    args = parser.parse_args()

    # Outputs default to MUKOO_MODEL_OUTPUT_DIR (else ~/mukoo), which is *not*
    # the checkout unless you happen to have cloned there — so say where we
    # looked. A bare FileNotFoundError traceback sends people hunting for a bug
    # in the script instead of running the step that produces the file.
    if not args.report.is_file():
        sys.exit(
            f"no claims report at {args.report}\n"
            "Run `mukoo-claims --gpkg <bdc.gpkg>` first, or pass --report. "
            "Outputs go to $MUKOO_MODEL_OUTPUT_DIR (default ~/mukoo), not the "
            "repository."
        )
    report = json.loads(args.report.read_text())
    gpkg = args.gpkg or Path(report["gpkg"])
    environment = report["environment"]

    engine = create_engine(os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
    # The report is a snapshot; the table keeps growing. Without --as-of this
    # aggregates whatever is in the table now, and the totals check below is
    # what stops a newer table being published under an older report's numbers.
    sql = "SELECT rsrp::float8 AS rsrp, geom FROM measurements WHERE rsrp IS NOT NULL"
    if args.as_of:
        sql += f" AND recorded_at < '{args.as_of}'"
    points = gpd.read_postgis(sql, engine, geom_col="geom")
    # An empty frame's total_bounds is all-NaN, and that NaN reaches GDAL as the
    # bbox filter, which fails deep inside pyogrio with an unreadable SQL error.
    # Catch it here, where we still know what it means.
    if points.empty:
        sys.exit(
            "no measurements with a non-null rsrp in the database"
            + (f" before {args.as_of}" if args.as_of else "")
            + " — nothing to aggregate"
        )

    minx, miny, maxx, maxy = points.total_bounds
    pad = 0.02
    hexes = gpd.read_file(
        gpkg, where=f"environmnt = {environment}",
        bbox=(minx - pad, miny - pad, maxx + pad, maxy + pad))

    joined = gpd.sjoin(points, hexes, predicate="within", how="inner")
    joined["gap"] = joined["rsrp"] - joined["minsignal"]
    viol = joined[joined["gap"] < 0]

    grp = joined.groupby("h3_res9_id")
    vgrp = viol.groupby("h3_res9_id")
    stats = gpd.pd.DataFrame({
        "claimed_minsignal_dbm": grp["minsignal"].first().astype(int),
        "n_measurements": grp.size().astype(int),
        "avg_measured_dbm": grp["rsrp"].mean().round(1),
        "worst_measured_dbm": grp["rsrp"].min(),
        "n_violations": vgrp.size().reindex(grp.size().index).fillna(0).astype(int),
        "avg_gap_db": vgrp["gap"].mean().round(2),
        "worst_gap_db": vgrp["gap"].min(),
    })
    stats = stats[stats["n_violations"] >= 1].copy()
    stats["violation_share"] = (stats["n_violations"] / stats["n_measurements"]).round(4)

    # Refuse to publish totals the shipped audit doesn't back. Checked *before*
    # the publication floor: the floor decides what gets published, never what
    # was found, so it must not be able to talk this check into passing.
    tot_viol, want_viol = int(stats["n_violations"].sum()), report["violations"]["count"]
    tot_inside, want_inside = int(joined.shape[0]), report["points"]["inside_claimed_hex"]
    if tot_viol != want_viol or tot_inside != want_inside:
        print(f"MISMATCH violations map={tot_viol} report={want_viol}; "
              f"inside map={tot_inside} report={want_inside}", file=sys.stderr)
        sys.exit("aggregate disagrees with claims_report.json — re-run mukoo-claims, "
                 "or pass --as-of to pin to the snapshot the report describes")

    # Publication floor (see DEFAULT_MIN_MEASUREMENTS): a per-hex rate computed
    # on one or two readings is not a rate anyone should read.
    n_hexes_before = int(len(stats))
    if args.min_measurements > 1:
        stats = stats[stats["n_measurements"] >= args.min_measurements].copy()
    pub_viol = int(stats["n_violations"].sum())
    pub_meas = int(stats["n_measurements"].sum())

    geom_by_hex = hexes.set_index("h3_res9_id").geometry
    carrier = str(hexes["brandname"].iloc[0]) if "brandname" in hexes else "carrier"

    # worst hex first
    stats = stats.sort_values("worst_gap_db")
    features = []
    for h3_id, row in stats.iterrows():
        features.append({
            "type": "Feature",
            "geometry": round_coords(geom_by_hex.loc[h3_id]),
            "properties": {
                "h3_res9_id": h3_id,
                "claimed_minsignal_dbm": int(row["claimed_minsignal_dbm"]),
                "n_measurements": int(row["n_measurements"]),
                "n_violations": int(row["n_violations"]),
                "violation_share": float(row["violation_share"]),
                "avg_measured_dbm": float(row["avg_measured_dbm"]),
                "worst_measured_dbm": float(row["worst_measured_dbm"]),
                "avg_gap_db": float(row["avg_gap_db"]),
                "worst_gap_db": float(row["worst_gap_db"]),
            },
        })

    fc = {
        "type": "FeatureCollection",
        "name": "verizon_claim_violations_by_hex",
        "properties": {
            "description": (
                f"Measurements vs {carrier}'s FCC-filed minimum signal, aggregated "
                "to the carrier's own H3 res-9 claimed-coverage hexes. One feature "
                "per hex that contained at least one below-claim measurement. "
                "Individual GPS points, timestamps, and per-point ids are "
                "intentionally omitted; the only coordinates are the FCC filing's "
                "fixed hex boundaries, so the drive path cannot be reconstructed. "
                "Full-precision point data stays local (see mukoo-claims)."
            ),
            "environment": environment,
            # A hex is published only if it holds at least this many
            # measurements: below it, violation_share and the per-hex means are
            # too few readings to be read as a rate. A statistical floor on what
            # is publishable, not a restriction on what was measured — the audit
            # totals below cover every measurement either way.
            "min_measurements_per_hex": args.min_measurements,
            # The audit, over every measurement — the numbers claims_report.json
            # is checked against, unaffected by the floor.
            "n_violations": tot_viol,
            "n_measurements_in_claimed_hexes": tot_inside,
            "n_hexes_with_a_violation": n_hexes_before,
            # What this file actually carries, after the floor.
            "n_hexes": len(features),
            "n_violations_published": pub_viol,
            "n_measurements_published": pub_meas,
        },
        "features": features,
    }
    args.out.write_text(json.dumps(fc, indent=2))
    print(f"wrote {args.out}")
    print(f"audit: {tot_viol} violations over {tot_inside} in-claim measurements "
          f"in {n_hexes_before} hexes")
    print(f"published: {len(features)} hexes (floor n>={args.min_measurements}, "
          f"{n_hexes_before - len(features)} withheld) · {pub_viol} violations "
          f"· {pub_meas} measurements")


if __name__ == "__main__":
    main()
