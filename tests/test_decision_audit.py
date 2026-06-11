from trader import decision_audit


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


def test_hold_pendant_un_move_est_missed_pas_bad() -> None:
    rows = [_row("MOVER", "HOLD")]
    prices = {
        "MOVER": [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 102.0},  # +2% > seuil 0.5
        ],
    }

    result = decision_audit.audit_rows(rows, prices, horizons=["1h"], threshold_pct=0.5)

    audit = result["rows"][0]["audits"]["1h"]
    assert audit["verdict"] == "missed"

    metrics = result["metrics"]["1h"]
    assert metrics["missed"] == 1
    assert metrics["bad"] == 0
    assert metrics["known"] == 1  # un move connu compte dans la couverture
    assert metrics["missed_known_pct"] == 100.0
    assert metrics["nonbad_known_pct"] == 100.0  # une abstention n'est pas un trade raté


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
    assert audit["verdict"] == "missed"  # HOLD pendant un move = opportunité, pas échec


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
    assert verdicts["AGENT"] == "missed"  # la vraie décision agent reste jugée

    metrics = result["metrics"]["1h"]
    assert metrics["known"] == 1  # seule la décision agent compte
    assert metrics["missed"] == 1
