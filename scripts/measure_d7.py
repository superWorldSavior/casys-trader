"""Mesure D7/D8 — appels LLM, gate, plans armés depuis un instant donné.

Lecture seule sur state/events.jsonl + state/decisions.jsonl.
Usage : .venv/bin/python scripts/measure_d7.py --since 2026-06-11T04:53:00+00:00
Référence pré-D7 : 92 appels LLM décideur / 24 h (mesuré le 2026-06-11).
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / "state"


def _rows(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, help="ISO 8601 (ex. le ts du restart)")
    args = parser.parse_args()
    since = args.since

    cycles = 0
    model_calls = 0
    events = Counter()
    for event in _rows(STATE / "events.jsonl"):
        if str(event.get("ts") or "") < since:
            continue
        kind = str(event.get("event") or "")
        events[kind] += 1
        if kind == "cycle_completed":
            cycles += 1
            model_calls += int(event.get("model_calls_used") or 0)

    reasons = Counter()
    sources = Counter()
    for row in _rows(STATE / "decisions.jsonl"):
        if str(row.get("cycle_ts") or "") < since:
            continue
        reasons[str(row.get("reason") or "?")] += 1
        sources[str(row.get("source") or "daemon")] += 1

    now = datetime.now(timezone.utc).isoformat()
    elapsed_h = None
    try:
        dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        elapsed_h = round((datetime.now(timezone.utc) - dt).total_seconds() / 3600, 1)
    except ValueError:
        pass

    print(f"Mesure D7 depuis {since} (maintenant {now}, {elapsed_h} h écoulées)")
    print(f"  cycles                : {cycles}")
    print(f"  appels LLM décideur   : {model_calls}", end="")
    if elapsed_h and elapsed_h > 0:
        print(f"  (projection 24 h : {model_calls / elapsed_h * 24:.0f} — référence pré-D7 : 92)")
    else:
        print()
    print(f"  décisions par raison  : {dict(reasons.most_common(8))}")
    print(f"  décisions par source  : {dict(sources)}")
    armed = {k: v for k, v in events.items() if k.startswith("armed_plan")}
    print(f"  événements plans armés: {armed or 'aucun'}")
    print(f"  watches déclenchées   : {events.get('indicator_watch_triggered', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
