from __future__ import annotations

from copy import deepcopy
import gzip
import json
from pathlib import Path

import pytest
import yaml

from trader.execution.broker import Order
from trader.infrastructure.state_db.broker_store import SqliteBroker
from trader.infrastructure.state_db.connection import open_state_db
from trader.infrastructure.state_db.migrations import import_broker_from_json
from trader.infrastructure.brokers.commission_models import IbkrCommissionModel
from trader.reporting.read_models.live_kpis import (
    compute_live_kpis,
    read_equity_curve,
    read_gbp_history_attestation,
)
from trader.reporting.read_models.trade_history import (
    aggregate_position_cycles,
    compute_round_trips,
)
from trader.support.metadata.experiment import (
    LEGACY_ID_PREFIX,
    SCHEMA_VERSION,
    _experiment_id,
)


def _experiment_state(tmp_path: Path) -> tuple[Path, SqliteBroker]:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "universe.yaml").write_text(
        yaml.dump({"starting_cash": 100_000.0, "symbols": ["SPY"]}),
        encoding="utf-8",
    )
    db = open_state_db(state_dir / "casys.db")
    import_broker_from_json(
        db,
        state_dir / "_absent_broker.json",
        starting_cash=100_000.0,
    )
    return state_dir, SqliteBroker(db, commission_model=IbkrCommissionModel())


def _decision_row(
    decision_id: str,
    variant: str,
    *,
    decision_grade: bool,
) -> dict:
    model = f"gpt-{variant}"
    components = {
        "git_commit": "a" * 40,
        "git_tracked_dirty": False,
        "model": {
            "provider": "acpx",
            "model": model,
            "preset": "test-medium",
            "execution_profile": {
                "transport": "acpx",
                "agent": "codex",
                "reasoning_effort": "medium",
                "profile_fingerprint": "sha256:" + "b" * 64,
            },
        },
        "execution": {"commission_model": "none"},
        "risk": {
            "max_position_value": 50_000.0,
            "max_gross_exposure": 100_000.0,
            "max_order_value": 50_000.0,
            "min_equity": 50_000.0,
            "max_risk_per_trade_pct": 0.01,
            "min_trade_confidence": 0.7,
            "full_risk_confidence": 0.9,
            "confidence_gate_enabled": False,
            "require_hard_stop": False,
        },
    }
    return {
        "decision_id": decision_id,
        "experiment_id": _experiment_id(components),
        "experiment_status": {
            "schema_version": SCHEMA_VERSION,
            "decision_grade": decision_grade,
            "issues": [],
        },
        "experiment_components": components,
        "llm_provider": "acpx",
        "llm_model": model,
    }


def _write_decisions(state_dir: Path, rows: list[dict]) -> None:
    (state_dir / "decisions.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows),
        encoding="utf-8",
    )


def _legacy_decision_row(decision_id: str, variant: str) -> dict:
    row = _decision_row(decision_id, variant, decision_grade=True)
    components = deepcopy(row["experiment_components"])
    components["model"].pop("execution_profile")
    row["experiment_components"] = components
    row["experiment_id"] = _experiment_id(
        components,
        prefix=LEGACY_ID_PREFIX,
    )
    row["experiment_status"]["schema_version"] = 1
    return row


def _write_history(state_dir: Path, rows: list[dict]) -> None:
    (state_dir / "history.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows),
        encoding="utf-8",
    )


def _insert_gbp_attestation(
    state_dir: Path,
    *,
    imported_at: str = "2026-01-02T00:00:00+00:00",
    scope_imported_at: str | None = None,
    scope_store: str | None = None,
) -> None:
    scope_store = scope_store or (
        "gbp_minor_quotes_v1:scope:v1:1000:2:1000:"
        + "a" * 64
        + ":"
        + "b" * 64
    )
    db = open_state_db(state_dir / "casys.db")
    with db.transaction() as cur:
        cur.executemany(
            "INSERT INTO state_imports(store, imported_at) VALUES (?, ?)",
            [
                ("gbp_minor_quotes_v1", imported_at),
                (scope_store, scope_imported_at or imported_at),
            ],
        )


