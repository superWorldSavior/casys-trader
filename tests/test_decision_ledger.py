import json

from trader import decision_ledger


def _report(decisions: list[dict] | None = None) -> dict:
    return {
        "ts": "2026-06-08T12:15:21+00:00",
        "dry_run": False,
        "symbols_due": ["SPY", "QQQ"],
        "prices": {"SPY": 532.12, "QQQ": 461.5},
        "portfolio": {"cash": 100000.0, "equity": 100000.0, "holdings": []},
        "stale_market_data": {},
        "model_calls_used": 1,
        "code_version": {
            "git_commit": "abcdef1234567890",
            "git_commit_short": "abcdef123456",
            "git_branch": "main",
            "git_dirty": False,
        },
        "decisions": decisions or [],
    }


def _decision(symbol: str = "SPY", action: str = "HOLD") -> dict:
    return {
        "symbol": symbol,
        "action": action,
        "qty": 0.0,
        "confidence": 0.62,
        "rationale": "range sans catalyseur",
        "intent": "HOLD",
        "llm_provider": "spark",
        "llm_model": "gpt-5.3-codex-spark/medium",
        "executed": False,
        "reason": "hold",
    }


def test_build_decision_row_normalise_une_decision_pour_audit() -> None:
    report = _report([_decision()])

    row = decision_ledger.build_decision_row(
        report,
        report["decisions"][0],
        sequence=0,
        source="daemon",
    )

    assert row["schema_version"] == 1
    assert row["decision_id"] == "2026-06-08T12:15:21+00:00|0|SPY"
    assert row["cycle_ts"] == "2026-06-08T12:15:21+00:00"
    assert row["symbol"] == "SPY"
    assert row["action"] == "HOLD"
    assert row["intent"] == "HOLD"
    assert row["price"] == 532.12
    assert row["executed"] is False
    assert row["reason"] == "hold"
    assert row["code_version"]["git_commit_short"] == "abcdef123456"
    assert row["code_version"]["git_branch"] == "main"
    assert row["decision"]["rationale"] == "range sans catalyseur"
    assert row["market_snapshot"] == {
        "price": 532.12,
        "stale_market_data": None,
        "symbols_due": ["SPY", "QQQ"],
        "model_calls_used": 1,
    }
    assert row["portfolio_snapshot"]["equity"] == 100000.0
    assert row["labels"] == {}


def test_build_decision_row_propage_les_rejets_indicator_watch() -> None:
    """La ligne d'audit conserve l'intention indicator_watch et ses rejets."""
    decision = _decision()
    rejections = [{"reason": "non_finite_threshold", "indicator": "z_score", "raw_value": None}]
    decision["indicator_watch_requested"] = True
    decision["indicator_watch_rejections"] = rejections
    report = _report([decision])

    row = decision_ledger.build_decision_row(report, decision, sequence=0, source="daemon")

    assert row["runtime"]["indicator_watch_requested"] is True
    assert row["runtime"]["indicator_watch_rejections"] == rejections


def test_build_decision_row_propage_context_request_et_next_wake_requested() -> None:
    """La ligne d'audit garde les primitives de trace sans blob redondant."""
    decision = _decision()
    context_request = {
        "rounds": 1,
        "requested": [{"symbol": "SPY", "indicators": ["z_score"], "timeframe": "1h"}],
        "resolved": 1,
    }
    decision["context_request"] = context_request
    decision["next_wake_requested"] = 120.0
    decision["next_wake_in_minutes"] = 60.0
    report = _report([decision])

    row = decision_ledger.build_decision_row(report, decision, sequence=0, source="daemon")

    assert row["next_wake_in_minutes"] == 60.0
    assert row["runtime"]["context_request"] == context_request
    assert row["runtime"]["next_wake_requested"] == 120.0


def test_build_decision_row_propage_les_champs_risque_runtime() -> None:
    decision = _decision(action="BUY")
    decision.update(
        {
            "intent": "OPEN_LONG",
            "qty": 150.0,
            "executed": True,
            "reason": "ok",
            "risk_pct": 0.0075,
            "stop_distance": 5.0,
            "risk_clamped": True,
            "risk_unbounded_no_stop": False,
        }
    )
    report = _report([decision])

    row = decision_ledger.build_decision_row(report, decision, sequence=0, source="daemon")

    assert row["runtime"]["risk_pct"] == 0.0075
    assert row["runtime"]["stop_distance"] == 5.0
    assert row["runtime"]["risk_clamped"] is True
    assert row["runtime"]["risk_unbounded_no_stop"] is False


def test_decision_ledger_append_est_idempotent(tmp_path) -> None:
    store = decision_ledger.DecisionLedgerStore(tmp_path / "decisions.jsonl")
    row = decision_ledger.build_decision_row(_report([_decision()]), _decision(), sequence=0)

    assert store.append(row) is True
    assert store.append(row) is False

    rows = store.read_all()
    assert len(rows) == 1
    assert rows[0]["decision_id"] == row["decision_id"]


