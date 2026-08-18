import json

from trader.application.record.decision_ledger_rows import decision_row_mandate_ref
from trader.reporting.ledger import decision_ledger


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
        "since_open_m": None,
        "to_close_m": None,
        "venue": None,
    }
    assert row["portfolio_snapshot"]["equity"] == 100000.0
    assert row["labels"] == {}
    assert row["mandate_ref"] is None


def test_decision_row_persists_trade_evaluation_audit_fields() -> None:
    decision = {
        **_decision(action="BUY"),
        "intent": "OPEN_LONG",
        "qty": 2.0,
        "trade_evaluation_id": "tpe_audit",
        "entry_dimensions": {
            "setup": "breakout",
            "horizon": "swing",
            "side": "long",
            "regime": "trend",
        },
        "trade_plan_evaluation": {
            "evaluation_id": "tpe_audit",
            "as_of": "2026-06-08T12:15:00+00:00",
            "economics": {
                "status": "positive",
                "expected_value_usd": 12.5,
                "p_break_even": 0.4,
            },
        },
        "trade_evaluation_rejection": "trade_evaluation_stale",
    }

    row = decision_ledger.build_decision_row(
        _report([decision]),
        decision,
        sequence=0,
    )

    assert row["trade_evaluation_id"] == "tpe_audit"
    assert row["trade_evaluation_as_of"] == "2026-06-08T12:15:00+00:00"
    assert row["trade_economics_status"] == "positive"
    assert row["trade_evaluation_rejection"] == "trade_evaluation_stale"
    assert row["entry_dimensions"]["regime"] == "trend"
    assert row["runtime"]["trade_plan_evaluation"]["economics"][
        "expected_value_usd"
    ] == 12.5


def test_build_decision_row_expose_mandate_ref_a_la_racine() -> None:
    decision = _decision()
    mandate_ref = {
        "mandate_id": "universe-mandate:eu-1",
        "venue": "EU",
        "status": "active",
        "as_of": "2026-07-11T08:00:00+00:00",
    }
    decision["mandate_ref"] = mandate_ref

    row = decision_ledger.build_decision_row(_report([decision]), decision, sequence=0)

    assert row["mandate_ref"] == mandate_ref
    assert row["schema_version"] == 1


def test_build_decision_row_mandate_ref_none_quand_absent() -> None:
    decision = _decision()
    row = decision_ledger.build_decision_row(_report([decision]), decision, sequence=0)
    assert row["mandate_ref"] is None


def test_ancienne_ligne_sans_mandate_ref_reste_lisible() -> None:
    old_row = {
        "schema_version": 1,
        "decision_id": "2026-06-08T12:15:21+00:00|0|SPY",
        "symbol": "SPY",
        "action": "HOLD",
        "decision": {"symbol": "SPY", "mandate_ref": {"mandate_id": "legacy"}},
    }
    assert "mandate_ref" not in old_row
    assert decision_row_mandate_ref(old_row) == {"mandate_id": "legacy"}
    assert old_row["schema_version"] == 1
    assert old_row["decision"]["mandate_ref"]["mandate_id"] == "legacy"


def test_build_decision_row_preserves_global_learning_citations() -> None:
    decision = _decision()
    decision["applied_learning_ids"] = ["rule-breakout", "rule-fees", 42]

    row = decision_ledger.build_decision_row(_report([decision]), decision, sequence=0)

    assert row["applied_learning_ids"] == ["rule-breakout", "rule-fees"]