def test_compute_live_kpis_read_model_projects_state_files(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "universe.yaml").write_text(
        yaml.dump({"starting_cash": 100_000.0, "symbols": ["SPY"]}),
        encoding="utf-8",
    )
    (state_dir / "history.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-01-01T10:00:00", "equity": 100_000.0}),
                json.dumps({"ts": "2026-01-02T10:00:00", "equity": 101_000.0}),
            ]
        ),
        encoding="utf-8",
    )
    (state_dir / "broker.json").write_text(
        json.dumps(
            {
                "cash": 95_000.0,
                "positions": {
                    "SPY": {"symbol": "SPY", "quantity": 5.0, "avg_price": 100.0},
                    "QQQ": {"symbol": "QQQ", "quantity": 0.0, "avg_price": 0.0},
                },
                "fills": [
                    {
                        "ts": "2026-01-01T10:00:00+00:00",
                        "symbol": "SPY",
                        "side": "BUY",
                        "quantity": 5,
                        "price": 100.0,
                        "fx_rate": 1.0,
                        "commission": 0.0,
                        "commission_currency": "USD",
                        "commission_model": "ibkr_us_stock_tiered",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    kpis = compute_live_kpis(state_dir)

    assert kpis["equity"] == 101_000.0
    assert kpis["cash"] == 95_000.0
    assert kpis["num_trades"] == 0
    assert kpis["num_closed_position_cycles"] == 0
    assert kpis["num_fills"] == 1
    assert kpis["trade_count_grain"] == "flat_to_flat_position_cycle"
    assert kpis["positions"] == [{"symbol": "SPY", "quantity": 5.0, "avg_price": 100.0}]


def test_compute_live_kpis_reads_broker_from_sqlite_when_db_exists(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "universe.yaml").write_text(
        yaml.dump({"starting_cash": 100_000.0, "symbols": ["SPY"]}),
        encoding="utf-8",
    )
    (state_dir / "history.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-01-01T10:00:00", "equity": 100_000.0}),
                json.dumps({"ts": "2026-01-02T10:00:00", "equity": 100_500.0}),
            ]
        ),
        encoding="utf-8",
    )

    db = open_state_db(state_dir / "casys.db")
    import_broker_from_json(db, state_dir / "_absent_broker.json", starting_cash=100_000.0)
    broker = SqliteBroker(db)
    broker.submit(Order("SPY", "BUY", 5.0), 100.0, "2026-01-01T10:00:00+00:00", dry_run=False)

    kpis = compute_live_kpis(state_dir)

    assert not (state_dir / "broker.json").exists()
    assert kpis["cash"] == 99_500.0
    assert kpis["num_trades"] == 0
    assert kpis["num_closed_position_cycles"] == 0
    assert kpis["num_fills"] == 1
    assert kpis["positions"] == [{"symbol": "SPY", "quantity": 5.0, "avg_price": 100.0}]


def test_compute_live_kpis_counts_completed_position_cycles_not_fill_rows(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "universe.yaml").write_text(
        yaml.dump({"starting_cash": 100_000.0, "symbols": ["SPY"]}),
        encoding="utf-8",
    )
    (state_dir / "model_performance.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-01-01T10:00:00+00:00",
                        "symbol": "SPY",
                        "action": "BUY",
                        "quantity": 10,
                        "price": 100.0,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-01-01T11:00:00+00:00",
                        "symbol": "SPY",
                        "action": "SELL",
                        "quantity": 4,
                        "price": 105.0,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-01-01T12:00:00+00:00",
                        "symbol": "SPY",
                        "action": "SELL",
                        "quantity": 6,
                        "price": 95.0,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    (state_dir / "broker.json").write_text(
        json.dumps(
            {
                "cash": 99_990.0,
                "positions": {},
                "fills": [
                    {
                        "ts": "2026-01-01T10:00:00+00:00",
                        "symbol": "SPY",
                        "side": "BUY",
                        "quantity": 10,
                        "price": 100.0,
                        "fx_rate": 1.0,
                        "commission": 0.0,
                        "commission_currency": "USD",
                        "commission_model": "ibkr_us_stock_tiered",
                    },
                    {
                        "ts": "2026-01-01T11:00:00+00:00",
                        "symbol": "SPY",
                        "side": "SELL",
                        "quantity": 4,
                        "price": 105.0,
                        "fx_rate": 1.0,
                        "commission": 0.0,
                        "commission_currency": "USD",
                        "commission_model": "ibkr_us_stock_tiered",
                    },
                    {
                        "ts": "2026-01-01T12:00:00+00:00",
                        "symbol": "SPY",
                        "side": "SELL",
                        "quantity": 6,
                        "price": 95.0,
                        "fx_rate": 1.0,
                        "commission": 0.0,
                        "commission_currency": "USD",
                        "commission_model": "ibkr_us_stock_tiered",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    kpis = compute_live_kpis(state_dir)

    assert kpis["num_trades"] == 1
    assert kpis["num_closed_position_cycles"] == 1


def test_sqlite_cycle_headline_ignores_missing_duplicate_and_divergent_projection(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "universe.yaml").write_text(
        yaml.dump({"starting_cash": 100_000.0, "symbols": ["SPY"]}),
        encoding="utf-8",
    )

    db = open_state_db(state_dir / "casys.db")
    import_broker_from_json(
        db,
        state_dir / "_absent_broker.json",
        starting_cash=100_000.0,
    )
    broker = SqliteBroker(db)
    broker.submit(
        Order("SPY", "BUY", 10.0),
        100.0,
        "2026-01-01T10:00:00+00:00",
        dry_run=False,
    )
    broker.submit(
        Order("SPY", "SELL", 10.0),
        110.0,
        "2026-01-01T11:00:00+00:00",
        dry_run=False,
    )

    # Missing projection: the durable broker ledger is sufficient.
    assert not (state_dir / "model_performance.jsonl").exists()
    missing = compute_live_kpis(state_dir)
    assert missing["num_fills"] == 2
    assert missing["num_closed_position_cycles"] == 1

    matching_buy = {
        "ts": "2026-01-01T10:00:00+00:00",
        "symbol": "SPY",
        "action": "BUY",
        "quantity": 10.0,
        "price": 100.0,
        "confidence": 0.8,
    }
    projection_path = state_dir / "model_performance.jsonl"
    projection_path.write_text(
        "\n".join(json.dumps(matching_buy) for _ in range(2)),
        encoding="utf-8",
    )
    duplicated = compute_live_kpis(state_dir)
    assert duplicated["num_fills"] == 2
    assert duplicated["num_closed_position_cycles"] == 1

    projection_path.write_text(
        json.dumps({**matching_buy, "price": 9_999.0, "quantity": 999.0}),
        encoding="utf-8",
    )
    divergent = compute_live_kpis(state_dir)
    assert divergent["num_fills"] == 2
    assert divergent["num_closed_position_cycles"] == 1


def test_headline_does_not_promote_projection_rows_without_broker_ledger(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "universe.yaml").write_text(
        yaml.dump({"starting_cash": 100_000.0, "symbols": ["SPY"]}),
        encoding="utf-8",
    )
    (state_dir / "model_performance.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                {
                    "ts": "2026-01-01T10:00:00+00:00",
                    "symbol": "SPY",
                    "action": "BUY",
                    "quantity": 1.0,
                    "price": 100.0,
                },
                {
                    "ts": "2026-01-01T11:00:00+00:00",
                    "symbol": "SPY",
                    "action": "SELL",
                    "quantity": 1.0,
                    "price": 110.0,
                },
            )
        ),
        encoding="utf-8",
    )

    kpis = compute_live_kpis(state_dir)

    assert kpis["num_fills"] == 0
    assert kpis["num_closed_position_cycles"] == 0
    assert sum(row["fills"] for row in kpis["model_performance"]) == 2


def test_model_performance_ignores_only_marked_stale_equity_fields(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "universe.yaml").write_text(
        yaml.dump({"starting_cash": 100_000.0, "symbols": ["SPY"]}),
        encoding="utf-8",
    )
    rows = [
        {
            "ts": "2026-01-01T10:00:00+00:00",
            "symbol": "SPY",
            "llm_provider": "acpx",
            "llm_model": "gpt-test",
            "confidence": 0.6,
            "equity": 100_000.0,
        },
        {
            "ts": "2026-01-01T11:00:00+00:00",
            "symbol": "RR.L",
            "llm_provider": "acpx",
            "llm_model": "gpt-test",
            "confidence": 0.9,
            "equity": 999_999.0,
            "cash": 888_888.0,
            "economic_fields_quality": {
                "status": "stale",
                "reason": "legacy_gbp_minor_quote_accounting_not_reconstructed",
                "migration": "gbp_minor_quotes_v1",
                "fields": ["cash", "equity"],
            },
        },
        {
            "ts": "2026-01-01T12:00:00+00:00",
            "symbol": "QQQ",
            "llm_provider": "acpx",
            "llm_model": "gpt-test",
            "confidence": 0.3,
            "equity": 100_100.0,
        },
    ]
    (state_dir / "model_performance.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows),
        encoding="utf-8",
    )

    performance = compute_live_kpis(state_dir)["model_performance"][0]

    assert performance["fills"] == 3
    assert performance["symbols"] == ["QQQ", "RR.L", "SPY"]
    assert performance["avg_confidence"] == pytest.approx(0.6)
    assert performance["first_equity"] == 100_000.0
    assert performance["last_equity"] == 100_100.0
    assert performance["portfolio_equity_delta"] == 100.0


def test_experiment_performance_attributes_normal_completed_cycle(
    tmp_path: Path,
) -> None:
    state_dir, broker = _experiment_state(tmp_path)
    broker.submit(
        Order("SPY", "BUY", 10.0, decision_id="entry-1"),
        100.0,
        "2026-01-01T10:00:00+00:00",
        dry_run=False,
    )
    broker.submit(
        Order("SPY", "SELL", 10.0, decision_id="exit-1"),
        110.0,
        "2026-01-01T11:00:00+00:00",
        dry_run=False,
    )
    # A matching secondary projection may add display metadata, but its causal
    # decision_id must never replace the canonical fill link.
    (state_dir / "model_performance.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-01-01T10:00:00+00:00",
                "symbol": "SPY",
                "action": "BUY",
                "quantity": 10.0,
                "price": 100.0,
                "decision_id": "projection-entry",
            }
        ),
        encoding="utf-8",
    )
    entry_row = _decision_row("entry-1", "test", decision_grade=True)
    projection_row = _decision_row(
        "projection-entry",
        "secondary",
        decision_grade=True,
    )
    _write_decisions(state_dir, [entry_row, projection_row])

    rows = compute_live_kpis(state_dir)["experiment_performance"]

    assert len(rows) == 1
    cohort = rows[0]
    assert cohort["cohort"] == entry_row["experiment_id"]
    assert cohort["experiment_id"] == entry_row["experiment_id"]
    assert cohort["attribution_status"] == "attributed"
    assert cohort["completed_cycles"] == 1
    assert cohort["wins"] == 1
    assert cohort["gross_pnl"] == pytest.approx(100.0)
    assert cohort["commissions"] == pytest.approx(0.7)
    assert cohort["net_pnl"] == pytest.approx(99.3)
    assert cohort["win_rate"] == 1.0
    assert cohort["commission_quality"] == {
        "status": "available",
        "reason": None,
        "models": [],
        "reasons": [],
        "complete_cycles": 1,
        "incomplete_cycles": 0,
    }
    assert cohort["provider"] == "acpx"
    assert cohort["model"] == "gpt-test"
    assert cohort["profile"] == {
        "preset": "test-medium",
        "execution_profile": {
            "transport": "acpx",
            "agent": "codex",
            "reasoning_effort": "medium",
            "profile_fingerprint": "sha256:" + "b" * 64,
        },
    }
    assert cohort["components"]["git_commit"] == "a" * 40


def test_experiment_performance_buckets_missing_decision_as_unknown(
    tmp_path: Path,
) -> None:
    state_dir, broker = _experiment_state(tmp_path)
    broker.submit(
        Order("SPY", "BUY", 1.0, decision_id="missing-entry"),
        100.0,
        "2026-01-01T10:00:00+00:00",
        dry_run=False,
    )
    broker.submit(
        Order("SPY", "SELL", 1.0, decision_id="exit"),
        90.0,
        "2026-01-01T11:00:00+00:00",
        dry_run=False,
    )

    rows = compute_live_kpis(state_dir)["experiment_performance"]

    assert rows == [
        {
            "cohort": "unknown",
            "experiment_id": None,
            "experiment_ids": [],
            "attribution_status": "unknown",
            "attribution_quality": {
                "status": "available",
                "reason": None,
                "source": None,
            },
            "attribution_reasons": {"missing_decision": 1},
            "completed_cycles": 1,
            "wins": 0,
            "gross_pnl": -10.0,
            "commissions": 0.7,
            "net_pnl": -10.7,
            "win_rate": 0.0,
            "commission_quality": {
                "status": "available",
                "reason": None,
                "models": [],
                "reasons": [],
                "complete_cycles": 1,
                "incomplete_cycles": 0,
            },
            "provider": "unknown",
            "model": "unknown",
            "profile": None,
            "components": None,
        }
    ]


def test_experiment_performance_buckets_mixed_entry_experiments(
    tmp_path: Path,
) -> None:
    state_dir, broker = _experiment_state(tmp_path)
    broker.submit(
        Order("SPY", "BUY", 5.0, decision_id="entry-a"),
        100.0,
        "2026-01-01T10:00:00+00:00",
        dry_run=False,
    )
    broker.submit(
        Order("SPY", "BUY", 5.0, decision_id="entry-b"),
        102.0,
        "2026-01-01T10:30:00+00:00",
        dry_run=False,
    )
    broker.submit(
        Order("SPY", "SELL", 10.0, decision_id="exit"),
        110.0,
        "2026-01-01T11:00:00+00:00",
        dry_run=False,
    )
    entry_a = _decision_row("entry-a", "a", decision_grade=True)
    entry_b = _decision_row("entry-b", "b", decision_grade=True)
    _write_decisions(state_dir, [entry_a, entry_b])

    cohort = compute_live_kpis(state_dir)["experiment_performance"][0]

    assert cohort["cohort"] == "mixed"
    assert cohort["experiment_id"] is None
    assert cohort["experiment_ids"] == sorted(
        [entry_a["experiment_id"], entry_b["experiment_id"]]
    )
    assert cohort["attribution_status"] == "mixed"
    assert cohort["attribution_reasons"] == {"mixed_experiment_ids": 1}
    assert cohort["completed_cycles"] == 1
    assert cohort["gross_pnl"] == pytest.approx(90.0)


def test_experiment_performance_buckets_non_grade_entry_as_unknown(
    tmp_path: Path,
) -> None:
    state_dir, broker = _experiment_state(tmp_path)
    broker.submit(
        Order("SPY", "BUY", 1.0, decision_id="entry-dirty"),
        100.0,
        "2026-01-01T10:00:00+00:00",
        dry_run=False,
    )
    broker.submit(
        Order("SPY", "SELL", 1.0, decision_id="exit"),
        101.0,
        "2026-01-01T11:00:00+00:00",
        dry_run=False,
    )
    _write_decisions(
        state_dir,
        [_decision_row("entry-dirty", "dirty", decision_grade=False)],
    )

    cohort = compute_live_kpis(state_dir)["experiment_performance"][0]

    assert cohort["cohort"] == "unknown"
    assert cohort["experiment_id"] is None
    assert cohort["attribution_status"] == "unknown"
    assert cohort["attribution_reasons"] == {"not_decision_grade": 1}


def test_experiment_performance_resolves_entry_decision_from_archive(
    tmp_path: Path,
) -> None:
    state_dir, broker = _experiment_state(tmp_path)
    broker.submit(
        Order("SPY", "BUY", 1.0, decision_id="archived-entry"),
        100.0,
        "2026-06-01T10:00:00+00:00",
        dry_run=False,
    )
    broker.submit(
        Order("SPY", "SELL", 1.0, decision_id="exit"),
        105.0,
        "2026-06-01T11:00:00+00:00",
        dry_run=False,
    )
    archive_dir = state_dir / "archive"
    archive_dir.mkdir()
    with gzip.open(
        archive_dir / "decisions-2026-06.jsonl.gz",
        "wt",
        encoding="utf-8",
    ) as handle:
        handle.write(
            json.dumps(
                _decision_row(
                    "archived-entry",
                    "archive",
                    decision_grade=True,
                )
            )
            + "\n"
        )

    cohort = compute_live_kpis(state_dir)["experiment_performance"][0]

    archived_row = _decision_row(
        "archived-entry",
        "archive",
        decision_grade=True,
    )
    assert cohort["cohort"] == archived_row["experiment_id"]
    assert cohort["attribution_status"] == "attributed"
    assert cohort["net_pnl"] == pytest.approx(4.3)


def test_experiment_performance_fails_closed_on_live_archive_conflict(
    tmp_path: Path,
) -> None:
    state_dir, broker = _experiment_state(tmp_path)
    broker.submit(
        Order("SPY", "BUY", 1.0, decision_id="duplicate-entry"),
        100.0,
        "2026-06-01T10:00:00+00:00",
        dry_run=False,
    )
    broker.submit(
        Order("SPY", "SELL", 1.0, decision_id="exit"),
        105.0,
        "2026-06-01T11:00:00+00:00",
        dry_run=False,
    )
    _write_decisions(
        state_dir,
        [_decision_row("duplicate-entry", "live", decision_grade=True)],
    )
    archive_dir = state_dir / "archive"
    archive_dir.mkdir()
    with gzip.open(
        archive_dir / "decisions-2026-06.jsonl.gz",
        "wt",
        encoding="utf-8",
    ) as handle:
        handle.write(
            json.dumps(
                _decision_row(
                    "duplicate-entry",
                    "archive",
                    decision_grade=True,
                )
            )
            + "\n"
        )

    cohort = compute_live_kpis(state_dir)["experiment_performance"][0]

    assert cohort["cohort"] == "unknown"
    assert cohort["experiment_id"] is None
    assert cohort["attribution_reasons"] == {"conflicting_decision_rows": 1}


def test_gbp_migration_sentinel_excludes_unreliable_history_until_fresh(
    tmp_path: Path,
) -> None:
    state_dir, broker = _experiment_state(tmp_path)
    # The consumer requires the producer's strict quality/cutoff equality.
    _insert_gbp_attestation(state_dir)
    history_path = state_dir / "history.jsonl"
    history_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-01-01T10:00:00+00:00",
                        "equity": 999_999.0,
                    }
                ),
                # Legacy NaN is irrelevant once its valid timestamp proves it
                # belongs before the authoritative migration cutoff.
                '{"ts":"2026-01-01T11:00:00+00:00","equity":NaN}',
            ]
        ),
        encoding="utf-8",
    )

    unavailable = compute_live_kpis(state_dir)

    assert broker.cash() == 100_000.0
    assert unavailable["equity"] is None
    assert unavailable["total_return"] is None
    assert unavailable["max_drawdown"] is None
    quality = unavailable["history_quality"]
    assert quality["status"] == "unavailable"
    assert quality["reason"] == "insufficient_post_migration_history"
    assert quality["cutoff"] == "2026-01-02T00:00:00+00:00"
    assert quality["rows_total"] == 2
    assert quality["rows_used"] == 0
    assert quality["rows_excluded"] == 2
    assert quality["rows_invalid"] == 0
    assert quality["economic_metrics_available"] is False
    assert quality["migration_attestation"]["status"] == "valid"
    assert quality["migration_attestation"]["scope"]["through_seq"] == 1000
    assert quality["migration_attestation"]["scope"]["quality_through_seq"] == 1000

    with history_path.open("a", encoding="utf-8") as fh:
        fh.write(
            "\n"
            + json.dumps({"ts": "2026-01-02T01:00:00+00:00", "equity": 100_000.0})
            + "\n"
            + json.dumps({"ts": "2026-01-02T02:00:00+00:00", "equity": 99_000.0})
        )

    refreshed = compute_live_kpis(state_dir)

    assert refreshed["equity"] == 99_000.0
    assert refreshed["total_return"] == pytest.approx(-0.01)
    assert refreshed["max_drawdown"] == pytest.approx(0.01)
    assert refreshed["history_quality"]["status"] == "post_migration_only"
    assert refreshed["history_quality"]["rows_used"] == 2


