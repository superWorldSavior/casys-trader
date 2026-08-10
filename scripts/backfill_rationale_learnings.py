"""Backfill LLM decision rationales into the durable learnings sources.

The command is read-only by default.  Pass ``--apply`` to append eligible,
deduplicated candidates to ``state/archive/rationale-experiences.jsonl``.

Usage::

    uv run python scripts/backfill_rationale_learnings.py \
      --since 2026-07-10T11:16:13.393684+00:00
    uv run python scripts/backfill_rationale_learnings.py \
      --since 2026-07-10T11:16:13.393684+00:00 --apply
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from trader.application.record.confidence_feedback import merge_gate_feedback
from trader.application.record.decision_recorder import candidate_learning_note
from trader.infrastructure.files.ledger_rotation import read_rows_with_archive

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = "rationale-experiences.jsonl"


def _utc_timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _existing_decision_ids(state_dir: Path, output_path: Path) -> set[str]:
    paths = {
        output_path,
        state_dir / "learnings.jsonl",
        state_dir / "archive" / "learnings-from-ledger.jsonl",
        state_dir / "archive" / "learnings-evicted.jsonl",
    }
    decision_ids: set[str] = set()
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("decision_id"):
                decision_ids.add(str(row["decision_id"]))
    return decision_ids


def build_backfill_rows(
    *,
    state_dir: Path,
    since: datetime,
    output_path: Path,
) -> tuple[list[dict[str, object]], dict[str, int]]:
    """Return eligible rows plus deterministic audit counters without writing."""
    existing_ids = _existing_decision_ids(state_dir, output_path)
    selected_ids: set[str] = set()
    rows: list[dict[str, object]] = []
    counters: Counter[str] = Counter()

    for decision in read_rows_with_archive(
        state_dir / "decisions.jsonl",
        state_dir / "archive",
    ):
        counters["ledger_rows"] += 1
        ts_raw = decision.get("cycle_ts") or decision.get("ts")
        ts = _utc_timestamp(ts_raw)
        if ts is None or ts <= since:
            counters["before_or_at_watermark"] += 1
            continue

        note, rationale, annotation = candidate_learning_note(decision)
        if note is None or rationale is None:
            counters["ineligible"] += 1
            continue

        decision_id = str(decision.get("decision_id") or "").strip()
        if not decision_id:
            counters["missing_decision_id"] += 1
            continue
        if decision_id in existing_ids or decision_id in selected_ids:
            counters["duplicate_decision_id"] += 1
            continue

        note = merge_gate_feedback(
            decision.get("reason"),
            decision.get("context"),
            note,
        ) or note
        row: dict[str, object] = {
            "ts": str(ts_raw),
            "symbol": str(decision.get("symbol") or ""),
            "note": note,
            "rationale": rationale,
            "action": decision.get("action"),
            "intent": decision.get("intent"),
            "executed": decision.get("executed"),
            "reason": decision.get("reason"),
            "dry_run": decision.get("dry_run"),
            "decision_id": decision_id,
            "source": "decision-rationale-backfill-v1",
        }
        if annotation is not None:
            row["learning_annotation"] = annotation
        rows.append(row)
        selected_ids.add(decision_id)
        counters["selected"] += 1
        counters[f"selected_action_{str(decision.get('action') or 'UNKNOWN').upper()}"] += 1

    return rows, dict(sorted(counters.items()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default=str(ROOT / "state"))
    parser.add_argument("--since", required=True, help="Exclusive ISO-8601 watermark")
    parser.add_argument(
        "--output",
        help="Destination JSONL (default: STATE/archive/rationale-experiences.jsonl)",
    )
    parser.add_argument("--apply", action="store_true", help="Append the selected rows")
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir)
    since = _utc_timestamp(args.since)
    if since is None:
        parser.error("--since must be a valid ISO-8601 timestamp")
    output_path = (
        Path(args.output)
        if args.output
        else state_dir / "archive" / DEFAULT_OUTPUT
    )
    rows, counters = build_backfill_rows(
        state_dir=state_dir,
        since=since,
        output_path=output_path,
    )

    written = 0
    if args.apply and rows:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        with output_path.open("a", encoding="utf-8") as handle:
            handle.write(payload)
        written = len(rows)

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "state_dir": str(state_dir),
        "output": str(output_path),
        "since_exclusive": since.isoformat(),
        "candidates": len(rows),
        "written": written,
        "counters": counters,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
