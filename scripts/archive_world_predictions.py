"""Plan or publish verified Parquet mirrors of closed World prediction days.

This command is export-only.  It never deletes, updates, vacuums, or replaces
``world_model.db``.  Omit ``--apply`` for a read-only plan.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path

from trader.application.world_model.prediction_archive import WorldPredictionArchiveService
from trader.infrastructure.state_db.world_prediction_archive_source import (
    SQLiteWorldPredictionArchiveSource,
)
from trader.infrastructure.state_db.world_prediction_parquet_store import (
    DuckDbWorldPredictionParquetStore,
)


def _before(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--before must be YYYY-MM-DD") from exc


def main(
    argv: list[str] | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> int:
    utc_clock = clock or (lambda: datetime.now(timezone.utc))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("state"))
    parser.add_argument(
        "--before",
        type=_before,
        default=None,
        help="exclusive UTC date cutoff (default: today UTC, so only closed days)",
    )
    parser.add_argument("--apply", action="store_true", help="publish verified Parquet partitions")
    args = parser.parse_args(argv)

    db_path = args.state_dir / "world_model.db"
    archive_root = args.state_dir / "world_model_archive"
    before = args.before
    try:
        if before is None:
            now = utc_clock()
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("archive clock must return a timezone-aware UTC datetime")
            before = now.astimezone(timezone.utc).date()
        report = WorldPredictionArchiveService(
            source=SQLiteWorldPredictionArchiveSource(db_path),
            sink=DuckDbWorldPredictionParquetStore(archive_root),
            clock=utc_clock,
        ).execute(before=before, apply=args.apply)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        report = {
            "schema_version": "world_prediction_archive_run.v1",
            "mode": "apply" if args.apply else "dry_run",
            "before": None if before is None else before.isoformat(),
            "ok": False,
            "error": f"{type(exc).__name__}:{exc}",
            "source_retained": True,
            "authority": "shadow_only",
            "decision_effect": "none",
        }
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 1
    report["ok"] = True
    report["db_path"] = str(db_path)
    report["archive_root"] = str(archive_root)
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