def test_gbp_attestation_legitimate_absence_keeps_complete_fresh_history(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _write_history(
        state_dir,
        [
            {"ts": "2026-01-01T10:00:00+00:00", "equity": 100_000.0},
            {"ts": "2026-01-01T11:00:00+00:00", "equity": 101_000.0},
        ],
    )

    attestation = read_gbp_history_attestation(state_dir)
    curve, quality = read_equity_curve(state_dir)

    assert attestation == {
        "status": "absent",
        "reason": None,
        "cutoff": None,
        "scope": None,
    }
    assert curve == [
        ("2026-01-01T10:00:00+00:00", 100_000.0),
        ("2026-01-01T11:00:00+00:00", 101_000.0),
    ]
    assert quality["status"] == "complete"
    assert quality["economic_metrics_available"] is True


@pytest.mark.parametrize(
    ("imported_at", "scope_imported_at", "scope_store", "expected_reason"),
    [
        (
            "2026-01-02T00:00:00+00:00",
            "2026-01-02T00:01:00+00:00",
            None,
            "sentinel_timestamps_invalid_or_divergent",
        ),
        (
            "2026-01-02T00:00:00",
            "2026-01-02T00:00:00",
            None,
            "sentinel_timestamps_invalid_or_divergent",
        ),
        (
            "2026-01-02T00:00:00+00:00",
            None,
            (
                "gbp_minor_quotes_v1:scope:v1:1000:2:999:"
                + "a" * 64
                + ":"
                + "b" * 64
            ),
            "sentinel_scope_invalid",
        ),
        (
            "2026-01-02T00:00:00+00:00",
            None,
            "gbp_minor_quotes_v1:scope:v1:1000:2:1000:not-a-hash",
            "sentinel_scope_invalid",
        ),
    ],
)
def test_gbp_attestation_invalid_pair_fails_closed(
    tmp_path: Path,
    imported_at: str,
    scope_imported_at: str | None,
    scope_store: str | None,
    expected_reason: str,
) -> None:
    state_dir, _ = _experiment_state(tmp_path)
    _insert_gbp_attestation(
        state_dir,
        imported_at=imported_at,
        scope_imported_at=scope_imported_at,
        scope_store=scope_store,
    )
    _write_history(
        state_dir,
        [
            {"ts": "2026-01-03T10:00:00+00:00", "equity": 100_000.0},
            {"ts": "2026-01-03T11:00:00+00:00", "equity": 101_000.0},
        ],
    )

    attestation = read_gbp_history_attestation(state_dir)
    curve, quality = read_equity_curve(state_dir)

    assert attestation["status"] == "unavailable"
    assert attestation["reason"] == expected_reason
    assert curve == []
    assert quality["status"] == "unavailable"
    assert quality["reason"] == f"migration_attestation:{expected_reason}"
    assert quality["economic_metrics_available"] is False


def test_gbp_attestation_partial_or_ambiguous_pair_fails_closed(
    tmp_path: Path,
) -> None:
    state_dir, _ = _experiment_state(tmp_path)
    db = open_state_db(state_dir / "casys.db")
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO state_imports(store, imported_at) VALUES (?, ?)",
            ("gbp_minor_quotes_v1", "2026-01-02T00:00:00+00:00"),
        )

    partial = read_gbp_history_attestation(state_dir)

    assert partial["status"] == "unavailable"
    assert partial["reason"] == "sentinel_pair_partial_or_ambiguous"

    with db.transaction() as cur:
        for suffix in ("a", "b"):
            cur.execute(
                "INSERT INTO state_imports(store, imported_at) VALUES (?, ?)",
                (
                    "gbp_minor_quotes_v1:scope:v1:1000:2:1000:"
                    + suffix * 64
                    + ":"
                    + "c" * 64,
                    "2026-01-02T00:00:00+00:00",
                ),
            )

    ambiguous = read_gbp_history_attestation(state_dir)

    assert ambiguous["status"] == "unavailable"
    assert ambiguous["reason"] == "sentinel_pair_partial_or_ambiguous"


