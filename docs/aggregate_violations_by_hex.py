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

Usage (after ``mukoo-claims`` has produced its report)::

    pip install -e 'model[viz]'
    python docs/aggregate_violations_by_hex.py            # defaults from ~/mukoo
    python docs/aggregate_violations_by_hex.py --gpkg path/to/bdc_*.gpkg

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
    args = parser.parse_args()

    report = json.loads(args.report.read_text())
    gpkg = args.gpkg or Path(report["gpkg"])
    environment = report["environment"]

    engine = create_engine(os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
    points = gpd.read_postgis(
        "SELECT rsrp::float8 AS rsrp, geom FROM measurements WHERE rsrp IS NOT NULL",
        engine, geom_col="geom")

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

    # refuse to publish totals the shipped audit doesn't back
    tot_viol, want_viol = int(stats["n_violations"].sum()), report["violations"]["count"]
    tot_inside, want_inside = int(joined.shape[0]), report["points"]["inside_claimed_hex"]
    if tot_viol != want_viol or tot_inside != want_inside:
        print(f"MISMATCH violations map={tot_viol} report={want_viol}; "
              f"inside map={tot_inside} report={want_inside}", file=sys.stderr)
        sys.exit("aggregate disagrees with claims_report.json — re-run mukoo-claims first")

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
            "n_hexes": len(features),
            "n_violations": tot_viol,
            "n_measurements_in_claimed_hexes": tot_inside,
        },
        "features": features,
    }
    args.out.write_text(json.dumps(fc, indent=2))
    print(f"wrote {args.out}")
    print(f"{len(features)} hexes · {tot_viol} violations · {tot_inside} in-claim measurements")


if __name__ == "__main__":
    main()
