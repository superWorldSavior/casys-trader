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


# ---------------------------------------------------------------------------
# activity_buckets
# ---------------------------------------------------------------------------

def test_activity_buckets_fenetre_vide():
    from trader.cockpit.aggregates import ACTIVITY_STATES, activity_buckets

    buckets = activity_buckets([], NOW)
    assert set(buckets) == set(ACTIVITY_STATES)
    assert all(len(v) == 12 and sum(v) == 0 for v in buckets.values())


def test_activity_buckets_placement_et_exclusions():
    from trader.cockpit.aggregates import activity_buckets

    rows = [
        # âge 0 → dernier bucket (bord inclus)
        {"executed": True, "action": "BUY", "ts": NOW.isoformat()},
        # âge 30 min → bucket 12 - 1 - 6 = 5
        {"reason": "risk:x", "ts": (NOW - timedelta(minutes=30)).isoformat()},
        # âge 60 min pile → exclu
        {"action": "HOLD", "ts": (NOW - timedelta(minutes=60)).isoformat()},
        # ts illisible → ignoré
        {"action": "HOLD", "ts": "n/a"},
        # futur → ignoré
        {"action": "HOLD", "ts": (NOW + timedelta(minutes=1)).isoformat()},
    ]
    buckets = activity_buckets(rows, NOW)
    assert buckets["exec"][11] == 1
    assert buckets["risk"][5] == 1
    assert sum(buckets["hold"]) == 0


def test_activity_buckets_mapping_series():
    """armé→plan, quiet/signal→hold (spec §4.3)."""
    from trader.cockpit.aggregates import activity_buckets

    ts = NOW.isoformat()
    rows = [
        {"decision_source": "armed_plan", "ts": ts},
        {"model_called": False, "reason": "quiet_gate", "ts": ts},
        {"action": "BUY", "ts": ts},
    ]
    buckets = activity_buckets(rows, NOW)
    assert buckets["plan"][11] == 1
    assert buckets["hold"][11] == 2


# ---------------------------------------------------------------------------
# risk_at_stops
# ---------------------------------------------------------------------------

def test_risk_at_stops_long_short_fx():
    from trader.cockpit.aggregates import risk_at_stops

    holdings = [
        {"symbol": "AAA", "quantity": 10, "last_price": 100.0, "fx_rate": 1.0},
        {"symbol": "BBB", "quantity": -5, "last_price": 50.0, "fx_rate": 2.0},
    ]
    plans = [
        {"symbol": "AAA", "side": "LONG", "hard_stop_price": 95.0, "remaining_quantity": 10},
        {"symbol": "BBB", "side": "SHORT", "hard_stop_price": 55.0, "remaining_quantity": 5},
    ]
    result = risk_at_stops(holdings, plans)
    # AAA : (95-100)*10*1*1 = -50 ; BBB : (55-50)*5*(-1)*2 = -50
    assert result.total_usd == -100.0
    assert result.worst_usd == -50.0
    assert result.without_stop == ()


def test_risk_at_stops_sans_stop_et_vide():
    from trader.cockpit.aggregates import risk_at_stops

    holdings = [{"symbol": "CCC", "quantity": 3, "last_price": 10.0}]
    result = risk_at_stops(holdings, [])  # aucun plan
    assert result.without_stop == ("CCC",)
    assert result.total_usd == 0.0

    empty = risk_at_stops([], [])
    assert empty.total_usd == 0.0 and empty.worst_symbol is None


def test_risk_at_stops_plan_solde_contribue_zero():
    """remaining_quantity=0 (plan soldé) → risque nul, pas de fallback sur quantity."""
    from trader.cockpit.aggregates import risk_at_stops

    holdings = [{"symbol": "AAA", "quantity": 10, "last_price": 100.0, "fx_rate": 1.0}]
    plans = [{"symbol": "AAA", "side": "LONG", "hard_stop_price": 95.0,
              "remaining_quantity": 0, "quantity": 10}]
    result = risk_at_stops(holdings, plans)
    assert result.total_usd == 0.0
    assert result.without_stop == ()


