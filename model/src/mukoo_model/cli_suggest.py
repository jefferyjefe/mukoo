"""Command-line entry point: ``mukoo-suggest``.

Suggests where to drive next to most reduce the model's uncertainty, then writes
the ranked targets as GeoJSON. Decision support only — it proposes, you choose.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from .config import Config
from .suggest import (
    DEFAULT_CANDIDATE_QUANTILE,
    DEFAULT_MAX_ROAD_DIST_M,
    DEFAULT_MIN_SEPARATION_M,
    DEFAULT_TOP_N,
    DEFAULT_WEAK_BIAS,
)
from .suggest_pipeline import format_suggestions, run_suggest


def _build_config(args: argparse.Namespace) -> Config:
    config = Config.from_env()
    if args.database_url:
        config = replace(config, database_url=args.database_url)
    if args.out_dir:
        config = replace(config, output_dir=Path(args.out_dir))
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mukoo-suggest",
        description="Suggest drive targets that most reduce kriging uncertainty.",
    )
    parser.add_argument("--database-url", default=None, help="override DATABASE_URL")
    parser.add_argument("--out-dir", default=None, help="output dir (default: ~/mukoo)")
    parser.add_argument("--metric", default="rsrp", choices=["rsrp", "rsrq", "sinr"])
    parser.add_argument(
        "--stddev-tif",
        default=None,
        help="uncertainty GeoTIFF (default: <out-dir>/<metric>_kriging_stddev.tif)",
    )
    parser.add_argument(
        "--recompute",
        action="store_true",
        help="refit the model from PostGIS instead of reading the GeoTIFF",
    )
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument(
        "--candidate-quantile",
        type=float,
        default=DEFAULT_CANDIDATE_QUANTILE,
        help="only cells at/above this uncertainty quantile are candidates",
    )
    parser.add_argument(
        "--max-road-dist-m",
        type=float,
        default=DEFAULT_MAX_ROAD_DIST_M,
        help="discard candidates with no road within this distance",
    )
    parser.add_argument(
        "--min-separation-m",
        type=float,
        default=DEFAULT_MIN_SEPARATION_M,
        help="minimum spacing between suggestions (spreads them out)",
    )
    parser.add_argument(
        "--weak-bias",
        type=float,
        default=DEFAULT_WEAK_BIAS,
        help="how strongly predicted-weak coverage boosts a target (0 = off)",
    )
    parser.add_argument("--cache-dir", default=None, help="OSM road cache dir")
    parser.add_argument(
        "--refresh-roads", action="store_true", help="force re-fetch of OSM roads"
    )
    parser.add_argument(
        "--network-type",
        default="drive",
        help="osmnx network type (drive, drive_service, all, ...)",
    )
    args = parser.parse_args(argv)

    config = _build_config(args)
    try:
        result = run_suggest(
            config,
            metric=args.metric,
            stddev_tif=Path(args.stddev_tif) if args.stddev_tif else None,
            recompute=args.recompute,
            top_n=args.top_n,
            candidate_quantile=args.candidate_quantile,
            max_road_dist_m=args.max_road_dist_m,
            min_separation_m=args.min_separation_m,
            weak_bias=args.weak_bias,
            cache_dir=Path(args.cache_dir) if args.cache_dir else None,
            refresh_roads=args.refresh_roads,
            network_type=args.network_type,
        )
    except Exception as exc:  # clean message, not a traceback
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Uncertainty surface: {result.stddev_source}")
    # Tile count matters because it is budgeted, and a run that hit the cap has
    # to be distinguishable from one that did not: it fetched roads for only
    # part of the candidate ground, so its ranking is against a partial network
    # and the targets it left out were never considered at all.
    tiles = f"from {result.n_road_tiles} tile(s)"
    if result.n_road_tiles_wanted > result.n_road_tiles:
        tiles += (
            f" of {result.n_road_tiles_wanted} — TILE CAP HIT: targets were "
            f"ranked against a partial road network"
        )
    print(f"Road network: {result.n_roads} OSM edges {tiles}")
    scoring = (
        f"EVR (range {result.range_m:.0f} m), weak-bias {args.weak_bias:g}"
        if result.range_m
        else "raw sigma (no variogram range available)"
    )
    print(f"Scoring: {scoring}")
    print()
    print(format_suggestions(result.suggestions, metric=args.metric))
    print()
    print(f"Wrote {len(result.suggestions)} suggestions -> {result.output_path}")
    if result.gpx_path:
        print(f"Wrote GPX route -> {result.gpx_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