def test_build_decision_row_exposes_pilot_correlation_only_when_supplied() -> None:
    decision = _decision()
    decision.update(
        {
            "decision_id": "decision-1",
            "process_instance_id": "instance-1",
            "attempt_id": "attempt-1",
            "runtime_run_id": "decision-runtime-1",
        }
    )
    report = _report([decision])
    report["runtime_run_id"] = "report-runtime-ignored"
    report["governance_version"] = {"process_version": "0.1", "bundle_sha256": "abc"}

    row = decision_ledger.build_decision_row(report, decision, sequence=0)

    assert row["process"] == {
        "process_instance_id": "instance-1",
        "attempt_id": "attempt-1",
        "runtime_run_id": "decision-runtime-1",
        "decision_id": "decision-1",
        "governance_version": {"process_version": "0.1", "bundle_sha256": "abc"},
    }
    fallback = _report([_decision()])
    fallback["runtime_run_id"] = "report-runtime-1"
    assert decision_ledger.build_decision_row(fallback, _decision(), sequence=0)["process"] == {
        "process_instance_id": None,
        "attempt_id": None,
        "runtime_run_id": "report-runtime-1",
        "decision_id": "2026-06-08T12:15:21+00:00|0|SPY",
        "governance_version": None,
    }
    assert "process" not in decision_ledger.build_decision_row(_report(), _decision(), sequence=0)


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


def test_build_decision_row_propage_l_exit_plan_brut() -> None:
    """L'exit_plan brut (tel que fourni par le LLM) est tracé dans runtime pour
    diagnostiquer les rejets invalid_exit_plan sans fouiller decision_audit.json."""
    decision = _decision()
    exit_plan = {
        "hard_stop": {
            "type": "structural",
            "anchor": "swing_low",
            "window": 24,
            "min_pct": 0.008,
            "max_pct": 0.025,
        }
    }
    decision["exit_plan"] = exit_plan
    report = _report([decision])

    row = decision_ledger.build_decision_row(report, decision, sequence=0, source="daemon")

    assert row["runtime"]["exit_plan"] == exit_plan


def test_build_decision_row_exit_plan_absent_est_none() -> None:
    decision = _decision()
    report = _report([decision])

    row = decision_ledger.build_decision_row(report, decision, sequence=0, source="daemon")

    assert row["runtime"]["exit_plan"] is None


def test_build_decision_row_propage_le_reason_code_structure() -> None:
    decision = _decision()
    decision["decision_reason_code"] = "WAITING_PULLBACK"
    report = _report([decision])

    row = decision_ledger.build_decision_row(report, decision, sequence=0, source="daemon")

    assert row["decision_reason_code"] == "WAITING_PULLBACK"
    assert row["decision"]["decision_reason_code"] == "WAITING_PULLBACK"


def test_build_decision_row_propage_opportunity_side() -> None:
    decision = _decision()
    decision["opportunity_side"] = "short"
    report = _report([decision])

    row = decision_ledger.build_decision_row(report, decision, sequence=0, source="daemon")

    assert row["opportunity_side"] == "short"
    assert row["decision"]["opportunity_side"] == "short"


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


def test_build_decision_row_propage_context_et_warnings_exit_plan_runtime() -> None:
    decision = _decision(action="BUY")
    trace = {
        "hard_stop": {
            "spec_type": "percent",
            "warnings": [{"code": "hard_stop_above_max_pct", "field": "max_pct"}],
        }
    }
    warnings = trace["hard_stop"]["warnings"]
    decision.update(
        {
            "intent": "OPEN_LONG",
            "qty": 10.0,
            "executed": True,
            "reason": "ok",
            "context": "hard_stop_pct=0.10 max_pct=0.04",
            "exit_plan_trace": trace,
            "exit_plan_warnings": warnings,
        }
    )
    report = _report([decision])

    row = decision_ledger.build_decision_row(report, decision, sequence=0, source="daemon")

    assert row["context"] == "hard_stop_pct=0.10 max_pct=0.04"
    assert row["runtime"]["exit_plan_trace"] == trace
    assert row["runtime"]["exit_plan_warnings"] == warnings