def test_risk_at_stops_worst_symbol_determine():
    from trader.cockpit.aggregates import risk_at_stops

    holdings = [
        {"symbol": "AAA", "quantity": 10, "last_price": 100.0},
        {"symbol": "BBB", "quantity": 10, "last_price": 100.0},
    ]
    plans = [
        {"symbol": "AAA", "side": "LONG", "hard_stop_price": 95.0, "remaining_quantity": 10},
        {"symbol": "BBB", "side": "LONG", "hard_stop_price": 92.0, "remaining_quantity": 10},
    ]
    result = risk_at_stops(holdings, plans)
    assert result.worst_symbol == "BBB"
    assert result.worst_usd == -80.0
    assert result.total_usd == -130.0


# ---------------------------------------------------------------------------
# attention_items
# ---------------------------------------------------------------------------

def test_attention_items_ordre_et_kill_premier():
    from trader.cockpit.aggregates import attention_items

    state = {
        "halted": "drawdown",
        "portfolio": {"holdings": [{"symbol": "AAA", "quantity": 10, "last_price": 100.0}]},
        "trade_plans": [{"symbol": "AAA", "side": "LONG", "hard_stop_price": 90.0,
                         "remaining_quantity": 10}],
        "stale_streaks": {"BBB": 3},
        "recent_decisions": [{"reason": "risk:max_exposure"}],
        "armed_plans": [{"expires_at": (NOW + timedelta(minutes=30)).isoformat()}],
    }
    items = attention_items(state, kill_active=True, now=NOW)
    labels = [item.label for item in items]
    assert items[0].label == "KILL actif" and items[0].severity == "crit"
    assert labels[1].startswith("HALT")
    assert labels[2].startswith("risque@stops")
    assert labels[3].startswith("stale 1")
    assert labels[4].endswith("rejets risk")
    assert "expirent <1h" in labels[5]
    assert len(items) == 6


def test_attention_items_nominal_vide():
    from trader.cockpit.aggregates import attention_items

    assert attention_items({}, kill_active=False, now=NOW) == []


def test_attention_items_arme_deja_expire_ignore():
    from trader.cockpit.aggregates import attention_items

    state = {"armed_plans": [{"expires_at": (NOW - timedelta(minutes=5)).isoformat()}]}
    assert attention_items(state, kill_active=False, now=NOW) == []


# ---------------------------------------------------------------------------
# venue_clock
# ---------------------------------------------------------------------------

def test_venue_clock_ouverte_et_prochaine_transition():
    from trader.cockpit.aggregates import venue_clock

    sessions = {"TW": {"open": "01:00", "close": "05:30"},
                "EU": {"open": "07:00", "close": "15:30"}}
    # Vendredi 2026-07-03 12:00 UTC : EU ouverte, prochaine transition = close EU 15:30
    clock = venue_clock(sessions, NOW)
    assert clock.open_now == ("EU",)
    assert (clock.next_venue, clock.next_kind) == ("EU", "close")
    assert clock.next_at is not None and clock.next_at.hour == 15


def test_venue_clock_weekend_prochaine_ouverture_lundi():
    from trader.cockpit.aggregates import venue_clock

    saturday = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    clock = venue_clock({"EU": {"open": "07:00", "close": "15:30"}}, saturday)
    assert clock.open_now == ()
    assert clock.next_kind == "open"
    assert clock.next_at is not None and clock.next_at.weekday() == 0  # lundi


def test_venue_clock_sessions_absentes_neutre():
    from trader.cockpit.aggregates import venue_clock

    clock = venue_clock({}, NOW)
    assert clock.open_now == () and clock.next_at is None


def test_read_model_expose_sessions(tmp_path):
    from trader.reporting.read_models.runtime_state import load_runtime_state

    state = load_runtime_state(state_dir=tmp_path, config_dir=str(tmp_path))
    assert isinstance(state.get("sessions"), dict)
