from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.universe_selection_analytics import main
from trader.application.universe.selection_attribution import EvaluatedSelection, persist_and_score
from trader.domain.learnings.scoring import SIGNIFICANT_RETURN_BAND
from trader.domain.market_data import Bar
from trader.infrastructure.state_db.universe_selection_store import (
    try_open_universe_selection_store,
)


def test_commande_analytics_imprime_json(tmp_path: Path, capsys) -> None:
    store = try_open_universe_selection_store(tmp_path)
    persist_and_score(
        store,
        [
            EvaluatedSelection(
                mandate_id="m-1",
                symbol="AIR.PA",
                role="core_candidate",
                allowed_sides=("long",),
                as_of="2026-01-01T08:00:00+00:00",
                venue="EU",
                family="defense_aero_eu",
                horizon_sessions=5,
                forward_return=0.02,
                verdict="gagnant",
            ),
            EvaluatedSelection(
                mandate_id="m-2",
                symbol="TSLA",
                role="watch_only",
                allowed_sides=("long",),
                as_of="2026-01-01T08:00:00+00:00",
                venue="US",
                family="us_auto",
                horizon_sessions=5,
                forward_return=-0.03,
                verdict="perdant",
            ),
        ],
        now=datetime(2026, 1, 10, tzinfo=timezone.utc),
    )

    assert main(["--state-dir", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["n"] == 2
    assert payload["n_gagnant"] == 1
    assert payload["n_perdant"] == 1
    families = {item["family"]: item for item in payload["by_family"]}
    assert "defense_aero_eu" in families
    assert "us_auto" in families
    assert "by_venue" in payload and "by_role" in payload
    assert payload["pays"]["families"] == []
    assert payload["decoit"]["families"] == []


def test_commande_evaluate_juge_via_datasource(tmp_path: Path, capsys, monkeypatch) -> None:
    history = tmp_path / "universe_mandates" / "history.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text(
        json.dumps(
            {
                "mandate_id": "m-eu",
                "venue": "EU",
                "as_of": "2026-01-01T08:00:00+00:00",
                "symbols": {
                    "AIR.PA": {
                        "symbol": "AIR.PA",
                        "role": "core_candidate",
                        "allowed_sides": ["long"],
                        "family_context": {"family": "defense_aero_eu"},
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    end = 100.0 * (1.0 + SIGNIFICANT_RETURN_BAND + 0.002)
    bars = [
        Bar(
            ts=f"2026-01-{day:02d}T16:00:00+00:00",
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0 if day < 6 else end,
            volume=100.0,
        )
        for day in range(1, 7)
    ]

    class _FakeSource:
        def get_bars(self, symbol: str, lookback: str, interval: str) -> list[Bar]:
            assert symbol == "AIR.PA"
            return list(bars)

    monkeypatch.setattr(
        "scripts.universe_selection_analytics.build_script_data_source",
        lambda: _FakeSource(),
    )
    assert main(["evaluate", "--state-dir", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["n_evaluated_this_run"] == 2
    assert payload["n_gagnant"] == 1
    assert payload["pays"]["families"] == []


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_commande_trader_joint_mandate_ref_et_round_trips(tmp_path: Path, capsys) -> None:
    _write_jsonl(
        tmp_path / "decisions.jsonl",
        [
            {
                "decision_id": "d-air",
                "cycle_ts": "2026-06-05T10:00:00+00:00",
                "symbol": "AIR.PA",
                "action": "BUY",
                "executed": True,
                "mandate_ref": {
                    "mandate_id": "m-eu",
                    "venue": "EU",
                    "as_of": "2026-06-05T08:00:00+00:00",
                },
            },
            {
                "decision_id": "d-spy",
                "cycle_ts": "2026-06-05T10:00:00+00:00",
                "symbol": "SPY",
                "action": "BUY",
                "executed": True,
            },
        ],
    )
    _write_jsonl(
        tmp_path / "model_performance.jsonl",
        [
            {
                "ts": "2026-06-05T10:00:00+00:00",
                "symbol": "AIR.PA",
                "action": "BUY",
                "quantity": 10,
                "price": 100.0,
                "commission": 0,
                "fx_rate": 1,
                "decision_id": "d-air",
            },
            {
                "ts": "2026-06-05T11:00:00+00:00",
                "symbol": "AIR.PA",
                "action": "SELL",
                "quantity": 10,
                "price": 110.0,
                "commission": 0,
                "fx_rate": 1,
                "decision_id": "d-air-x",
            },
            {
                "ts": "2026-06-05T10:00:00+00:00",
                "symbol": "SPY",
                "action": "BUY",
                "quantity": 5,
                "price": 200.0,
                "commission": 0,
                "fx_rate": 1,
                "decision_id": "d-spy",
            },
            {
                "ts": "2026-06-05T11:00:00+00:00",
                "symbol": "SPY",
                "action": "SELL",
                "quantity": 5,
                "price": 190.0,
                "commission": 0,
                "fx_rate": 1,
                "decision_id": "d-spy-x",
            },
        ],
    )
    history = tmp_path / "universe_mandates" / "history.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text(
        json.dumps(
            {
                "mandate_id": "m-eu",
                "venue": "EU",
                "as_of": "2026-06-05T08:00:00+00:00",
                "symbols": {
                    "AIR.PA": {
                        "symbol": "AIR.PA",
                        "role": "core_candidate",
                        "allowed_sides": ["long"],
                        "family_context": {"family": "eu_industrials"},
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert main(["trader", "--state-dir", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["with_mandate_ref"]["n"] == 1
    assert payload["with_mandate_ref"]["win_rate"] == 1.0
    assert payload["with_mandate_ref"]["net_pnl"] == 100.0
    assert payload["without_mandate_ref"]["n"] == 1
    assert payload["without_mandate_ref"]["win_rate"] == 0.0
    assert payload["without_mandate_ref"]["net_pnl"] == -50.0
    assert payload["by_family"] == [
        {"family": "eu_industrials", "n": 1, "win_rate": 1.0, "net_pnl": 100.0}
    ]
    assert payload["by_role"] == [
        {"role": "core_candidate", "n": 1, "win_rate": 1.0, "net_pnl": 100.0}
    ]


def test_commande_trader_sortie_lisible(tmp_path: Path, capsys) -> None:
    _write_jsonl(
        tmp_path / "decisions.jsonl",
        [
            {
                "decision_id": "d-air",
                "cycle_ts": "2026-06-05T10:00:00+00:00",
                "symbol": "AIR.PA",
                "action": "BUY",
                "executed": True,
                "mandate_ref": {"mandate_id": "m-eu", "venue": "EU"},
            }
        ],
    )
    _write_jsonl(
        tmp_path / "model_performance.jsonl",
        [
            {
                "ts": "2026-06-05T10:00:00+00:00",
                "symbol": "AIR.PA",
                "action": "BUY",
                "quantity": 1,
                "price": 100.0,
                "commission": 0,
                "fx_rate": 1,
                "decision_id": "d-air",
            },
            {
                "ts": "2026-06-05T11:00:00+00:00",
                "symbol": "AIR.PA",
                "action": "SELL",
                "quantity": 1,
                "price": 110.0,
                "commission": 0,
                "fx_rate": 1,
                "decision_id": "d-air-x",
            },
        ],
    )
    history = tmp_path / "universe_mandates" / "history.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text(
        json.dumps(
            {
                "mandate_id": "m-eu",
                "venue": "EU",
                "as_of": "2026-06-05T08:00:00+00:00",
                "symbols": {
                    "AIR.PA": {
                        "symbol": "AIR.PA",
                        "role": "core_candidate",
                        "family_context": {"family": "eu_industrials"},
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert main(["trader", "--state-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "mandate_ref" in out
    assert "eu_industrials" in out
    assert "core_candidate" in out
    assert "10.00" in out