def test_seed_existing_reports_recupere_last_et_current_sans_doublons(tmp_path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    first = _decision("SPY")
    second = _decision("QQQ", action="SELL")
    (state_dir / "last_report.json").write_text(json.dumps(_report([first])), encoding="utf-8")
    (state_dir / "current_report.json").write_text(json.dumps(_report([first, second])), encoding="utf-8")

    result = decision_ledger.seed_existing_reports(state_dir)

    assert result == {"reports": 2, "candidates": 3, "appended": 2, "skipped": 1}
    rows = decision_ledger.DecisionLedgerStore(state_dir / "decisions.jsonl").read_all()
    assert [row["decision_id"] for row in rows] == [
        "2026-06-08T12:15:21+00:00|0|SPY",
        "2026-06-08T12:15:21+00:00|1|QQQ",
    ]

    rerun = decision_ledger.seed_existing_reports(state_dir)
    assert rerun == {"reports": 2, "candidates": 3, "appended": 0, "skipped": 3}


def test_seed_existing_events_recupere_les_decisions_legacy_sans_doublonner(tmp_path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = decision_ledger.DecisionLedgerStore(state_dir / "decisions.jsonl")
    store.append(
        decision_ledger.build_decision_row(
            {"ts": "2026-06-08T12:15:21+00:00", "decisions": [_decision("SPY")]},
            _decision("SPY"),
            sequence=0,
            source="daemon",
        )
    )
    events = [
        {
            "ts": "2026-06-08T12:15:50+00:00",
            "event": "decision_recorded",
            "symbol": "SPY",
            "action": "HOLD",
            "reason": "hold",
            "executed": False,
        },
        {
            "ts": "2026-06-08T12:16:00+00:00",
            "event": "decision_recorded",
            "symbol": "QQQ",
            "action": "SELL",
            "reason": "ok",
            "executed": True,
        },
    ]
    with (state_dir / "events.jsonl").open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")

    result = decision_ledger.seed_existing_events(state_dir)

    assert result == {"event_files": 1, "candidates": 2, "appended": 1, "skipped": 1}
    rows = store.read_all()
    assert len(rows) == 2
    assert rows[1]["decision_id"] == "2026-06-08T12:16:00+00:00|legacy|1|QQQ"
    assert rows[1]["source"] == "event_seed:events.jsonl"
    assert rows[1]["action"] == "SELL"
    assert rows[1]["intent"] == "UNKNOWN"
    assert rows[1]["quality"] == "legacy_event_summary"
    assert rows[1]["code_version"]["source"] == "unknown"
    assert rows[1]["decision"]["rationale"] == "legacy_event:ok"
    assert rows[1]["runtime"]["context_request"] is None
    assert rows[1]["runtime"]["next_wake_requested"] is None


def test_decision_ledger_filtre_par_symbole_et_limite(tmp_path) -> None:
    store = decision_ledger.DecisionLedgerStore(tmp_path / "decisions.jsonl")
    for index, symbol in enumerate(["SPY", "QQQ", "SPY"]):
        store.append(
            decision_ledger.build_decision_row(
                _report([_decision(symbol)]),
                _decision(symbol),
                sequence=index,
            )
        )

    rows = store.read_all(symbol="SPY", limit=1)

    assert len(rows) == 1
    assert rows[0]["decision_id"] == "2026-06-08T12:15:21+00:00|2|SPY"


def test_backfill_code_versions_complete_les_lignes_historiques(monkeypatch, tmp_path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = decision_ledger.DecisionLedgerStore(state_dir / "decisions.jsonl")
    unknown = decision_ledger.build_legacy_event_row(
        {
            "ts": "2026-06-08T12:16:00+00:00",
            "symbol": "SPY",
            "action": "HOLD",
            "reason": "hold",
            "executed": False,
        },
        sequence=0,
        source="event_seed:events.jsonl",
    )
    known = decision_ledger.build_decision_row(_report([_decision("QQQ")]), _decision("QQQ"), sequence=1)
    store.append(unknown)
    store.append(known)

    def fake_historical_code_version(repo_root, decision_ts, *, ref="HEAD"):
        assert decision_ts == "2026-06-08T12:16:00+00:00"
        return {
            "source": "git_history",
            "git_commit": "1111111111112222",
            "git_commit_short": "111111111111",
            "git_branch": "main",
            "git_dirty": None,
            "git_dirty_files": [],
        }

    monkeypatch.setattr(decision_ledger.code_version, "historical_code_version", fake_historical_code_version)

    result = decision_ledger.backfill_code_versions(state_dir, repo_root=tmp_path)

    assert result == {
        "rows": 2,
        "updated": 1,
        "skipped": 1,
        "unresolved": 0,
        "overwrite": False,
        "ref": "HEAD",
    }
    rows = store.read_all()
    assert rows[0]["code_version"]["git_commit_short"] == "111111111111"
    assert rows[1]["code_version"]["git_commit_short"] == "abcdef123456"
