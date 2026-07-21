"""``mukoo-refresh``: re-run the model only when new measurements exist.

Closes the drive -> model -> suggestion loop without manual steps: a scheduler
(launchd/cron) runs this every few minutes; it compares the measurements
table's (count, max id) against the state saved by the previous run and exits
immediately when nothing changed. When data did change it re-runs the kriging
pipeline and the route suggester, then records the new state — so surfaces,
suggestions, and the GPX route stay current with what the phone has uploaded,
and an idle table costs one trivial SELECT.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .config import Config
from .db import make_engine

STATE_FILENAME = ".model_refresh_state.json"


def current_state(engine: Engine) -> dict:
    """The table fingerprint that decides whether a refresh is due."""
    with engine.connect() as conn:
        count, max_id = conn.execute(
            text("SELECT count(*), coalesce(max(id), 0) FROM measurements")
        ).one()
    return {"count": int(count), "max_id": int(max_id)}


def should_refresh(previous: "dict | None", current: dict) -> bool:
    """True when the table changed since the recorded state (or none exists).

    Compares both count and max id: count catches deletions, max id catches
    new rows even if a simultaneous deletion kept the count equal.
    """
    if not previous:
        return True
    return (
        previous.get("count") != current["count"]
        or previous.get("max_id") != current["max_id"]
    )


def load_state(path: Path) -> "dict | None":
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def save_state(path: Path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def run_refresh(
    config: Config, *, metric: str = "rsrp", force: bool = False
) -> str:
    """Refresh surfaces + suggestions if the data changed. Returns a log line."""
    # imported here so `mukoo-refresh` on an unchanged table stays instant.
    engine = make_engine(config.database_url)
    state_path = Path(config.output_dir) / STATE_FILENAME
    cur = current_state(engine)
    prev = load_state(state_path)

    if not force and not should_refresh(prev, cur):
        return f"up-to-date ({cur['count']} rows); nothing to do"

    from .pipeline import run
    from .suggest_pipeline import run_suggest

    result = run(config, metric=metric, prefix=f"{metric}_kriging")
    suggest = run_suggest(config, metric=metric)

    headline = result.cv
    save_state(
        state_path,
        {
            **cur,
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
            "kriging": config.kriging_mode,
            "cv_scheme": headline.scheme,
            "cv_rmse": round(headline.rmse, 3),
        },
    )
    return (
        f"refreshed: {cur['count']} rows -> surfaces + "
        f"{len(suggest.suggestions)} suggestions "
        f"({headline.scheme} RMSE {headline.rmse:.2f})"
    )


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mukoo-refresh",
        description="Re-run kriging + suggestions when new measurements exist.",
    )
    parser.add_argument("--metric", default="rsrp", choices=["rsrp", "rsrq", "sinr"])
    parser.add_argument("--force", action="store_true", help="refresh regardless")
    parser.add_argument("--out-dir", default=None, help="output dir (default ~/mukoo)")
    parser.add_argument(
        "--kriging",
        default=None,
        choices=["ordinary", "pathloss"],
        help="model to refresh with (default: MUKOO_KRIGING env, else ordinary)",
    )
    args = parser.parse_args(argv)

    config = Config.from_env()
    if args.out_dir:
        config = replace(config, output_dir=Path(args.out_dir))
    if args.kriging:
        config = replace(config, kriging_mode=args.kriging)

    try:
        message = run_refresh(config, metric=args.metric, force=args.force)
    except Exception as exc:  # one clean line; launchd logs capture it
        print(
            f"{datetime.now(timezone.utc).isoformat()} error: {exc}",
            file=sys.stderr,
        )
        return 1
    print(f"{datetime.now(timezone.utc).isoformat()} {message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