def test_build_decision_row_propage_les_nouveaux_champs_audit_runtime() -> None:
    decision = _decision(action="BUY")
    decision.update(
        {
            "intent": "OPEN_LONG",
            "qty": 100.0,
            "next_wake_event": "session_open",
            "next_wake_event_iso": "2026-06-09T13:30:00+00:00",
            "risk_pct_target": 0.005,
            "risk_qty_derived": True,
            "exit_update": {"hard_stop": 97.0},
            "exit_update_applied": True,
            "exit_update_trace": {"hard_stop": {"resolved_price": 97.0}},
            "exit_update_warnings": [{"code": "hard_stop_below_min_pct"}],
        }
    )
    report = _report([decision])

    row = decision_ledger.build_decision_row(report, decision, sequence=0, source="daemon")

    assert row["runtime"]["next_wake_event"] == "session_open"
    assert row["runtime"]["next_wake_event_iso"] == "2026-06-09T13:30:00+00:00"
    assert row["runtime"]["risk_pct_target"] == 0.005
    assert row["runtime"]["risk_qty_derived"] is True
    assert row["runtime"]["exit_update"] is True
    assert row["runtime"]["exit_update_applied"] is True
    assert row["runtime"]["exit_update_reason"] is None
    assert row["runtime"]["exit_update_trace"] == {"hard_stop": {"resolved_price": 97.0}}
    assert row["runtime"]["exit_update_warnings"] == [{"code": "hard_stop_below_min_pct"}]


def test_build_decision_row_propage_data_source_runtime() -> None:
    # Régression : entry["data_source"] (source composite ayant servi les barres)
    # était perdu par la whitelist runtime — décisions persistées sans traçabilité.
    decision = _decision(action="HOLD")
    decision["data_source"] = "yfinance"
    report = _report([decision])

    row = decision_ledger.build_decision_row(report, decision, sequence=0, source="daemon")

    assert row["runtime"]["data_source"] == "yfinance"


def test_decision_ledger_append_est_idempotent(tmp_path) -> None:
    store = decision_ledger.DecisionLedgerStore(tmp_path / "decisions.jsonl")
    row = decision_ledger.build_decision_row(_report([_decision()]), _decision(), sequence=0)

    assert store.append(row) is True
    assert store.append(row) is False

    rows = store.read_all()
    assert len(rows) == 1
    assert rows[0]["decision_id"] == row["decision_id"]


def test_decision_ledger_relit_une_decision_exacte_depuis_le_disque(tmp_path) -> None:
    path = tmp_path / "decisions.jsonl"
    writer = decision_ledger.DecisionLedgerStore(path)
    first = decision_ledger.build_decision_row(
        _report([_decision("SPY")]),
        _decision("SPY"),
        sequence=0,
    )
    second = decision_ledger.build_decision_row(
        _report([_decision("QQQ")]),
        _decision("QQQ"),
        sequence=1,
    )
    writer.append(first)
    writer.append(second)

    reader = decision_ledger.DecisionLedgerStore(path)

    assert reader.read_by_decision_id(first["decision_id"]) == first
    assert reader.read_by_decision_id("missing") is None


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


def _simple_report():
    return {"ts": "2026-06-23T12:00:00+00:00", "prices": {"ACA.PA": 12.3}}


def test_build_decision_row_includes_news_when_present():
    news = {
        "earnings_in_h": 24.0,
        "news_coverage": "ok",
        "news_count": 2,
        "source": "yahoo",
        "asof": "2026-06-23T12:00:00+00:00",
    }
    decision = {"symbol": "ACA.PA", "action": "HOLD", "news": news}
    row = decision_ledger.build_decision_row(_simple_report(), decision, sequence=0)
    assert row["news"] == news


def test_build_decision_row_news_defaults_to_empty_dict():
    decision = {"symbol": "ACA.PA", "action": "HOLD"}
    row = decision_ledger.build_decision_row(_simple_report(), decision, sequence=0)
    assert row["news"] == {}


# ---------------------------------------------------------------------------
# Cache d'IDs en mémoire (P0 data-lifecycle §3.1)
# ---------------------------------------------------------------------------


