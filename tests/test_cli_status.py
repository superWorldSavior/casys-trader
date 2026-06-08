import json

from trader import cli, daemon


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
