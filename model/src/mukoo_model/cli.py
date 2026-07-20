"""Command-line entry point: ``mukoo-krige``.

Prints the cross-validation numbers first — session (honest), block, then
random k-fold — then writes the GeoTIFF surfaces and JSON report. ``--compare``
CVs the candidate models (ordinary / path-loss / anisotropy scan) and exits
without writing surfaces.
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
        cv_block_m=args.block_m,
    )


def _parse_anisotropy(value: str) -> "tuple[float, float]":
    """--anisotropy SCALING:ANGLE, e.g. 2:60."""
    try:
        scaling_s, angle_s = value.split(":", 1)
        return float(scaling_s), float(angle_s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "expected SCALING:ANGLE, e.g. 2:60"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mukoo-krige",
        description="Kriging of RSRP over the measurements table.",
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
    parser.add_argument(
        "--block-m",
        type=float,
        default=Config.cv_block_m,
        help="tile size for spatial block CV (m)",
    )
    parser.add_argument(
        "--kriging",
        default="ordinary",
        choices=["ordinary", "pathloss"],
        help="ordinary kriging, or regression kriging with the path-loss prior",
    )
    parser.add_argument(
        "--anisotropy",
        type=_parse_anisotropy,
        default=None,
        metavar="SCALING:ANGLE",
        help="anisotropic variogram, e.g. 2:60 (ordinary kriging only)",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="CV-compare ordinary / pathloss / anisotropy scan, then exit",
    )
    args = parser.parse_args(argv)

    config = _build_config(args)
    prefix = f"{args.metric}_kriging"
    scaling, angle = args.anisotropy if args.anisotropy else (1.0, 0.0)

    def report_cv(cv: CVResult) -> None:
        # CV numbers first, to stdout, before the surface is even built.
        print(cv.summary())
        print()

    try:
        if args.compare:
            return _compare(config, args)
        result = run(
            config,
            metric=args.metric,
            where=args.where,
            prefix=prefix,
            kriging=args.kriging,
            anisotropy_scaling=scaling,
            anisotropy_angle=angle,
            on_cv=report_cv,
        )
    except Exception as exc:  # surface a clean message, not a traceback
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Surfaces + report written to {config.output_dir}:")
    for name, path in result.surface_paths.items():
        print(f"  {name:9s} {path}")
    print(f"  {'report':9s} {result.report_path}")
    return 0


def _compare(config: Config, args: argparse.Namespace) -> int:
    """CV table across candidate models; heavy imports stay off the fast path."""
    from .compare import compare_models
    from .data import load_rsrp_points
    from .db import make_engine

    cloud = load_rsrp_points(
        make_engine(config.database_url), metric=args.metric, where=args.where
    )
    rows = compare_models(cloud, config, folds=config.cv_folds)
    print(f"Model comparison ({rows[0][1].scheme}), best first:")
    print(f"{'model':38s} {'RMSE':>7s} {'MAE':>7s} {'R^2':>7s} {'w/in 1sd':>9s}")
    for label, cv in rows:
        print(
            f"{label:38s} {cv.rmse:7.3f} {cv.mae:7.3f} {cv.r2:7.3f} "
            f"{cv.within_1sigma:8.1%}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
