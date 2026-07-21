"""Command-line entry point: ``mukoo-claims``.

Compares every measured point against the carrier's FCC-filed coverage claim
(a BDC mobile H3 GeoPackage) and reports where the measurements contradict the
filing. Writes a JSON report plus a GeoJSON of the violating points, and prints
the plain-language verdict.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
from sqlalchemy import text

from .claims import (
    evaluate_claims,
    plain_language_summary,
    read_claimed_hexes,
    write_claims_outputs,
)
from .config import Config
from .db import make_engine

# Padding (degrees, ~1 km) around the measurement bbox when pre-filtering
# hexes, so hexes straddling the edge of the driven area are kept whole.
BBOX_MARGIN_DEG = 0.01


def _load_measurements(
    database_url: str, *, where: "str | None" = None
) -> "tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]":
    """(id, lon, lat, rsrp) for every non-null-RSRP measurement, by id.

    Raw rows, deliberately not the collapsed kriging cloud: a claims report
    should count every individual sample the phone recorded.
    """
    predicate = "rsrp IS NOT NULL"
    if where:
        predicate = f"({predicate}) AND ({where})"
    sql = text(
        f"SELECT id, ST_X(geom) AS lon, ST_Y(geom) AS lat, rsrp "
        f"FROM measurements WHERE {predicate} ORDER BY id"
    )
    engine = make_engine(database_url)
    with engine.connect() as conn:
        rows = conn.execute(sql).all()
    if not rows:
        raise ValueError("no measurements with non-null rsrp" + (f" matching {where!r}" if where else ""))
    ids = np.array([r.id for r in rows], dtype=np.int64)
    lon = np.array([r.lon for r in rows], dtype=np.float64)
    lat = np.array([r.lat for r in rows], dtype=np.float64)
    rsrp = np.array([float(r.rsrp) for r in rows], dtype=np.float64)
    return ids, lon, lat, rsrp


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mukoo-claims",
        description=(
            "Compare measured RSRP against an FCC BDC claimed-coverage filing."
        ),
    )
    parser.add_argument(
        "--gpkg",
        required=True,
        help="path to the BDC mobile H3 GeoPackage (per-provider filing)",
    )
    parser.add_argument(
        "--environment",
        type=int,
        default=1,
        choices=[0, 1],
        help="BDC environmnt to compare against: 1 = in-vehicle mobile "
        "(matches drive data; default), 0 = outdoor stationary",
    )
    parser.add_argument("--database-url", default=None, help="override DATABASE_URL")
    parser.add_argument("--where", default=None, help="extra SQL predicate (ANDed)")
    parser.add_argument("--out-dir", default=None, help="output dir (default ~/mukoo)")
    parser.add_argument(
        "--prefix",
        default="claims",
        help="output filename prefix (default: claims -> claims_report.json, "
        "claims_violations.geojson)",
    )
    args = parser.parse_args(argv)

    config = Config.from_env()
    if args.database_url:
        config = replace(config, database_url=args.database_url)
    if args.out_dir:
        config = replace(config, output_dir=Path(args.out_dir))

    try:
        ids, lon, lat, rsrp = _load_measurements(
            config.database_url, where=args.where
        )
        bbox = (
            float(lon.min()) - BBOX_MARGIN_DEG,
            float(lat.min()) - BBOX_MARGIN_DEG,
            float(lon.max()) + BBOX_MARGIN_DEG,
            float(lat.max()) + BBOX_MARGIN_DEG,
        )
        hexes = read_claimed_hexes(
            Path(args.gpkg), environment=args.environment, bbox=bbox
        )
        result = evaluate_claims(lon, lat, rsrp, ids, hexes)
        paths = write_claims_outputs(
            config.output_dir, result, prefix=args.prefix
        )
    except Exception as exc:  # clean message, not a traceback
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"Filing: {hexes.gpkg_path}\n"
        f"Hexes in the measured area (environmnt={hexes.environment}): "
        f"{hexes.n} (of {hexes.n_scanned} scanned)\n"
    )
    print(plain_language_summary(result))
    print()
    for name, path in paths.items():
        print(f"  {name:10s} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
