from trader.reporting.audit import decision_quality as decision_audit


def _row(
    symbol: str,
    action: str,
    price: float | None = 100.0,
    *,
    commit: str | None = None,
    dirty: bool = False,
) -> dict:
    return {
        "decision_id": f"2026-06-08T10:00:00+00:00|0|{symbol}",
        "cycle_ts": "2026-06-08T10:00:00+00:00",
        "symbol": symbol,
        "action": action,
        "price": price,
        "code_version": {
            "git_commit": commit,
            "git_commit_short": commit[:12] if commit else None,
            "git_branch": "main" if commit else None,
            "git_dirty": dirty if commit else None,
        },
    }


def test_audit_rows_labelle_buy_sell_hold_sur_rendement_futur() -> None:
    rows = [_row("BUYME", "BUY"), _row("SELLME", "SELL"), _row("WAIT", "HOLD")]
    prices = {
        "BUYME": [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 101.0},
        ],
        "SELLME": [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 101.0},
        ],
        "WAIT": [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 100.1},
        ],
    }

    result = decision_audit.audit_rows(rows, prices, horizons=["1h"], threshold_pct=0.5)

    verdicts = {
        item["decision_id"]: item["audits"]["1h"]["verdict"]
        for item in result["rows"]
    }
    assert verdicts["2026-06-08T10:00:00+00:00|0|BUYME"] == "good"
    assert verdicts["2026-06-08T10:00:00+00:00|0|SELLME"] == "bad"
    assert verdicts["2026-06-08T10:00:00+00:00|0|WAIT"] == "good"
    assert result["summary"]["1h"]["good"] == 2
    assert result["summary"]["1h"]["bad"] == 1


def test_hold_sans_direction_pendant_un_move_reste_non_score() -> None:
    rows = [_row("MOVER", "HOLD")]
    prices = {
        "MOVER": [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 102.0},  # +2% > seuil 0.5
        ],
    }

    result = decision_audit.audit_rows(rows, prices, horizons=["1h"], threshold_pct=0.5)

    audit = result["rows"][0]["audits"]["1h"]
    assert audit["verdict"] == "unknown"
    assert audit["benchmark_basis"] == "directionless_hold"
    assert audit["scoreability_reason"] == "direction_unknown"

    metrics = result["metrics"]["1h"]
    assert metrics["missed"] == 0
    assert metrics["bad"] == 0
    assert metrics["known"] == 0
    assert metrics["unknown"] == 1
    assert metrics["coverage_pct"] == 0.0


def test_hold_directionnel_identifie_missed_et_prudence_selon_le_sens() -> None:
    rows = [
        {**_row("LONG_UP", "HOLD"), "opportunity_side": "long"},
        {**_row("LONG_DOWN", "HOLD"), "opportunity_side": "long"},
        {**_row("SHORT_DOWN", "HOLD"), "opportunity_side": "short"},
        {**_row("SHORT_UP", "HOLD"), "opportunity_side": "short"},
    ]
    prices = {
        symbol: [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": close},
        ]
        for symbol, close in {
            "LONG_UP": 102.0,
            "LONG_DOWN": 98.0,
            "SHORT_DOWN": 98.0,
            "SHORT_UP": 102.0,
        }.items()
    }

    result = decision_audit.audit_rows(rows, prices, horizons=["1h"], threshold_pct=0.5)

    verdicts = {row["symbol"]: row["audits"]["1h"]["verdict"] for row in result["rows"]}
    assert verdicts == {
        "LONG_UP": "missed",
        "LONG_DOWN": "good",
        "SHORT_DOWN": "missed",
        "SHORT_UP": "good",
    }


def test_hold_de_position_est_score_comme_exposition_conservee() -> None:
    rows = [
        {
            **_row("LONG", "HOLD"),
            "portfolio_snapshot": {"holdings": [{"symbol": "LONG", "quantity": 10}]},
        },
        {
            **_row("SHORT", "HOLD"),
            "portfolio_snapshot": {"holdings": [{"symbol": "SHORT", "quantity": -10}]},
        },
    ]
    prices = {
        symbol: [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 102.0},
        ]
        for symbol in ("LONG", "SHORT")
    }

    result = decision_audit.audit_rows(rows, prices, horizons=["1h"], threshold_pct=0.5)

    audits = {row["symbol"]: row["audits"]["1h"] for row in result["rows"]}
    assert audits["LONG"]["verdict"] == "good"
    assert audits["LONG"]["benchmark_basis"] == "position_long"
    assert audits["SHORT"]["verdict"] == "bad"
    assert audits["SHORT"]["benchmark_basis"] == "position_short"


