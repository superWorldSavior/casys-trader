from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.universe_selection_analytics import main
from trader.application.universe.selection_attribution import EvaluatedSelection, persist_and_score
from trader.domain.learnings.scoring import SIGNIFICANT_RETURN_BAND
from trader.domain.market_data import Bar
from trader.infrastructure.state_db.connection import StateDb
from trader.infrastructure.state_db.migrations import UNIVERSE_SELECTION_MIGRATION
from trader.infrastructure.state_db.universe_selection_store import UniverseSelectionStore


def test_commande_analytics_imprime_json(tmp_path: Path, capsys) -> None:
    db = StateDb(tmp_path / "casys.db")
    db.apply_migrations([UNIVERSE_SELECTION_MIGRATION])
    store = UniverseSelectionStore(db)
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
    assert payload["pays"]["families"] == ["defense_aero_eu"]
    assert payload["decoit"]["families"] == ["us_auto"]


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
    assert payload["n_evaluated_this_run"] == 1
    assert payload["n_gagnant"] == 1
    assert payload["pays"]["families"] == ["defense_aero_eu"]
