import json

from backtest.data import HistoryStore
from trader import cli, daemon
from trader.tools.market import Bar


def _history_bar(ts: str, close: float) -> Bar:
    return Bar(ts=ts, open=close, high=close + 1.0, low=close - 1.0, close=close, volume=1000.0)


def _write_perf(state_dir, rows: list[dict]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "model_performance.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def test_cli_status_json_expose_les_fichiers_runtime(monkeypatch, tmp_path, capsys) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    (state_dir / "daemon_status.json").write_text(
        json.dumps(
            {
                "phase": "deciding_symbol",
                "current_symbol": "SPY",
                "decisions_done": 1,
                "symbols_total": 2,
            }
        )
    )
    (state_dir / "broker.json").write_text(
        json.dumps(
            {
                "cash": 90000.0,
                "positions": {"SPY": {"symbol": "SPY", "quantity": 10.0, "avg_price": 100.0}},
                "fills": [],
            }
        )
    )
    (state_dir / "current_report.json").write_text(json.dumps({"decisions": [{"symbol": "SPY"}]}))

    assert cli.main(["status", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["daemon_status"]["phase"] == "deciding_symbol"
    assert payload["broker"]["cash"] == 90000.0
    assert payload["current_report"]["decisions"][0]["symbol"] == "SPY"


def test_cli_decisions_seed_existing_puis_liste_json(monkeypatch, tmp_path, capsys) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    report = {
        "ts": "2026-06-08T12:15:21+00:00",
        "dry_run": False,
        "symbols_due": ["SPY"],
        "prices": {"SPY": 532.12},
        "portfolio": {"cash": 100000.0, "equity": 100000.0},
        "decisions": [
            {
                "symbol": "SPY",
                "action": "HOLD",
                "qty": 0.0,
                "confidence": 0.6,
                "rationale": "range",
                "intent": "HOLD",
                "executed": False,
                "reason": "hold",
            }
        ],
    }
    (state_dir / "last_report.json").write_text(json.dumps(report), encoding="utf-8")

    assert cli.main(["decisions", "seed-existing", "--json"]) == 0
    seed_payload = json.loads(capsys.readouterr().out)
    assert seed_payload["appended"] == 1

    assert cli.main(["decisions", "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["decision_id"] == "2026-06-08T12:15:21+00:00|0|SPY"
    assert rows[0]["price"] == 532.12


def test_cli_decisions_seed_events_inclut_les_archives(monkeypatch, tmp_path, capsys) -> None:
    state_dir = tmp_path / "state"
    archive_dir = tmp_path / "state_archive_old"
    state_dir.mkdir()
    archive_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    for path, symbol in [(state_dir / "events.jsonl", "SPY"), (archive_dir / "events.jsonl", "QQQ")]:
        path.write_text(
            json.dumps(
                {
                    "ts": "2026-06-08T12:15:21+00:00",
                    "event": "decision_recorded",
                    "symbol": symbol,
                    "action": "HOLD",
                    "reason": "hold",
                    "executed": False,
                }
            )
            + "\n",
            encoding="utf-8",
        )

    assert cli.main(["decisions", "seed-events", "--include-archives", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["event_files"] == 2
    assert payload["appended"] == 2


def test_cli_decisions_audit_ecrit_un_rapport(monkeypatch, tmp_path, capsys) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    (state_dir / "decisions.jsonl").write_text(
        json.dumps(
            {
                "decision_id": "2026-06-08T10:00:00+00:00|0|SPY",
                "cycle_ts": "2026-06-08T10:00:00+00:00",
                "symbol": "SPY",
                "action": "BUY",
                "price": 100.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def load_prices(symbols, *, start, end, interval):
        assert symbols == ["SPY"]
        assert interval == "1h"
        return {"SPY": [{"ts": "2026-06-08T11:00:00+00:00", "close": 101.0}]}

    monkeypatch.setattr(cli.decision_audit, "load_prices_yfinance", load_prices)
    monkeypatch.setattr(
        cli.code_version,
        "current_code_version",
        lambda root: {"git_commit_short": "cafebabecafe", "git_dirty": False},
    )

    assert cli.main(["decisions", "audit", "--horizons", "1h", "--threshold-pct", "0.5", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["1h"]["good"] == 1
    assert payload["audit_code_version"]["git_commit_short"] == "cafebabecafe"
    saved = json.loads((state_dir / "decision_audit.json").read_text(encoding="utf-8"))
    assert saved["rows"][0]["audits"]["1h"]["verdict"] == "good"


def test_cli_decisions_backfill_code_version_met_a_jour_ledger_et_audit(monkeypatch, tmp_path, capsys) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    decision_id = "2026-06-08T10:00:00+00:00|0|SPY"
    row = {
        "decision_id": decision_id,
        "cycle_ts": "2026-06-08T10:00:00+00:00",
        "symbol": "SPY",
        "action": "BUY",
        "price": 100.0,
        "code_version": {"source": "unknown", "git_commit": None},
    }
    fresh_row = {
        "decision_id": "2026-06-08T11:00:00+00:00|1|QQQ",
        "cycle_ts": "2026-06-08T11:00:00+00:00",
        "symbol": "QQQ",
        "action": "HOLD",
        "price": 200.0,
        "code_version": {
            "source": "git",
            "git_commit": "already1234567890",
            "git_commit_short": "already12345",
            "git_dirty": False,
        },
    }
    (state_dir / "decisions.jsonl").write_text(
        json.dumps(row) + "\n" + json.dumps(fresh_row) + "\n",
        encoding="utf-8",
    )
    (state_dir / "decision_audit.json").write_text(
        json.dumps(
            {
                "threshold_pct": 0.5,
                "horizons": ["1h"],
                "rows": [
                    {
                        **row,
                        "audits": {
                            "1h": {
                                "entry_price": 100.0,
                                "future_price": 101.0,
                                "future_return_pct": 1.0,
                                "verdict": "good",
                            }
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        cli.decision_ledger.code_version,
        "historical_code_version",
        lambda repo_root, decision_ts, *, ref="HEAD": {
            "source": "git_history",
            "git_commit": "feedface12345678",
            "git_commit_short": "feedface1234",
            "git_branch": "main",
            "git_dirty": None,
            "git_dirty_files": [],
        },
    )
    monkeypatch.setattr(
        cli.code_version,
        "current_code_version",
        lambda repo_root: {"source": "git", "git_commit_short": "cafebabecafe", "git_dirty": False},
    )

    assert cli.main(["decisions", "backfill-code-version", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["updated"] == 1
    assert payload["skipped"] == 1
    assert payload["audit_updated"] is True
    ledger_rows = [
        json.loads(line)
        for line in (state_dir / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert ledger_rows[0]["code_version"]["git_commit_short"] == "feedface1234"
    audit = json.loads((state_dir / "decision_audit.json").read_text(encoding="utf-8"))
    assert audit["rows"][0]["decision_commit_key"] == "feedface1234"
    assert len(audit["rows"]) == 2
    assert audit["rows"][1]["decision_commit_key"] == "already12345"
    assert audit["rows"][1]["audits"]["1h"]["verdict"] == "unknown"
    assert audit["metrics_by_commit"]["1h"]["feedface1234"]["good_known_pct"] == 100.0


def test_cli_decisions_stats_affiche_n_et_pourcentages_par_commit(monkeypatch, tmp_path, capsys) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    (state_dir / "decision_audit.json").write_text(
        json.dumps(
            {
                "metrics": {
                    "1h": {
                        "total": 12,
                        "known": 8,
                        "unknown": 4,
                        "good_known_pct": 62.5,
                        "bad_known_pct": 25.0,
                        "nonbad_known_pct": 75.0,
                    }
                },
                "metrics_by_commit": {
                    "1h": {
                        "bigcommit123": {
                            "total": 10,
                            "known": 8,
                            "unknown": 2,
                            "good_known_pct": 62.5,
                            "bad_known_pct": 25.0,
                            "nonbad_known_pct": 75.0,
                        },
                        "tinycommit45": {
                            "total": 2,
                            "known": 0,
                            "unknown": 2,
                            "good_known_pct": None,
                            "bad_known_pct": None,
                            "nonbad_known_pct": None,
                        },
                    }
                },
                "rows": [
                    {
                        "decision_id": "d1",
                        "decision_commit_key": "bigcommit123",
                        "code_version": {
                            "git_commit_short": "bigcommit123",
                            "inference": {"commit_date": "2026-06-08T08:00:00+00:00"},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert cli.main(["decisions", "stats", "--horizon", "1h", "--min-known", "1"]) == 0

    out = capsys.readouterr().out
    assert "commit" in out
    assert "date" in out
    assert "known" in out
    assert "good%" in out
    assert "bigcommit123" in out
    assert "2026-06-08" in out
    assert "10" in out
    assert "8" in out
    assert "62.5" in out
    assert "tinycommit45" not in out

    assert cli.main(["decisions", "stats", "--horizon", "1h", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["horizon"] == "1h"
    assert payload["global"]["known"] == 8
    assert payload["commits"][0]["commit"] == "bigcommit123"
    assert payload["commits"][0]["date"] == "2026-06-08"


def test_stats_payload_expose_missed_par_commit() -> None:
    audit = {
        "metrics": {
            "1h": {"total": 1, "known": 1, "unknown": 0, "missed_known_pct": 100.0}
        },
        "metrics_by_commit": {
            "1h": {
                "c1": {
                    "total": 1,
                    "known": 1,
                    "unknown": 0,
                    "good": 0,
                    "bad": 0,
                    "neutral": 0,
                    "missed": 1,
                    "missed_known_pct": 100.0,
                }
            }
        },
        "rows": [],
    }

    payload = cli._decision_stats_payload(audit, horizon="1h", min_known=0)

    row = payload["commits"][0]
    assert row["missed"] == 1
    assert row["missed_pct"] == 100.0


def test_cli_diagnostics_hard_stops_json(monkeypatch, tmp_path, capsys) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    _write_perf(
        state_dir,
        [
            {"ts": "2026-06-05T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 10, "price": 100.0, "confidence": 0.8, "intent": "OPEN_LONG"},
            {"ts": "2026-06-05T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 10, "price": 95.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
        ],
    )

    def get_bars(symbol, *, lookback, interval):
        assert symbol == "SPY"
        assert lookback == "5d"
        assert interval == "1h"
        return [
            Bar(ts="2026-06-05T12:00:00+00:00", open=95.0, high=99.0, low=94.0, close=96.0, volume=1000.0),
            Bar(ts="2026-06-05T13:00:00+00:00", open=96.0, high=108.0, low=95.0, close=106.0, volume=1000.0),
        ]

    monkeypatch.setattr(cli.market, "get_bars", get_bars)

    assert cli.main(["diagnostics", "hard-stops", "--json", "--all-regimes", "--lookback", "5d"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["hard_stops"] == 1
    assert payload["summary"]["stop_too_early"] == 1
    assert payload["cases"][0]["symbol"] == "SPY"
    assert payload["cases"][0]["hold_to_lookahead_pnl"] == 60.0


def test_cli_decisions_bench_dry_run_preview_les_cases(monkeypatch, tmp_path, capsys) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    (state_dir / "decision_audit.json").write_text(
        json.dumps(
            {
                "threshold_pct": 0.5,
                "horizons": ["4h"],
                "rows": [
                    {
                        "decision_id": "d1",
                        "cycle_ts": "2026-06-12T01:00:00+00:00",
                        "symbol": "SPY",
                        "action": "HOLD",
                        "intent": "HOLD",
                        "confidence": 0.9,
                        "rationale": "range",
                        "reason": "hold",
                        "price": 100.0,
                        "portfolio_snapshot": {"cash": 100000.0, "equity": 100000.0},
                        "market_snapshot": {"stale_market_data": None},
                        "runtime": {"data_source": "ib"},
                        "audits": {
                            "4h": {
                                "entry_price": 100.0,
                                "future_price": 101.0,
                                "future_return_pct": 1.0,
                                "verdict": "missed",
                            }
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert (
        cli.main(
            [
                "decisions",
                "bench",
                "--horizon",
                "4h",
                "--limit",
                "1",
                "--models",
                "spark:m",
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["models"] == [{"provider": "acpx", "model": "m"}]
    assert payload["cases"][0]["decision_id"] == "d1"
    assert "future_return_pct" not in json.dumps(payload["prompt"], ensure_ascii=False)


def test_cli_decisions_bench_offset_choisit_le_batch_suivant(monkeypatch, tmp_path, capsys) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    rows = []
    for idx in range(3):
        rows.append(
            {
                "decision_id": f"d{idx + 1}",
                "cycle_ts": f"2026-06-12T0{idx + 1}:00:00+00:00",
                "symbol": "SPY",
                "action": "HOLD",
                "price": 100.0,
                "audits": {
                    "4h": {
                        "entry_price": 100.0,
                        "future_price": 101.0,
                        "future_return_pct": 1.0,
                        "verdict": "missed",
                    }
                },
            }
        )
    (state_dir / "decision_audit.json").write_text(
        json.dumps({"threshold_pct": 0.5, "horizons": ["4h"], "rows": rows}),
        encoding="utf-8",
    )

    assert (
        cli.main(
            [
                "decisions",
                "bench",
                "--horizon",
                "4h",
                "--limit",
                "1",
                "--offset",
                "1",
                "--models",
                "spark:m",
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["offset"] == 1
    assert [case["decision_id"] for case in payload["cases"]] == ["d2"]


def test_cli_decisions_bench_dry_run_reconstruit_le_contexte(monkeypatch, tmp_path, capsys) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "universe.yaml").write_text("symbols: [SPY, QQQ, DIA]\n", encoding="utf-8")
    (state_dir / "decision_audit.json").write_text(
        json.dumps(
            {
                "threshold_pct": 0.5,
                "horizons": ["4h"],
                "rows": [
                    {
                        "decision_id": "d1",
                        "cycle_ts": "2026-06-12T01:00:00+00:00",
                        "symbol": "NG=F",
                        "action": "HOLD",
                        "price": 100.0,
                        "portfolio_snapshot": {"cash": 100000.0, "equity": 100000.0},
                        "market_snapshot": {"stale_market_data": None},
                        "runtime": {"data_source": "ib"},
                        "audits": {
                            "4h": {
                                "entry_price": 100.0,
                                "future_price": 101.0,
                                "future_return_pct": 1.0,
                                "verdict": "missed",
                            }
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = HistoryStore.from_bars(
        {
            "SPY": [
                _history_bar("2026-06-12T00:00:00+00:00", 100.0),
                _history_bar("2026-06-12T00:15:00+00:00", 101.0),
                _history_bar("2026-06-12T00:30:00+00:00", 102.0),
                _history_bar("2026-06-12T01:00:00+00:00", 103.0),
            ],
            "QQQ": [
                _history_bar("2026-06-12T00:00:00+00:00", 200.0),
                _history_bar("2026-06-12T00:15:00+00:00", 201.0),
                _history_bar("2026-06-12T00:30:00+00:00", 202.0),
                _history_bar("2026-06-12T01:00:00+00:00", 203.0),
            ],
            "DIA": [
                _history_bar("2026-06-12T00:00:00+00:00", 300.0),
                _history_bar("2026-06-12T00:15:00+00:00", 301.0),
                _history_bar("2026-06-12T00:30:00+00:00", 302.0),
                _history_bar("2026-06-12T01:00:00+00:00", 303.0),
            ],
            "NG=F": [
                _history_bar("2026-06-12T00:00:00+00:00", 3.0),
                _history_bar("2026-06-12T00:15:00+00:00", 3.1),
                _history_bar("2026-06-12T00:30:00+00:00", 3.2),
                _history_bar("2026-06-12T01:00:00+00:00", 3.3),
            ],
        }
    )

    def load_history(cases, *, symbols, interval, padding_days):
        assert [case["decision_id"] for case in cases] == ["d1"]
        assert symbols == ["SPY", "QQQ", "DIA", "NG=F"]
        assert interval == "15m"
        assert padding_days == 10
        return store, {
            "enabled": True,
            "source": "history_asof_reconstruction",
            "exact_replay": False,
            "interval": interval,
            "symbols": symbols,
            "loaded_symbols": symbols,
            "unavailable_symbols": [],
            "window": {"start": "2026-06-02", "end": "2026-06-13", "padding_days": padding_days},
        }

    monkeypatch.setattr(cli.decision_bench, "load_reconstruction_history", load_history)

    assert (
        cli.main(
            [
                "decisions",
                "bench",
                "--horizon",
                "4h",
                "--limit",
                "1",
                "--models",
                "spark:m",
                "--dry-run",
                "--reconstruct-context",
                "--context-symbols",
                "universe",
                "--context-lookback-bars",
                "4",
                "--cockpit-window",
                "3",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    context = payload["cases"][0]["case"]["reconstructed_context"]
    assert payload["context_reconstruction"]["enabled"] is True
    assert payload["context_reconstruction"]["exact_replay"] is False
    assert context["prices"]["NG=F"] == 3.3
    assert "reconstructed_context" in payload["prompt"]
