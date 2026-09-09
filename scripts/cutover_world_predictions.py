"""Plan, prepare or adopt an offline World prediction SQLite+Parquet cutover.

Dry-run is the default.  Never pass ``--apply`` against a live daemon.
Omit ``--adopt`` to keep the source ledger canonical and inspect the candidate.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path

from trader.infrastructure.state_db.world_prediction_cutover import (
    PredictionCutoverFailpoint,
    prepare_prediction_cutover,
)
from trader.infrastructure.state_db.world_prediction_storage_lock import PredictionStorageBusyError
from trader.infrastructure.state_db.world_prediction_tiers import PredictionReadUnavailable


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
        help="exclusive UTC date cutoff (required unless --resume)",
    )
    parser.add_argument("--apply", action="store_true", help="build a verified compact candidate")
    parser.add_argument("--adopt", action="store_true", help="atomically replace source with the candidate")
    parser.add_argument("--resume", action="store_true", help="continue an unfinished cutover journal")
    args = parser.parse_args(argv)

    if args.adopt and not args.apply and not args.resume:
        parser.error("--adopt requires --apply")
    if args.before is None and not args.resume:
        parser.error("--before is required unless --resume")

    try:
        report = prepare_prediction_cutover(
            args.state_dir,
            args.before,
            apply=args.apply,
            adopt=args.adopt,
            resume=args.resume,
            clock=utc_clock,
        )
    except (
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        PredictionStorageBusyError,
        PredictionReadUnavailable,
        PredictionCutoverFailpoint,
    ) as exc:
        report = {
            "schema_version": "world_prediction_cutover_report.v1",
            "ok": False,
            "mode": "resume" if args.resume else ("adopt" if args.adopt else ("apply" if args.apply else "preview")),
            "apply": bool(args.apply or args.resume),
            "adopt": args.adopt,
            "resume": args.resume,
            "before": None if args.before is None else args.before.isoformat(),
            "error": f"{type(exc).__name__}:{exc}",
        }
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 1
    report["ok"] = True
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
