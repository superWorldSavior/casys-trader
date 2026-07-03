"""Tests des agrégats purs de la home cockpit (aucune I/O)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# decision_status / parse_ts / select_decision_rows
# ---------------------------------------------------------------------------

def test_decision_status_priorites():
    from trader.cockpit.aggregates import decision_status

    assert decision_status({"executed": True, "action": "BUY"}) == "exec"
    assert decision_status({"reason": "risk:max_exposure"}) == "risk"
    assert decision_status({"reason": "stale_market_data"}) == "stale"
    assert decision_status({"decision_source": "armed_plan"}) == "armé"
    assert decision_status({"runtime": {"trade_plan_created": True}}) == "plan"
    assert decision_status({"runtime": {"indicator_watch_created": True}}) == "veille"
    assert decision_status({"model_called": False, "reason": "quiet_gate"}) == "quiet"
    assert decision_status({"action": "HOLD"}) == "hold"
    assert decision_status({"action": "BUY"}) == "signal"


def test_decision_status_identique_a_overview():
    """Anti-régression : overview délègue au même code."""
    from trader.cockpit import aggregates, overview

    row = {"reason": "risk:x", "action": "SELL"}
    assert overview._decision_status(row) == aggregates.decision_status(row)


def test_parse_ts_z_et_naif_et_invalide():
    from trader.cockpit.aggregates import parse_ts

    aware = parse_ts("2026-07-03T11:59:00Z")
    assert aware is not None and aware.tzinfo is not None
    naive = parse_ts("2026-07-03T11:59:00")
    assert naive is not None and naive.tzinfo is not None
    assert parse_ts("") is None
    assert parse_ts("pas-une-date") is None
    assert parse_ts(None) is None


def test_select_decision_rows_priorise_exec():
    from trader.cockpit.aggregates import select_decision_rows

    rows = [
        {"symbol": "AAA", "action": "HOLD", "ts": "2026-07-03T11:00:00Z"},
        {"symbol": "BBB", "action": "BUY", "executed": True, "ts": "2026-07-03T10:00:00Z"},
    ]
    selected = select_decision_rows([], rows, limit=1)
    assert selected[0]["symbol"] == "BBB"
