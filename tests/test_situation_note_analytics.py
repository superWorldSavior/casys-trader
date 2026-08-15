from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.situation_note_analytics import main
from trader.application.analyst.situation_attribution import EvaluatedNote, persist_and_score
from trader.domain.learnings.scoring import SIGNIFICANT_RETURN_BAND
from trader.domain.market_data import Bar
from trader.domain.situation import NewsMacroBrief
from trader.infrastructure.state_db.situation_memory_store import SituationMemoryStore


def _brief() -> NewsMacroBrief:
    brief = NewsMacroBrief.from_mapping(
        {
            "brief_id": "2026-01-01T08:00:00+00:00|EU",
            "venue": "EU",
            "as_of": "2026-01-01T08:00:00+00:00",
            "valid_until": "2026-01-02T08:00:00+00:00",
            "families": {
                "eu_industrials": [
                    {
                        "point": "Airbus order flow supports the industrial complex",
                        "sources": ["Reuters"],
                        "source_refs": ["u1"],
                        "symbols": ["AIR.PA"],
                        "direction": "bullish",
                        "horizon": "court terme",
                    }
                ]
            },
        }
    )
    assert brief is not None
    return brief


def test_commande_analytics_imprime_json(tmp_path: Path, capsys) -> None:
    store = SituationMemoryStore(tmp_path / "situation_memory.db")
    store.ingest_brief(_brief())
    persist_and_score(
        store,
        [
            EvaluatedNote(
                id=1,
                section_type="family",
                section_name="eu_industrials",
                direction="bullish",
                as_of="2026-01-01T08:00:00+00:00",
                venue="EU",
                horizon_sessions=5,
                forward_return=0.02,
                coverage_n=3,
                verdict="gagnant",
                symbol="eu_industrials",
                family="eu_industrials",
            )
        ],
        now=datetime(2026, 1, 10, tzinfo=timezone.utc),
    )

    assert main(["--state-dir", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["n"] == 1
    assert payload["n_gagnant"] == 1
    families = {item["family"]: item for item in payload["by_family"]}
    assert "eu_industrials" in families
    assert payload["pays"]["families"] == ["eu_industrials"]


def test_commande_evaluate_juge_via_datasource_et_reste_idempotente(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    store = SituationMemoryStore(tmp_path / "situation_memory.db")
    store.ingest_brief(_brief())
    store.close()

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
            return list(bars)

    monkeypatch.setattr(
        "scripts.situation_note_analytics.build_script_data_source",
        lambda: _FakeSource(),
    )
    assert main(["evaluate", "--state-dir", str(tmp_path)]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["n_evaluated_this_run"] >= 1
    assert first["n_gagnant"] == 1

    assert main(["evaluate", "--state-dir", str(tmp_path)]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["n"] == first["n"]
    assert second["n_gagnant"] == 1

    store = SituationMemoryStore(tmp_path / "situation_memory.db")
    assert store.count() == 1
    rows = store.load_outcomes()
    assert len(rows) == 1
    assert rows[0]["verdict"] == "gagnant"
    assert rows[0]["outcome_score"] is not None
    assert rows[0]["q_value"] is None