def test_hold_conditionnel_et_non_executable_restent_non_scores() -> None:
    rows = [
        {
            **_row("PLAN", "HOLD"),
            "runtime": {"indicator_watch_order": {"action": "SELL", "intent": "OPEN_SHORT"}},
        },
        {**_row("STALE_LLM", "HOLD"), "decision_reason_code": "DATA_STALE"},
        {**_row("CLOSED_LLM", "HOLD"), "decision_reason_code": "MARKET_CLOSED"},
    ]
    prices = {
        symbol: [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 102.0},
        ]
        for symbol in ("PLAN", "STALE_LLM", "CLOSED_LLM")
    }

    result = decision_audit.audit_rows(rows, prices, horizons=["1h"], threshold_pct=0.5)

    audits = {row["symbol"]: row["audits"]["1h"] for row in result["rows"]}
    assert audits["PLAN"]["verdict"] == "unknown"
    assert audits["PLAN"]["benchmark_basis"] == "conditional_order"
    assert audits["PLAN"]["intended_action"] == "SELL"
    assert audits["STALE_LLM"]["scoreability_reason"] == "data_stale"
    assert audits["CLOSED_LLM"]["scoreability_reason"] == "market_closed"
    assert result["metrics_by_basis"]["1h"]["conditional_order"]["coverage_pct"] == 0.0


def test_load_prices_for_audit_utilise_un_loader_injecte() -> None:
    captured: dict[str, object] = {}
    rows = [
        {"cycle_ts": "2026-06-08T10:00:00+00:00", "symbol": "SPY"},
        {"cycle_ts": "2026-06-08T11:00:00+00:00", "symbol": "QQQ"},
    ]

    def load_prices(symbols, *, start, end, interval):
        captured.update({"symbols": symbols, "start": start, "end": end, "interval": interval})
        return {"SPY": [{"ts": "2026-06-08T11:00:00+00:00", "close": 101.0}]}

    prices = decision_audit.load_prices_for_audit(
        rows,
        horizons=["1h", "1d"],
        interval="1h",
        load_prices=load_prices,
    )

    assert captured == {
        "symbols": ["QQQ", "SPY"],
        "start": "2026-06-07",
        "end": "2026-06-10",
        "interval": "1h",
    }
    assert prices == {"SPY": [{"ts": "2026-06-08T11:00:00+00:00", "close": 101.0}]}


def test_audit_rows_groupe_la_qualite_par_action_et_reason_code() -> None:
    rows = [
        {
            **_row("PULLBACK", "HOLD"),
            "decision_reason_code": "WAITING_PULLBACK",
            "intended_side": "long",
        },
        {**_row("FEES", "HOLD"), "decision_reason_code": "FEES_TOO_HIGH"},
        {**_row("TRADE", "BUY"), "decision_reason_code": "ENTRY_SIGNAL"},
    ]
    prices = {
        "PULLBACK": [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 102.0},
        ],
        "FEES": [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 100.1},
        ],
        "TRADE": [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 99.0},
        ],
    }

    result = decision_audit.audit_rows(rows, prices, horizons=["1h"], threshold_pct=0.5)

    metrics = result["metrics_by_reason"]["1h"]
    assert metrics["HOLD"]["WAITING_PULLBACK"]["missed"] == 1
    assert metrics["HOLD"]["WAITING_PULLBACK"]["missed_known_pct"] == 100.0
    assert metrics["HOLD"]["FEES_TOO_HIGH"]["good"] == 1
    assert metrics["BUY"]["ENTRY_SIGNAL"]["bad"] == 1
    assert result["rows"][0]["decision_reason_code"] == "WAITING_PULLBACK"


def test_audit_rows_infere_un_reason_code_legacy_depuis_la_rationale() -> None:
    rows = [{**_row("SPY", "HOLD"), "rationale": "apres hard_stop recent, attendre pullback propre"}]
    prices = {
        "SPY": [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 102.0},
        ]
    }

    result = decision_audit.audit_rows(rows, prices, horizons=["1h"], threshold_pct=0.5)

    row = result["rows"][0]
    assert row["decision_reason_code"] == "POST_LOSS_CAUTION"
    assert result["metrics_by_reason"]["1h"]["HOLD"]["POST_LOSS_CAUTION"]["unknown"] == 1


