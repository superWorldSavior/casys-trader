from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.backfill_rationale_learnings import build_backfill_rows, main

UTC = timezone.utc
WATERMARK = datetime(2026, 7, 10, 11, 16, 13, tzinfo=UTC)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _decision(decision_id: str, *, cycle_ts: str, **updates: object) -> dict:
    return {
        "decision_id": decision_id,
        "cycle_ts": cycle_ts,
        "symbol": "SPY",
        "action": "HOLD",
        "intent": "HOLD",
        "executed": False,
        "reason": "hold",
        "rationale": "Attendre une cassure confirmée.",
        "decision_source": "llm",
        "model_called": True,
        "llm_provider": "acpx",
        "llm_model": "gpt-5.6-sol",
        "llm_error": None,
        **updates,
    }


def test_build_backfill_rows_filters_and_deduplicates(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    output = state_dir / "archive" / "rationale-experiences.jsonl"
    _write_jsonl(
        state_dir / "decisions.jsonl",
        [
            # HOLD dirigé (opportunity_side nommé) : ingéré depuis c838ee1.
            _decision(
                "eligible",
                cycle_ts="2026-07-11T10:00:00+00:00",
                opportunity_side="long",
            ),
            # HOLD nu, sans thèse refusée ni annotation : plus jamais ingéré.
            _decision("undirected", cycle_ts="2026-07-11T10:30:00+00:00"),
            _decision(
                "annotated",
                cycle_ts="2026-07-11T11:00:00+00:00",
                learning="Ne pas poursuivre une extension verticale.",
            ),
            _decision(
                "infra",
                cycle_ts="2026-07-11T12:00:00+00:00",
                decision_source="infra",
                model_called=False,
            ),
            _decision(
                "errored",
                cycle_ts="2026-07-11T13:00:00+00:00",
                llm_error="bad_output",
            ),
            _decision("old", cycle_ts="2026-07-10T10:00:00+00:00"),
            # Dirigés eux aussi : ils doivent atteindre le test de déduplication.
            _decision(
                "already-there",
                cycle_ts="2026-07-11T14:00:00+00:00",
                opportunity_side="long",
            ),
            _decision(
                "legacy-backfill",
                cycle_ts="2026-07-11T15:00:00+00:00",
                opportunity_side="short",
            ),
        ],
    )
    _write_jsonl(output, [{"decision_id": "already-there", "note": "existing"}])
    _write_jsonl(
        state_dir / "archive" / "learnings-from-ledger.jsonl",
        [{"decision_id": "legacy-backfill", "note": "existing legacy backfill"}],
    )

    rows, counters = build_backfill_rows(
        state_dir=state_dir,
        since=WATERMARK,
        output_path=output,
    )

    assert [row["decision_id"] for row in rows] == ["eligible", "annotated"]
    assert rows[0]["note"] == "Attendre une cassure confirmée."
    assert rows[1]["note"] == (
        "Attendre une cassure confirmée.\n"
        "[annotation explicite] Ne pas poursuivre une extension verticale."
    )
    assert counters["selected"] == 2
    # infra + errored + le HOLD non dirigé
    assert counters["ineligible"] == 3
    assert counters["before_or_at_watermark"] == 1
    assert counters["duplicate_decision_id"] == 2


def test_main_is_dry_run_by_default_and_apply_is_idempotent(tmp_path: Path, capsys) -> None:
    state_dir = tmp_path / "state"
    output = state_dir / "archive" / "rationale-experiences.jsonl"
    _write_jsonl(
        state_dir / "decisions.jsonl",
        [
            _decision(
                "eligible",
                cycle_ts="2026-07-11T10:00:00+00:00",
                opportunity_side="long",
            )
        ],
    )
    args = [
        "--state-dir",
        str(state_dir),
        "--since",
        WATERMARK.isoformat(),
    ]

    assert main(args) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["mode"] == "dry-run"
    assert dry_run["candidates"] == 1
    assert dry_run["written"] == 0
    assert not output.exists()

    assert main([*args, "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["written"] == 1
    assert len(output.read_text(encoding="utf-8").splitlines()) == 1

    assert main([*args, "--apply"]) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["candidates"] == 0
    assert replay["written"] == 0
    assert len(output.read_text(encoding="utf-8").splitlines()) == 1
