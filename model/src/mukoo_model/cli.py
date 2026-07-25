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

from .config import Config, parse_anisotropy
from .crossval import CVResult
from .pipeline import run


def _build_config(args: argparse.Namespace) -> Config:
    config = Config.from_env()
    if args.database_url:
        config = replace(config, database_url=args.database_url)
    if args.out_dir:
        config = replace(config, output_dir=args.out_dir)
    if args.kriging:
        config = replace(config, kriging_mode=args.kriging)
    if args.anisotropy:
        config = replace(config, anisotropy=args.anisotropy)
    if args.none_floor is not None:
        config = replace(config, none_floor=args.none_floor)
    if args.dedupe_runs is not None:
        config = replace(config, dedupe_runs=args.dedupe_runs)
    if args.lag_spacing:
        config = replace(config, lag_spacing=args.lag_spacing)
    if args.max_lag_m is not None:
        config = replace(config, max_lag_m=args.max_lag_m)
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
        return parse_anisotropy(value)
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
        "--lag-spacing",
        default=None,
        choices=["log", "linear", "pykrige"],
        help="how variogram lag bins are spaced: log (default) resolves short "
        "lags so the nugget is measured; pykrige = equal-width bins over the "
        "full range, which fits the nugget to ~0 (default: MUKOO_LAG_SPACING)",
    )
    parser.add_argument(
        "--max-lag-m",
        type=float,
        default=None,
        metavar="M",
        help="longest lag used to fit the variogram (default: all pairs). "
        "Only helps if the variogram plateaus below the cap",
    )
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
        default=None,
        choices=["ordinary", "pathloss"],
        help="ordinary kriging, or regression kriging with the path-loss "
        "prior (default: MUKOO_KRIGING env, else ordinary)",
    )
    parser.add_argument(
        "--anisotropy",
        type=_parse_anisotropy,
        default=None,
        metavar="SCALING:ANGLE",
        help="anisotropic variogram, e.g. 2:60 (ordinary kriging only; "
        "default: MUKOO_ANISOTROPY env, else isotropic)",
    )
    parser.add_argument(
        "--none-floor",
        type=float,
        default=None,
        metavar="DBM",
        help="also load network_type='none' dead-zone rows at this RSRP floor "
        "(e.g. -127; default: MUKOO_NONE_FLOOR env, else excluded)",
    )
    parser.add_argument(
        "--dedupe-runs",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="collapse runs of consecutive samples with an identical "
        "(rsrp, rsrq, sinr) triple within a session to their first sample — "
        "the logger samples faster than the modem refreshes "
        "(default: MUKOO_DEDUPE_RUNS env, else off)",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="CV-compare ordinary / pathloss / anisotropy scan, then exit",
    )
    args = parser.parse_args(argv)

    config = _build_config(args)
    prefix = f"{args.metric}_kriging"

    def report_cv(cv: CVResult) -> None:
        # CV numbers first, to stdout, before the surface is even built.
        print(cv.summary())
        print()

    try:
        if args.compare:
            return _compare(config, args)
        # kriging mode / anisotropy / none-floor all live on the config now
        # (CLI flags > env > defaults), so run() just reads them from there.
        result = run(
            config,
            metric=args.metric,
            where=args.where,
            prefix=prefix,
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
        make_engine(config.database_url),
        metric=args.metric,
        where=args.where,
        dedupe_runs=config.dedupe_runs,
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
