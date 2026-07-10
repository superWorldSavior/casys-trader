from __future__ import annotations

import json

import pytest

from trader.runtime.learning_outcomes import realised_entry_outcomes, realised_verdict


def _append(path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_realised_entry_outcome_waits_for_all_partial_exits(tmp_path) -> None:
    _append(
        tmp_path / "model_performance.jsonl",
        [
            {"ts": "2026-07-01T10:00:00+00:00", "symbol": "SPY", "action": "BUY", "quantity": 10, "price": 100, "decision_id": "entry", "commission": 1, "fx_rate": 1},
            {"ts": "2026-07-02T10:00:00+00:00", "symbol": "SPY", "action": "SELL", "quantity": 4, "price": 110, "commission": 0.4, "fx_rate": 1},
        ],
    )
    assert realised_entry_outcomes(tmp_path) == {}

    with (tmp_path / "model_performance.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "2026-07-03T10:00:00+00:00", "symbol": "SPY", "action": "SELL", "quantity": 6, "price": 105, "commission": 0.6, "fx_rate": 1}) + "\n")

    assert realised_entry_outcomes(tmp_path)["entry"] == pytest.approx(0.068)


def test_realised_verdict_uses_directional_net_return_band() -> None:
    assert realised_verdict(0.006) == ("WIN", 1.0)
    assert realised_verdict(-0.006) == ("LOSS", -1.0)
    assert realised_verdict(0.001) == ("NEUTRAL", 0.0)