def test_audit_rows_reconstruit_le_prix_initial_pour_legacy() -> None:
    rows = [_row("SPY", "HOLD", price=None)]
    prices = {
        "SPY": [
            {"ts": "2026-06-08T09:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 104.0},
        ]
    }

    result = decision_audit.audit_rows(rows, prices, horizons=["1h"], threshold_pct=0.5)

    audit = result["rows"][0]["audits"]["1h"]
    assert audit["entry_price"] == 100.0
    assert audit["future_price"] == 104.0
    assert audit["future_return_pct"] == 4.0
    assert audit["verdict"] == "unknown"


def test_audit_rows_groupe_les_verdicts_par_commit_decision() -> None:
    rows = [
        _row("AAA", "BUY", commit="aaaaaaaaaaaa1111"),
        _row("BBB", "BUY", commit="bbbbbbbbbbbb2222", dirty=True),
        _row("CCC", "BUY"),
    ]
    prices = {
        "AAA": [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 101.0},
        ],
        "BBB": [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 99.0},
        ],
        "CCC": [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 101.0},
        ],
    }

    result = decision_audit.audit_rows(rows, prices, horizons=["1h"], threshold_pct=0.5)

    by_commit = result["summary_by_commit"]["1h"]
    assert by_commit["aaaaaaaaaaaa"]["good"] == 1
    assert by_commit["bbbbbbbbbbbb+dirty"]["bad"] == 1
    assert by_commit["unknown"]["good"] == 1
    metrics = result["metrics_by_commit"]["1h"]
    assert metrics["aaaaaaaaaaaa"]["good_known_pct"] == 100.0
    assert metrics["bbbbbbbbbbbb+dirty"]["bad_known_pct"] == 100.0
    assert metrics["unknown"]["known"] == 1
    assert result["rows"][1]["decision_commit_key"] == "bbbbbbbbbbbb+dirty"


def test_abstentions_machine_exclues_de_l_audit() -> None:
    """quiet_gate / stale_market_data ne sont pas des décisions agent :
    elles ne doivent compter ni en good ni en missed (review Codex D7)."""
    rows = [
        {**_row("GATED", "HOLD"), "reason": "quiet_gate"},
        {**_row("STALE", "HOLD"), "reason": "stale_market_data"},
        {**_row("AGENT", "HOLD"), "reason": "hold"},
    ]
    prices = {
        sym: [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 102.0},  # move > seuil
        ]
        for sym in ("GATED", "STALE", "AGENT")
    }

    result = decision_audit.audit_rows(rows, prices, horizons=["1h"], threshold_pct=0.5)

    verdicts = {r["symbol"]: r["audits"]["1h"]["verdict"] for r in result["rows"]}
    assert verdicts["GATED"] == "machine"
    assert verdicts["STALE"] == "machine"
    assert verdicts["AGENT"] == "unknown"  # direction absente : pas de faux missed

    metrics = result["metrics"]["1h"]
    assert metrics["known"] == 0
    assert metrics["unknown"] == 1


def test_abstentions_infra_machine_via_decision_source() -> None:
    """no_decision_in_batch / budget_exhausted sont des abstentions infra
    (decision_source='infra') : exclues des stats de qualité agent, même si
    leur `reason` n'est pas listée dans _MACHINE_REASONS. Un armed_plan
    (decision_source='armed_plan') reste une vraie décision agent à auditer."""
    rows = [
        {**_row("INFRA", "HOLD"), "reason": "no_decision_in_batch", "decision_source": "infra"},
        {**_row("ARMED", "BUY"), "reason": "armed_plan", "decision_source": "armed_plan"},
        {**_row("AGENT", "HOLD"), "reason": "hold", "decision_source": "llm"},
    ]
    prices = {
        sym: [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 102.0},  # move > seuil
        ]
        for sym in ("INFRA", "ARMED", "AGENT")
    }

    result = decision_audit.audit_rows(rows, prices, horizons=["1h"], threshold_pct=0.5)

    verdicts = {r["symbol"]: r["audits"]["1h"]["verdict"] for r in result["rows"]}
    assert verdicts["INFRA"] == "machine"  # abstention infra exclue
    assert verdicts["ARMED"] == "good"  # plan armé = vrai trade, jugé sur le move
    assert verdicts["AGENT"] == "unknown"


def test_abstentions_infra_machine_legacy_sans_decision_source() -> None:
    """Lignes ledger écrites AVANT le champ decision_source : les raisons infra
    (no_decision_in_batch / budget_exhausted) doivent rester 'machine' via le
    fallback _MACHINE_REASONS, sans avoir le champ decision_source."""
    rows = [
        {**_row("NODEC", "HOLD"), "reason": "no_decision_in_batch"},
        {**_row("BUDGET", "HOLD"), "reason": "model_call_budget_exhausted"},
        {**_row("BUDGET2", "HOLD"), "reason": "model_call_budget_exhausted_after_context"},
        {**_row("AGENT", "HOLD"), "reason": "hold"},
    ]
    prices = {
        sym: [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 102.0},  # move > seuil
        ]
        for sym in ("NODEC", "BUDGET", "BUDGET2", "AGENT")
    }

    result = decision_audit.audit_rows(rows, prices, horizons=["1h"], threshold_pct=0.5)

    verdicts = {r["symbol"]: r["audits"]["1h"]["verdict"] for r in result["rows"]}
    assert verdicts["NODEC"] == "machine"
    assert verdicts["BUDGET"] == "machine"
    assert verdicts["BUDGET2"] == "machine"
    assert verdicts["AGENT"] == "unknown"
