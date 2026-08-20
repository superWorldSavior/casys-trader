from __future__ import annotations

import gzip
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


def test_commande_bench_compare_agent_et_baseline(tmp_path: Path, capsys) -> None:
    store = try_open_universe_selection_store(tmp_path)
    persist_and_score(
        store,
        [
            EvaluatedSelection(
                mandate_id="m-agent",
                symbol="AIR.PA",
                role="core_candidate",
                allowed_sides=("long",),
                as_of="2026-01-01T08:00:00+00:00",
                venue="EU",
                family="defense_aero_eu",
                horizon_sessions=5,
                forward_return=0.02,
                verdict="gagnant",
                verdict_basis="allocation",
                selector="agent",
            ),
            EvaluatedSelection(
                mandate_id="m-agent",
                symbol="AIR.PA",
                role="core_candidate",
                allowed_sides=("long",),
                as_of="2026-01-01T08:00:00+00:00",
                venue="EU",
                family="defense_aero_eu",
                horizon_sessions=5,
                forward_return=0.02,
                verdict="perdant",
                verdict_basis="direction",
                selector="agent",
            ),
            EvaluatedSelection(
                mandate_id="m-fb",
                symbol="MC.PA",
                role="fallback_selection",
                allowed_sides=(),
                as_of="2026-01-01T08:00:00+00:00",
                venue="EU",
                family="luxury",
                horizon_sessions=5,
                forward_return=0.01,
                verdict="perdant",
                verdict_basis="allocation",
                selector="baseline_fallback",
            ),
        ],
        now=datetime(2026, 1, 10, tzinfo=timezone.utc),
    )

    assert main(["bench", "--state-dir", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["horizon_sessions"] == 5
    agent_alloc = payload["selectors"]["agent"]["allocation"]
    assert agent_alloc["n"] == 1
    assert agent_alloc["beat_bench_rate"] == 1.0
    assert agent_alloc["mean_flair_score"] is not None
    assert payload["selectors"]["agent"]["direction"]["n"] == 1
    assert payload["selectors"]["agent"]["direction"]["win_rate"] == 0.0
    assert payload["selectors"]["baseline_fallback"]["allocation"]["n"] == 1
    assert payload["selectors"]["baseline_fallback"]["allocation"]["beat_bench_rate"] == 0.0
    assert payload["selectors"]["baseline_fallback"]["direction"]["n"] == 0
    assert payload["selectors"]["baseline_fallback"]["direction"]["win_rate"] is None


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
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
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
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
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
                "commission_currency": "USD",
                "commission_model": "ibkr_us_stock_tiered",
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
                "commission_currency": "USD",
                "commission_model": "ibkr_us_stock_tiered",
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
    assert payload["with_mandate_ref"]["gross_pnl"] == 100.0
    assert payload["with_mandate_ref"]["economics_quality"]["status"] == "complete"
    assert payload["without_mandate_ref"]["n"] == 1
    assert payload["without_mandate_ref"]["win_rate"] == 0.0
    assert payload["without_mandate_ref"]["net_pnl"] == -50.0
    assert payload["by_family"][0]["family"] == "eu_industrials"
    assert payload["by_family"][0]["win_rate"] == 1.0
    assert payload["by_family"][0]["net_pnl"] == 100.0
    assert payload["by_family"][0]["gross_pnl"] == 100.0
    assert payload["by_role"][0]["role"] == "core_candidate"
    assert payload["by_role"][0]["win_rate"] == 1.0
    assert payload["by_role"][0]["net_pnl"] == 100.0
    assert payload["by_role"][0]["gross_pnl"] == 100.0


def test_commande_trader_garde_le_brut_et_explicite_le_net_indisponible(
    tmp_path: Path,
    capsys,
) -> None:
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
                "commission_currency": "EUR",
                "commission_model": "none",
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
                "commission_currency": "EUR",
                "commission_model": "none",
                "fx_rate": 1,
                "decision_id": "d-air-x",
            },
        ],
    )

    assert main(["trader", "--state-dir", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    stats = payload["with_mandate_ref"]
    assert stats["n"] == 1
    assert stats["gross_pnl"] == 10.0
    assert stats["net_pnl"] is None
    assert stats["win_rate"] is None
    assert stats["economics_quality"] == {
        "status": "incomplete",
        "reason": "commission_or_net_economics_incomplete",
        "models": ["none"],
        "reasons": ["commission_not_modeled"],
        "trips": 1,
        "net_known": 0,
        "net_unknown": 1,
        "gross_known": 1,
        "gross_unknown": 0,
        "gross_pnl_available": True,
        "commission_and_net_available": False,
    }


def test_commande_trader_agrege_les_sorties_partielles_en_un_cycle(
    tmp_path: Path,
    capsys,
) -> None:
    _write_jsonl(
        tmp_path / "decisions.jsonl",
        [
            {
                "decision_id": "d-partial",
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
                "quantity": 2,
                "price": 100.0,
                "commission": 0,
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
                "fx_rate": 1,
                "decision_id": "d-partial",
            },
            {
                "ts": "2026-06-05T11:00:00+00:00",
                "symbol": "AIR.PA",
                "action": "SELL",
                "quantity": 1,
                "price": 90.0,
                "commission": 0,
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
                "fx_rate": 1,
            },
            {
                "ts": "2026-06-05T12:00:00+00:00",
                "symbol": "AIR.PA",
                "action": "SELL",
                "quantity": 1,
                "price": 120.0,
                "commission": 0,
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
                "fx_rate": 1,
            },
        ],
    )

    assert main(["trader", "--state-dir", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["with_mandate_ref"]["n"] == 1
    assert payload["with_mandate_ref"]["win_rate"] == 1.0
    assert payload["with_mandate_ref"]["net_pnl"] == 10.0
    assert payload["with_mandate_ref"]["economics_quality"]["net_known"] == 1


def test_commande_trader_lit_une_decision_archivee_gzip(
    tmp_path: Path,
    capsys,
) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    archived_decision = {
        "decision_id": "d-archive",
        "cycle_ts": "2026-06-05T10:00:00+00:00",
        "symbol": "AIR.PA",
        "action": "BUY",
        "executed": True,
        "mandate_ref": {"mandate_id": "m-archive", "venue": "EU"},
    }
    with gzip.open(
        archive_dir / "decisions-2026-06.jsonl.gz",
        "wt",
        encoding="utf-8",
    ) as handle:
        handle.write(json.dumps(archived_decision) + "\n")
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
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
                "fx_rate": 1,
                "decision_id": "d-archive",
            },
            {
                "ts": "2026-06-05T11:00:00+00:00",
                "symbol": "AIR.PA",
                "action": "SELL",
                "quantity": 1,
                "price": 110.0,
                "commission": 0,
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
                "fx_rate": 1,
            },
        ],
    )

    assert main(["trader", "--state-dir", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["with_mandate_ref"]["n"] == 1
    assert payload["without_mandate_ref"]["n"] == 0
    assert payload["unattributed"]["n"] == 0
    assert payload["decision_rows_quality"] == {
        "status": "available",
        "reason": None,
        "source": None,
        "rows_requested": 1,
        "rows_found": 1,
        "rows_missing": 0,
        "duplicate_rows": 0,
    }


def test_commande_trader_deduplique_et_prefere_la_decision_live(
    tmp_path: Path,
    capsys,
) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    archived = {
        "decision_id": "d-duplicate",
        "cycle_ts": "2026-06-05T10:00:00+00:00",
        "symbol": "AIR.PA",
        "action": "BUY",
        "executed": True,
        "mandate_ref": {"mandate_id": "m-old", "venue": "EU"},
    }
    with gzip.open(
        archive_dir / "decisions-2026-06.jsonl.gz",
        "wt",
        encoding="utf-8",
    ) as handle:
        handle.write(json.dumps(archived) + "\n")
    live = {**archived}
    live.pop("mandate_ref")
    _write_jsonl(tmp_path / "decisions.jsonl", [live])
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
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
                "fx_rate": 1,
                "decision_id": "d-duplicate",
            },
            {
                "ts": "2026-06-05T11:00:00+00:00",
                "symbol": "AIR.PA",
                "action": "SELL",
                "quantity": 1,
                "price": 110.0,
                "commission": 0,
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
                "fx_rate": 1,
            },
        ],
    )

    assert main(["trader", "--state-dir", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["with_mandate_ref"]["n"] == 0
    assert payload["without_mandate_ref"]["n"] == 1
    assert payload["decision_rows_quality"]["duplicate_rows"] == 1


def test_commande_trader_reste_fail_soft_sur_archive_decisions_corrompue(
    tmp_path: Path,
    capsys,
) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "decisions-2026-06.jsonl.gz").write_bytes(b"not-a-gzip")
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
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
                "fx_rate": 1,
                "decision_id": "d-corrupt",
            },
            {
                "ts": "2026-06-05T11:00:00+00:00",
                "symbol": "AIR.PA",
                "action": "SELL",
                "quantity": 1,
                "price": 110.0,
                "commission": 0,
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
                "fx_rate": 1,
            },
        ],
    )

    assert main(["trader", "--state-dir", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["decision_rows_quality"]["status"] == "unavailable"
    assert payload["decision_rows_quality"]["reason"] == ("decision_ledger_unreadable")
    assert payload["with_mandate_ref"]["n"] == 0
    assert payload["without_mandate_ref"]["n"] == 0
    assert payload["unattributed"]["n"] == 1


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
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
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
                "commission_currency": "EUR",
                "commission_model": "ibkr_europe_stock_tiered",
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