def test_cache_ids_deuxieme_append_ne_relit_pas_le_fichier(monkeypatch, tmp_path) -> None:
    """Après le 1er append (qui charge le cache), _read_rows ne doit plus être
    appelée lors des appends suivants."""
    store = decision_ledger.DecisionLedgerStore(tmp_path / "decisions.jsonl")
    row1 = decision_ledger.build_decision_row(_report([_decision("SPY")]), _decision("SPY"), sequence=0)
    row2 = decision_ledger.build_decision_row(_report([_decision("QQQ")]), _decision("QQQ"), sequence=1)

    read_calls = 0
    original_read_rows = store._read_rows

    def counting_read_rows():
        nonlocal read_calls
        read_calls += 1
        return original_read_rows()

    monkeypatch.setattr(store, "_read_rows", counting_read_rows)

    store.append(row1)  # charge le cache (1 lecture)
    store.append(row2)  # doit utiliser le cache — pas de nouvelle lecture

    assert read_calls == 1, f"_read_rows appelée {read_calls} fois (attendu 1)"


def test_cache_ids_dedup_reste_effective_apres_cache(tmp_path) -> None:
    """Le cache d'IDs n'ouvre pas de faille : un decision_id déjà présent
    est toujours refusé, même après plusieurs appends."""
    store = decision_ledger.DecisionLedgerStore(tmp_path / "decisions.jsonl")
    row = decision_ledger.build_decision_row(_report([_decision("SPY")]), _decision("SPY"), sequence=0)

    assert store.append(row) is True
    assert store.append(row) is False  # doublon via cache chaud

    # Nouvelle instance (cache froid) : doit relire et refuser aussi.
    store2 = decision_ledger.DecisionLedgerStore(tmp_path / "decisions.jsonl")
    assert store2.append(row) is False


def test_replace_all_invalide_et_reconstruit_le_cache(tmp_path) -> None:
    """replace_all() doit reconstruire le cache pour que les appends suivants
    voient les nouvelles IDs sans relecture disque."""
    store = decision_ledger.DecisionLedgerStore(tmp_path / "decisions.jsonl")
    row_a = decision_ledger.build_decision_row(_report([_decision("SPY")]), _decision("SPY"), sequence=0)
    row_b = decision_ledger.build_decision_row(_report([_decision("QQQ")]), _decision("QQQ"), sequence=1)
    store.append(row_a)  # cache chargé avec row_a

    # replace_all remplace le contenu par row_b uniquement
    store.replace_all([row_b])

    # row_a ne doit plus bloquer (sorti du fichier et du cache)
    assert store.append(row_a) is True
    # row_b est déjà présent → refusé
    assert store.append(row_b) is False

    # Invariant DISQUE (review P0) : un store frais relit le fichier réel —
    # replace_all puis append doivent avoir laissé exactement [row_b, row_a].
    store2 = decision_ledger.DecisionLedgerStore(tmp_path / "decisions.jsonl")
    ids = [r.get("decision_id") for r in store2.read_all()]
    assert ids == [row_b.get("decision_id"), row_a.get("decision_id")]
    assert store2.append(row_a) is False  # dédup effective depuis le disque


# ---------------------------------------------------------------------------
# replace_all atomique (P0 data-lifecycle §3.3)
# ---------------------------------------------------------------------------


def test_replace_all_atomique_pas_de_fichier_tmp_residuel(tmp_path) -> None:
    """replace_all() doit écrire via un .tmp puis le renommer :
    le contenu final est correct et aucun .tmp ne reste sur disque."""
    store = decision_ledger.DecisionLedgerStore(tmp_path / "decisions.jsonl")
    rows = [
        decision_ledger.build_decision_row(_report([_decision("SPY")]), _decision("SPY"), sequence=0),
        decision_ledger.build_decision_row(_report([_decision("QQQ")]), _decision("QQQ"), sequence=1),
    ]

    store.replace_all(rows)

    # Aucun fichier temporaire résiduel
    assert not store.path.with_suffix(".tmp").exists()
    # Contenu attendu : les 2 rows
    persisted = store.read_all()
    assert [r["symbol"] for r in persisted] == ["SPY", "QQQ"]