def test_gbp_attestation_unreadable_database_fails_closed(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "casys.db").write_bytes(b"not-a-sqlite-database")
    _write_history(
        state_dir,
        [
            {"ts": "2026-01-03T10:00:00+00:00", "equity": 100_000.0},
            {"ts": "2026-01-03T11:00:00+00:00", "equity": 101_000.0},
        ],
    )

    attestation = read_gbp_history_attestation(state_dir)
    curve, quality = read_equity_curve(state_dir)

    assert attestation["status"] == "unavailable"
    assert attestation["reason"] == "state_imports_unreadable"
    assert curve == []
    assert quality["status"] == "unavailable"
    assert quality["economic_metrics_available"] is False


@pytest.mark.parametrize(
    ("payload", "reason", "rows_invalid"),
    [
        (None, "history_missing", 0),
        ("", "insufficient_history", 0),
        (
            json.dumps(
                {"ts": "2026-01-01T10:00:00+00:00", "equity": 100_000.0}
            ),
            "insufficient_history",
            0,
        ),
        ("not-json", "history_malformed", 1),
        (json.dumps({"ts": "not-a-date", "equity": 100_000.0}), "history_malformed", 1),
    ],
)
def test_history_quality_never_certifies_missing_or_malformed_as_zero(
    tmp_path: Path,
    payload: str | None,
    reason: str,
    rows_invalid: int,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    if payload is not None:
        (state_dir / "history.jsonl").write_text(payload, encoding="utf-8")

    curve, quality = read_equity_curve(state_dir)

    assert curve == []
    assert quality["status"] == "unavailable"
    assert quality["reason"] == reason
    assert quality["rows_invalid"] == rows_invalid
    assert quality["economic_metrics_available"] is False


def test_experiment_performance_accepts_revalidated_v1_descriptor(
    tmp_path: Path,
) -> None:
    state_dir, broker = _experiment_state(tmp_path)
    broker.submit(
        Order("SPY", "BUY", 1.0, decision_id="legacy-entry"),
        100.0,
        "2026-01-01T10:00:00+00:00",
        dry_run=False,
    )
    broker.submit(
        Order("SPY", "SELL", 1.0, decision_id="exit"),
        105.0,
        "2026-01-01T11:00:00+00:00",
        dry_run=False,
    )
    legacy = _legacy_decision_row("legacy-entry", "legacy")
    _write_decisions(state_dir, [legacy])

    cohort = compute_live_kpis(state_dir)["experiment_performance"][0]

    assert cohort["cohort"] == legacy["experiment_id"]
    assert cohort["attribution_status"] == "attributed"
    assert cohort["profile"] == {
        "preset": "test-medium",
        "execution_profile": None,
    }


@pytest.mark.parametrize("invalid_kind", ["tampered_hash", "reserved_id"])
def test_experiment_performance_rejects_invalid_or_reserved_descriptor(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    state_dir, broker = _experiment_state(tmp_path)
    broker.submit(
        Order("SPY", "BUY", 1.0, decision_id="entry-invalid"),
        100.0,
        "2026-01-01T10:00:00+00:00",
        dry_run=False,
    )
    broker.submit(
        Order("SPY", "SELL", 1.0, decision_id="exit"),
        105.0,
        "2026-01-01T11:00:00+00:00",
        dry_run=False,
    )
    row = _decision_row("entry-invalid", "invalid", decision_grade=True)
    row["experiment_id"] = (
        "unknown"
        if invalid_kind == "reserved_id"
        else "exp:v2:" + "0" * 64
    )
    _write_decisions(state_dir, [row])

    cohort = compute_live_kpis(state_dir)["experiment_performance"][0]

    assert cohort["cohort"] == "unknown"
    assert cohort["attribution_status"] == "unknown"
    assert cohort["attribution_reasons"] == {
        "invalid_experiment_descriptor": 1
    }


def test_projection_decision_id_is_used_only_for_unique_exact_match(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    canonical_fills = [
        {
            "ts": "2026-01-01T10:00:00+00:00",
            "symbol": "SPY",
            "side": "BUY",
            "quantity": 1.0,
            "price": 100.0,
            "fx_rate": 1.0,
            "commission": 0.0,
            "commission_currency": "USD",
            "commission_model": "ibkr_us_stock_tiered",
        },
        {
            "ts": "2026-01-01T11:00:00+00:00",
            "symbol": "SPY",
            "side": "SELL",
            "quantity": 1.0,
            "price": 105.0,
            "fx_rate": 1.0,
            "commission": 0.0,
            "commission_currency": "USD",
            "commission_model": "ibkr_us_stock_tiered",
        },
    ]
    projection = {
        "ts": "2026-01-01T10:00:00+00:00",
        "symbol": "SPY",
        "action": "BUY",
        "quantity": 1.0,
        "price": 100.0,
        "decision_id": "projected-entry",
    }
    projection_path = state_dir / "model_performance.jsonl"
    projection_path.write_text(json.dumps(projection), encoding="utf-8")

    uniquely_matched = compute_round_trips(state_dir, fills=canonical_fills)

    assert uniquely_matched[0]["entry_decision_ids"] == ["projected-entry"]

    projection_path.write_text(
        "\n".join(json.dumps(projection) for _ in range(2)),
        encoding="utf-8",
    )
    ambiguous = compute_round_trips(state_dir, fills=canonical_fills)

    assert ambiguous[0]["entry_decision_id"] is None
    assert ambiguous[0]["entry_decision_ids"] == []


@pytest.mark.parametrize(
    "bad_fill",
    [
        {
            "ts": "2026-01-01T10:00:00+00:00",
            "symbol": "SPY",
            "side": "BUY",
            "quantity": 1.0,
            "price": 100.0,
        },
        {
            "ts": "2026-01-01T10:00:00+00:00",
            "symbol": "SPY",
            "side": "BUY",
            "quantity": float("nan"),
            "price": 100.0,
        },
        {
            "ts": "2026-01-01T10:00:00+00:00",
            "symbol": "SPY",
            "side": "BUY",
            "quantity": 1.0,
            "price": float("inf"),
        },
        {
            "ts": "2026-01-01T10:00:00+00:00",
            "symbol": "SPY",
            "side": "BUY",
            "quantity": 1.0,
            "price": 100.0,
            "commission": -1.0,
        },
        {
            "ts": "2026-01-01T10:00:00+00:00",
            "symbol": "SPY",
            "side": "BUY",
            "quantity": 1.0,
            "price": 100.0,
            "commission": float("nan"),
        },
        {
            "ts": "2026-01-01T10:00:00+00:00",
            "symbol": "SPY",
            "side": "BUY",
            "quantity": 1e308,
            "price": 1e308,
        },
        {
            "ts": "2026-01-01T10:00:00+00:00",
            "symbol": "SAP.DE",
            "side": "BUY",
            "quantity": 1.0,
            "price": 100.0,
            "fx_rate": 1e308,
            "commission": 1e308,
            "commission_currency": "EUR",
            "commission_model": "ibkr_europe_stock_tiered",
        },
    ],
)
def test_invalid_fill_economics_preserves_valid_broker_snapshot(
    tmp_path: Path,
    bad_fill: dict,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "universe.yaml").write_text(
        yaml.dump({"starting_cash": 100_000.0, "symbols": ["SPY"]}),
        encoding="utf-8",
    )
    (state_dir / "broker.json").write_text(
        json.dumps(
            {
                "cash": 100_000.0,
                "positions": {},
                "fills": [bad_fill],
            }
        ),
        encoding="utf-8",
    )

    kpis = compute_live_kpis(state_dir)

    assert kpis["broker_quality"]["status"] == "available"
    assert kpis["broker_quality"]["reason"] is None
    assert kpis["cash"] == 100_000.0
    assert kpis["num_fills"] == 1
    assert kpis["n_positions"] == 0
    assert kpis["positions"] == []
    assert kpis["trade_economics_quality"]["status"] == "unavailable"
    assert kpis["trade_economics_quality"]["reason"] == (
        "canonical_fills_invalid"
    )
    for field in (
        "num_trades",
        "num_closed_position_cycles",
        "experiment_performance",
    ):
        assert kpis[field] is None


def test_unreadable_canonical_broker_database_never_projects_empty_portfolio(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "universe.yaml").write_text(
        yaml.dump({"starting_cash": 100_000.0, "symbols": ["SPY"]}),
        encoding="utf-8",
    )
    (state_dir / "casys.db").write_bytes(b"not-a-sqlite-database")

    kpis = compute_live_kpis(state_dir)

    assert kpis["broker_quality"]["status"] == "unavailable"
    assert kpis["broker_quality"]["source"] == "sqlite"
    assert kpis["broker_quality"]["reason"] == "broker_read_error"
    for field in (
        "cash",
        "num_trades",
        "num_closed_position_cycles",
        "num_fills",
        "n_positions",
        "positions",
        "experiment_performance",
    ):
        assert kpis[field] is None


@pytest.mark.parametrize(
    ("projection_kind", "expected_status"),
    [
        ("directory", "unavailable"),
        ("invalid_utf8", "unavailable"),
        ("non_mapping_json", "degraded"),
    ],
)
def test_model_performance_projection_is_advisory_and_fail_soft(
    tmp_path: Path,
    projection_kind: str,
    expected_status: str,
) -> None:
    state_dir, broker = _experiment_state(tmp_path)
    broker.submit(
        Order("SPY", "BUY", 1.0),
        100.0,
        "2026-01-01T10:00:00+00:00",
        dry_run=False,
    )
    broker.submit(
        Order("SPY", "SELL", 1.0),
        105.0,
        "2026-01-01T11:00:00+00:00",
        dry_run=False,
    )
    projection_path = state_dir / "model_performance.jsonl"
    if projection_kind == "directory":
        projection_path.mkdir()
    elif projection_kind == "invalid_utf8":
        projection_path.write_bytes(b"\xff\xfe")
    else:
        projection_path.write_text("[]", encoding="utf-8")

    kpis = compute_live_kpis(state_dir)

    assert kpis["broker_quality"]["status"] == "available"
    assert kpis["num_closed_position_cycles"] == 1
    assert kpis["model_performance"] == []
    assert kpis["model_performance_quality"]["status"] == expected_status


@pytest.mark.parametrize(
    "ledger_kind",
    ["directory", "invalid_utf8", "corrupt_archive"],
)
def test_unreadable_decision_ledger_only_disables_experiment_attribution(
    tmp_path: Path,
    ledger_kind: str,
) -> None:
    state_dir, broker = _experiment_state(tmp_path)
    broker.submit(
        Order("SPY", "BUY", 1.0, decision_id="entry-unreadable"),
        100.0,
        "2026-01-01T10:00:00+00:00",
        dry_run=False,
    )
    broker.submit(
        Order("SPY", "SELL", 1.0, decision_id="exit"),
        105.0,
        "2026-01-01T11:00:00+00:00",
        dry_run=False,
    )
    ledger_path = state_dir / "decisions.jsonl"
    if ledger_kind == "directory":
        ledger_path.mkdir()
    elif ledger_kind == "invalid_utf8":
        ledger_path.write_bytes(b"\xff\xfe")
    else:
        archive_dir = state_dir / "archive"
        archive_dir.mkdir()
        (archive_dir / "decisions-2026-01.jsonl.gz").write_bytes(
            b"not-gzip"
        )

    kpis = compute_live_kpis(state_dir)

    assert kpis["broker_quality"]["status"] == "available"
    assert kpis["num_closed_position_cycles"] == 1
    cohort = kpis["experiment_performance"][0]
    assert cohort["cohort"] == "unknown"
    assert cohort["gross_pnl"] == pytest.approx(5.0)
    assert cohort["attribution_reasons"] == {"attribution_unavailable": 1}
    assert cohort["attribution_quality"]["status"] == "unavailable"
    assert cohort["attribution_quality"]["reason"] == (
        "decision_ledger_unreadable"
    )


@pytest.mark.parametrize(
    ("symbol", "model", "currency", "reason"),
    [
        ("SPY", "none", "USD", "commission_not_modeled"),
        ("ERIC-B.ST", "ibkr_unknown", "USD", "commission_model_unavailable"),
        (
            "AZN.L",
            "ibkr_us_stock_tiered",
            "USD",
            "commission_model_incompatible",
        ),
    ],
)
def test_incomplete_commission_model_preserves_gross_but_not_net(
    tmp_path: Path,
    symbol: str,
    model: str,
    currency: str,
    reason: str,
) -> None:
    fx_rate = 1.0 if symbol == "SPY" else 1.25
    fills = [
        {
            "ts": "2026-01-01T10:00:00+00:00",
            "symbol": symbol,
            "side": "BUY",
            "quantity": 1.0,
            "price": 100.0,
            "fx_rate": fx_rate,
            "commission": 0.0,
            "commission_currency": currency,
            "commission_model": model,
        },
        {
            "ts": "2026-01-01T11:00:00+00:00",
            "symbol": symbol,
            "side": "SELL",
            "quantity": 1.0,
            "price": 110.0,
            "fx_rate": fx_rate,
            "commission": 0.0,
            "commission_currency": currency,
            "commission_model": model,
        },
    ]

    trip = compute_round_trips(tmp_path, fills=fills)[0]
    cycle = aggregate_position_cycles([trip])[0]

    assert trip["gross_pnl"] == pytest.approx(10.0 * fx_rate)
    assert trip["commission"] is None
    assert trip["pnl"] is None
    assert trip["commission_quality"]["reason"] == reason
    assert cycle["gross_pnl"] == pytest.approx(10.0 * fx_rate)
    assert cycle["commission"] is None
    assert cycle["pnl"] is None
    assert cycle["commission_quality"]["reason"] == reason


def test_live_experiment_cohort_does_not_certify_unknown_commission_net(
    tmp_path: Path,
) -> None:
    state_dir, broker = _experiment_state(tmp_path)
    broker.submit(
        Order("SPY", "BUY", 1.0, decision_id="entry-fee-unknown"),
        100.0,
        "2026-01-01T10:00:00+00:00",
        dry_run=False,
    )
    broker.submit(
        Order("SPY", "SELL", 1.0, decision_id="exit"),
        110.0,
        "2026-01-01T11:00:00+00:00",
        dry_run=False,
    )
    db = open_state_db(state_dir / "casys.db")
    with db.transaction() as cur:
        cur.execute(
            "UPDATE broker_fills SET commission_model='ibkr_unknown' "
            "WHERE seq=1"
        )
    decision = _decision_row(
        "entry-fee-unknown",
        "fee-unknown",
        decision_grade=True,
    )
    _write_decisions(state_dir, [decision])

    kpis = compute_live_kpis(state_dir)

    assert kpis["broker_quality"]["status"] == "available"
    assert kpis["num_closed_position_cycles"] == 1
    assert kpis["trade_economics_quality"] == {
        "status": "incomplete",
        "reason": "commission_or_net_economics_incomplete",
        "models": ["ibkr_unknown"],
        "reasons": ["commission_model_unavailable"],
        "completed_cycles": 1,
        "complete_cycles": 0,
        "incomplete_cycles": 1,
        "gross_pnl_available": True,
        "commission_and_net_available": False,
    }
    cohort = kpis["experiment_performance"][0]
    assert cohort["cohort"] == decision["experiment_id"]
    assert cohort["completed_cycles"] == 1
    assert cohort["gross_pnl"] == pytest.approx(10.0)
    assert cohort["commissions"] is None
    assert cohort["net_pnl"] is None
    assert cohort["wins"] is None
    assert cohort["win_rate"] is None
    assert cohort["commission_quality"] == {
        "status": "unavailable",
        "reason": "commission_model_unavailable",
        "models": ["ibkr_unknown"],
        "reasons": ["commission_model_unavailable"],
        "complete_cycles": 0,
        "incomplete_cycles": 1,
    }
