"""Command-line entry point: ``mukoo-krige``.

Prints the cross-validation numbers first (they decide whether to trust the
surface), then writes the GeoTIFF surfaces and JSON report to the output dir.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace

from .config import Config
from .crossval import CVResult
from .pipeline import run


def _build_config(args: argparse.Namespace) -> Config:
    config = Config.from_env()
    if args.database_url:
        config = replace(config, database_url=args.database_url)
    if args.out_dir:
        config = replace(config, output_dir=args.out_dir)
    return replace(
        config,
        cell_metres=args.cell_m,
        variogram_model=args.variogram_model,
        nlags=args.nlags,
        cv_folds=args.folds,
        cv_seed=args.seed,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mukoo-krige",
        description="Ordinary kriging of RSRP over the measurements table.",
    )
    parser.add_argument("--database-url", default=None, help="override DATABASE_URL")
    parser.add_argument(
        "--out-dir", default=None, help="output directory (default: ~/mukoo)"
    )
    parser.add_argument(
        "--metric",
        default="rsrp",
        choices=["rsrp", "rsrq", "sinr"],
        help="signal column to interpolate (default: rsrp)",
    )
    parser.add_argument("--where", default=None, help="extra SQL predicate (ANDed)")
    parser.add_argument(
        "--cell-m", type=float, default=Config.cell_metres, help="grid cell size (m)"
    )
    parser.add_argument(
        "--variogram-model",
        default=Config.variogram_model,
        choices=["spherical", "exponential", "gaussian", "linear", "power"],
    )
    parser.add_argument("--nlags", type=int, default=Config.nlags)
    parser.add_argument(
        "--folds", type=int, default=Config.cv_folds, help="k for k-fold CV"
    )
    parser.add_argument("--seed", type=int, default=Config.cv_seed)
    args = parser.parse_args(argv)

    config = _build_config(args)
    prefix = f"{args.metric}_kriging"

    def report_cv(cv: CVResult) -> None:
        # CV numbers first, to stdout, before the surface is even built.
        print(cv.summary())
        print()

    try:
        result = run(
            config, metric=args.metric, where=args.where, prefix=prefix, on_cv=report_cv
        )
    except Exception as exc:  # surface a clean message, not a traceback
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Surfaces + report written to {config.output_dir}:")
    for name, path in result.surface_paths.items():
        print(f"  {name:9s} {path}")
    print(f"  {'report':9s} {result.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
