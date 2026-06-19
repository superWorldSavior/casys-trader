import json

from trader import decision_audit, meta_performance


def _row(symbol: str, action: str, reason_code: str) -> dict:
    return {
        "decision_id": f"2026-06-08T10:00:00+00:00|0|{symbol}",
        "cycle_ts": "2026-06-08T10:00:00+00:00",
        "symbol": symbol,
        "action": action,
        "price": 100.0,
        "decision_reason_code": reason_code,
    }


def test_compute_meta_performance_resume_les_hold_missed_par_reason(tmp_path) -> None:
    rows = [
        _row("PULLBACK", "HOLD", "WAITING_PULLBACK"),
        _row("NOEDGE", "HOLD", "NO_EDGE"),
        _row("ENTRY", "BUY", "ENTRY_SIGNAL"),
    ]
    prices = {
        "PULLBACK": [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 102.0},
        ],
        "NOEDGE": [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 100.1},
        ],
        "ENTRY": [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 99.0},
        ],
    }
    audit = decision_audit.audit_rows(rows, prices, horizons=["1h"], threshold_pct=0.5)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "decision_audit.json").write_text(json.dumps(audit), encoding="utf-8")

    payload = meta_performance.compute_meta_performance(state_dir, horizons=("1h",))

    assert payload["available"] is True
    assert payload["horizons"]["1h"]["global"]["missed_known_pct"] == 33.33
    hold_rows = {row["reason_code"]: row for row in payload["horizons"]["1h"]["hold_quality_by_reason"]}
    assert hold_rows["WAITING_PULLBACK"]["missed_known_pct"] == 100.0
    assert hold_rows["NO_EDGE"]["good_known_pct"] == 100.0
    trade_rows = payload["horizons"]["1h"]["trade_quality_by_reason"]
    assert trade_rows == [
        {
            "action": "BUY",
            "reason_code": "ENTRY_SIGNAL",
            "known": 1,
            "good": 0,
            "bad": 1,
            "neutral": 0,
            "good_known_pct": 0.0,
            "bad_known_pct": 100.0,
            "neutral_known_pct": 0.0,
        }
    ]


def test_compute_meta_performance_absent_si_pas_daudit(tmp_path) -> None:
    assert meta_performance.compute_meta_performance(tmp_path)["available"] is False


def test_compute_meta_performance_recalcule_les_anciens_audits_sans_reason_metrics(tmp_path) -> None:
    audit = {
        "threshold_pct": 0.5,
        "horizons": ["1h"],
        "rows": [
            {
                "decision_id": "2026-06-08T10:00:00+00:00|0|SPY",
                "cycle_ts": "2026-06-08T10:00:00+00:00",
                "symbol": "SPY",
                "action": "HOLD",
                "rationale": "attendre pullback propre",
                "audits": {
                    "1h": {
                        "entry_price": 100.0,
                        "future_price": 102.0,
                        "future_return_pct": 2.0,
                        "verdict": "missed",
                    }
                },
            }
        ],
    }
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "decision_audit.json").write_text(json.dumps(audit), encoding="utf-8")

    payload = meta_performance.compute_meta_performance(state_dir, horizons=("1h",))

    assert payload["horizons"]["1h"]["hold_quality_by_reason"] == [
        {
            "reason_code": "WAITING_PULLBACK",
            "known": 1,
            "good": 0,
            "missed": 1,
            "good_known_pct": 0.0,
            "missed_known_pct": 100.0,
        }
    ]
