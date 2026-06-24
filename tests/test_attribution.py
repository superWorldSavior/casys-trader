import json
from pathlib import Path

from trader import attribution
from trader.attribution import compute_round_trips, compute_attribution, compute_hard_stop_diagnostics


def _write_perf(state_dir: Path, rows: list[dict]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "model_performance.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def test_round_trip_long_simple(tmp_path) -> None:
    _write_perf(
        tmp_path,
        [
            {"ts": "2026-06-05T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 10, "price": 100.0, "confidence": 0.8, "intent": "OPEN_LONG"},
            {"ts": "2026-06-05T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 10, "price": 110.0, "confidence": 0.6, "intent": "CLOSE"},
        ],
    )

    trips = compute_round_trips(tmp_path)
    assert len(trips) == 1
    trip = trips[0]
    assert trip["symbol"] == "SPY"
    assert trip["side"] == "LONG"
    assert trip["quantity"] == 10
    assert trip["entry_price"] == 100.0
    assert trip["exit_price"] == 110.0
    assert trip["gross_pnl"] == 100.0
    assert trip["commission"] == 0.0
    assert trip["pnl"] == 100.0
    assert trip["entry_confidence"] == 0.8
    assert trip["holding_minutes"] == 60.0


def test_round_trip_short_simple(tmp_path) -> None:
    _write_perf(
        tmp_path,
        [
            {"ts": "2026-06-05T10:00:00+00:00", "symbol": "QQQ", "action": "SELL",
             "quantity": 5, "price": 200.0, "confidence": 0.7, "intent": "OPEN_SHORT"},
            {"ts": "2026-06-05T10:30:00+00:00", "symbol": "QQQ", "action": "BUY",
             "quantity": 5, "price": 190.0, "confidence": 0.5, "intent": "CLOSE"},
        ],
    )

    trips = compute_round_trips(tmp_path)
    assert len(trips) == 1
    assert trips[0]["side"] == "SHORT"
    assert trips[0]["gross_pnl"] == 50.0
    assert trips[0]["commission"] == 0.0
    assert trips[0]["pnl"] == 50.0
    assert trips[0]["holding_minutes"] == 30.0


def test_round_trip_soustrait_les_commissions_entree_et_sortie(tmp_path) -> None:
    _write_perf(
        tmp_path,
        [
            {"ts": "2026-06-05T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.8, "intent": "OPEN_LONG",
             "commission": 0.35},
            {"ts": "2026-06-05T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 1, "price": 110.0, "confidence": 0.6, "intent": "CLOSE",
             "commission": 0.35},
        ],
    )

    trips = compute_round_trips(tmp_path)

    assert len(trips) == 1
    assert trips[0]["gross_pnl"] == 10.0
    assert trips[0]["commission"] == 0.70
    assert trips[0]["pnl"] == 9.30
    assert compute_attribution(tmp_path)["realized_pnl"] == 9.30


def test_reduction_partielle_prorate_la_commission_d_entree(tmp_path) -> None:
    _write_perf(
        tmp_path,
        [
            {"ts": "2026-06-05T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 10, "price": 100.0, "confidence": 0.8, "intent": "OPEN_LONG",
             "commission": 1.0},
            {"ts": "2026-06-05T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 4, "price": 105.0, "confidence": 0.6, "intent": "REDUCE",
             "commission": 0.4},
            {"ts": "2026-06-05T12:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 6, "price": 110.0, "confidence": 0.6, "intent": "CLOSE",
             "commission": 0.6},
        ],
    )

    trips = compute_round_trips(tmp_path)

    assert [trip["gross_pnl"] for trip in trips] == [20.0, 60.0]
    assert [round(trip["commission"], 6) for trip in trips] == [0.8, 1.2]
    assert [round(trip["pnl"], 6) for trip in trips] == [19.2, 58.8]


def test_reduction_partielle_emet_un_round_trip_et_garde_la_position(tmp_path) -> None:
    _write_perf(
        tmp_path,
        [
            {"ts": "2026-06-05T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 10, "price": 100.0, "confidence": 0.8, "intent": "OPEN_LONG"},
            {"ts": "2026-06-05T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 4, "price": 105.0, "confidence": 0.6, "intent": "REDUCE"},
        ],
    )

    trips = compute_round_trips(tmp_path)
    assert len(trips) == 1
    assert trips[0]["quantity"] == 4
    assert trips[0]["pnl"] == 20.0


def test_round_trip_propage_la_raison_de_sortie(tmp_path) -> None:
    _write_perf(
        tmp_path,
        [
            {"ts": "2026-06-05T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 10, "price": 100.0, "confidence": 0.8, "intent": "OPEN_LONG"},
            {"ts": "2026-06-05T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 10, "price": 95.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
        ],
    )

    trips = compute_round_trips(tmp_path)
    assert trips[0]["exit_reason"] == "hard_stop"
    assert trips[0]["pnl"] == -50.0


def test_compute_attribution_calibration_par_confidence_et_raison(tmp_path) -> None:
    _write_perf(
        tmp_path,
        [
            # trade gagnant haute confiance
            {"ts": "2026-06-05T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 10, "price": 100.0, "confidence": 0.9, "intent": "OPEN_LONG"},
            {"ts": "2026-06-05T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 10, "price": 110.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "take_profit"},
            # trade perdant haute confiance
            {"ts": "2026-06-05T12:00:00+00:00", "symbol": "QQQ", "action": "BUY",
             "quantity": 10, "price": 200.0, "confidence": 0.9, "intent": "OPEN_LONG"},
            {"ts": "2026-06-05T13:00:00+00:00", "symbol": "QQQ", "action": "SELL",
             "quantity": 10, "price": 190.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
        ],
    )

    attr = compute_attribution(tmp_path)
    assert attr["n_closed_trades"] == 2
    assert attr["realized_pnl"] == 0.0
    assert attr["win_rate"] == 0.5

    high = next(b for b in attr["by_confidence"] if b["bucket"] == "0.85-1.0")
    assert high["n"] == 2
    assert high["win_rate"] == 0.5

    reasons = {r["reason"]: r for r in attr["by_exit_reason"]}
    assert reasons["hard_stop"]["total_pnl"] == -100.0
    assert reasons["take_profit"]["total_pnl"] == 100.0


def test_compute_attribution_filtre_les_trips_clotures_avant_since(tmp_path) -> None:
    _write_perf(
        tmp_path,
        [
            # Exclu : sortie avant la frontière incluse.
            {"ts": "2026-06-09T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.9, "intent": "OPEN_LONG"},
            {"ts": "2026-06-09T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 1, "price": 90.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
            # Gardé : entrée avant la frontière, mais sortie le jour de la frontière.
            {"ts": "2026-06-09T23:30:00+00:00", "symbol": "QQQ", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.9, "intent": "OPEN_LONG"},
            {"ts": "2026-06-10T00:30:00+00:00", "symbol": "QQQ", "action": "SELL",
             "quantity": 1, "price": 110.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "take_profit"},
            # Gardé : sortie après la frontière.
            {"ts": "2026-06-11T10:00:00+00:00", "symbol": "IWM", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.6, "intent": "OPEN_LONG"},
            {"ts": "2026-06-11T11:00:00+00:00", "symbol": "IWM", "action": "SELL",
             "quantity": 1, "price": 105.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "take_profit"},
        ],
    )

    attr = compute_attribution(tmp_path, since="2026-06-10")

    assert attr["n_closed_trades"] == 2
    assert attr["realized_pnl"] == 15.0
    assert attr["win_rate"] == 1.0
    assert attr["regime"] == {
        "since": "2026-06-10",
        "excluded_symbols": [],
        "n_excluded_trades": 1,
        "min_entry_confidence": None,
        "n_excluded_low_confidence": 0,
    }


def test_compute_attribution_exclut_les_symboles_et_recalcule_les_raisons(tmp_path) -> None:
    _write_perf(
        tmp_path,
        [
            {"ts": "2026-06-10T10:00:00+00:00", "symbol": "CL=F", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.9, "intent": "OPEN_LONG"},
            {"ts": "2026-06-10T11:00:00+00:00", "symbol": "CL=F", "action": "SELL",
             "quantity": 1, "price": 50.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
            {"ts": "2026-06-10T12:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.7, "intent": "OPEN_LONG"},
            {"ts": "2026-06-10T13:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 1, "price": 112.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "take_profit"},
        ],
    )

    attr = compute_attribution(tmp_path, exclude_symbols=("CL=F",))

    assert attr["n_closed_trades"] == 1
    assert attr["realized_pnl"] == 12.0
    reasons = {r["reason"]: r for r in attr["by_exit_reason"]}
    assert set(reasons) == {"take_profit"}
    assert reasons["take_profit"]["total_pnl"] == 12.0
    assert attr["regime"] == {
        "since": None,
        "excluded_symbols": ["CL=F"],
        "n_excluded_trades": 1,
        "min_entry_confidence": None,
        "n_excluded_low_confidence": 0,
    }


def test_compute_attribution_sans_args_garde_tous_les_trips(tmp_path) -> None:
    _write_perf(
        tmp_path,
        [
            {"ts": "2026-06-09T10:00:00+00:00", "symbol": "CL=F", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.9, "intent": "OPEN_LONG"},
            {"ts": "2026-06-09T11:00:00+00:00", "symbol": "CL=F", "action": "SELL",
             "quantity": 1, "price": 50.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
            {"ts": "2026-06-10T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.7, "intent": "OPEN_LONG"},
            {"ts": "2026-06-10T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 1, "price": 112.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "take_profit"},
        ],
    )

    attr = compute_attribution(tmp_path)

    assert attr["n_closed_trades"] == 2
    assert attr["realized_pnl"] == -38.0
    assert attr["regime"] == {
        "since": None,
        "excluded_symbols": [],
        "n_excluded_trades": 0,
        "min_entry_confidence": None,
        "n_excluded_low_confidence": 0,
    }


def test_compute_attribution_expose_recent_trips_tries_par_sortie_desc(tmp_path) -> None:
    _write_perf(
        tmp_path,
        [
            {"ts": "2026-06-05T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.7, "intent": "OPEN_LONG"},
            {"ts": "2026-06-05T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 1, "price": 105.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "take_profit:tp1"},
            {"ts": "2026-06-05T12:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 1, "price": 110.0, "confidence": 0.7, "intent": "OPEN_LONG"},
            {"ts": "2026-06-05T13:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 1, "price": 105.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
            {"ts": "2026-06-05T14:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.8, "intent": "OPEN_LONG"},
            {"ts": "2026-06-05T15:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 1, "price": 115.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "trailing_stop"},
        ],
    )

    attr = compute_attribution(tmp_path)
    recent = attr.get("recent_trips")

    assert isinstance(recent, list)
    assert [trip["exit_ts"] for trip in recent] == [
        "2026-06-05T15:00:00+00:00",
        "2026-06-05T13:00:00+00:00",
        "2026-06-05T11:00:00+00:00",
    ]
    assert [trip["exit_reason"] for trip in recent] == [
        "trailing_stop",
        "hard_stop",
        "take_profit:tp1",
    ]
    assert [trip["pnl"] for trip in recent] == [15.0, -5.0, 5.0]


def test_compute_attribution_expose_brut_et_frais(tmp_path) -> None:
    """L'agent doit voir le brut ET les frais pour distinguer un scalp neutre
    qui devient perdant une fois les commissions déduites."""
    _write_perf(
        tmp_path,
        [
            {"ts": "2026-06-05T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 10, "price": 100.0, "confidence": 0.8, "intent": "OPEN_LONG",
             "commission": 0.35},
            {"ts": "2026-06-05T10:05:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 10, "price": 100.5, "intent": "CLOSE", "exit_reason": "scalp",
             "commission": 0.35},
        ],
    )

    attr = compute_attribution(tmp_path)
    # gross = (100.5-100)*10 = 5.0 ; frais = 0.70 ; net = 4.30
    assert attr["realized_gross_pnl"] == 5.0
    assert attr["total_commissions"] == 0.70
    assert attr["realized_pnl"] == 4.30

    scalp = next(r for r in attr["by_exit_reason"] if r["reason"] == "scalp")
    assert scalp["total_gross_pnl"] == 5.0
    assert scalp["total_commission"] == 0.70


def test_select_hard_stop_symbols_applique_les_filtres_regime(tmp_path) -> None:
    _write_perf(
        tmp_path,
        [
            {"ts": "2026-06-09T10:00:00+00:00", "symbol": "OLD", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.8, "intent": "OPEN_LONG"},
            {"ts": "2026-06-09T11:00:00+00:00", "symbol": "OLD", "action": "SELL",
             "quantity": 1, "price": 95.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
            {"ts": "2026-06-10T10:00:00+00:00", "symbol": "CL=F", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.8, "intent": "OPEN_LONG"},
            {"ts": "2026-06-10T11:00:00+00:00", "symbol": "CL=F", "action": "SELL",
             "quantity": 1, "price": 95.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
            {"ts": "2026-06-10T12:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.8, "intent": "OPEN_LONG"},
            {"ts": "2026-06-10T13:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 1, "price": 95.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
            {"ts": "2026-06-10T14:00:00+00:00", "symbol": "QQQ", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.8, "intent": "OPEN_LONG"},
            {"ts": "2026-06-10T15:00:00+00:00", "symbol": "QQQ", "action": "SELL",
             "quantity": 1, "price": 105.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "take_profit"},
        ],
    )

    symbols = attribution.select_hard_stop_symbols(
        tmp_path,
        since="2026-06-10",
        exclude_symbols=("CL=F",),
    )

    assert symbols == ["SPY"]


def test_round_trip_pnl_converted_to_usd(tmp_path) -> None:
    """Un round-trip TWD est rapporté en USD = (pnl natif) converti via fx_rate."""
    import pytest

    _write_perf(
        tmp_path,
        [
            {"ts": "2026-06-23T05:00:00+00:00", "symbol": "2379.TW", "action": "BUY",
             "quantity": 10.0, "price": 870.0, "commission": 80.0,
             "commission_currency": "TWD", "fx_rate": 0.031},
            {"ts": "2026-06-24T01:00:00+00:00", "symbol": "2379.TW", "action": "SELL",
             "quantity": 10.0, "price": 829.0, "commission": 80.0,
             "commission_currency": "TWD", "fx_rate": 0.031},
        ],
    )

    trips = compute_round_trips(tmp_path)
    assert len(trips) == 1
    # gross_pnl natif = (829-870)*10 = -410 TWD → en USD via exit fx_rate
    # commissions natives = 80 entry + 80 exit = 160 TWD → en USD via chaque leg fx_rate
    # pnl natif total = -410 - 160 = -570 TWD ; en USD = -570 * 0.031
    assert trips[0]["pnl"] == pytest.approx(-570.0 * 0.031, rel=1e-6)


def test_round_trip_legacy_fill_sans_fx_rate_inchange(tmp_path) -> None:
    """Un fill sans fx_rate doit garder le comportement legacy (pnl en native = USD)."""
    import pytest

    _write_perf(
        tmp_path,
        [
            {"ts": "2026-06-05T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 10, "price": 100.0, "commission": 0.35},
            {"ts": "2026-06-05T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 10, "price": 110.0, "commission": 0.35},
        ],
    )

    trips = compute_round_trips(tmp_path)
    assert len(trips) == 1
    # gross = (110-100)*10 = 100.0 ; commissions = 0.70 ; pnl = 99.30
    assert trips[0]["gross_pnl"] == pytest.approx(100.0)
    assert trips[0]["pnl"] == pytest.approx(99.30)


def test_hard_stop_diagnostics_marque_stop_trop_tot_si_reprise_apres_stop(tmp_path) -> None:
    _write_perf(
        tmp_path,
        [
            {"ts": "2026-06-05T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 10, "price": 100.0, "confidence": 0.8, "intent": "OPEN_LONG"},
            {"ts": "2026-06-05T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 10, "price": 95.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
        ],
    )

    diagnostic = compute_hard_stop_diagnostics(
        tmp_path,
        {
            "SPY": [
                {"ts": "2026-06-05T11:00:00+00:00", "open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
                {"ts": "2026-06-05T12:00:00+00:00", "open": 95.0, "high": 99.0, "low": 94.0, "close": 96.0},
                {"ts": "2026-06-05T13:00:00+00:00", "open": 96.0, "high": 108.0, "low": 95.0, "close": 106.0},
            ]
        },
        lookahead_bars=2,
    )

    assert diagnostic["summary"]["hard_stops"] == 1
    assert diagnostic["summary"]["diagnosed"] == 1
    assert diagnostic["summary"]["stop_too_early"] == 1
    assert diagnostic["summary"]["actual_pnl"] == -50.0
    assert diagnostic["summary"]["hold_to_lookahead_pnl"] == 60.0
    assert diagnostic["summary"]["hold_to_lookahead_delta_vs_actual"] == 110.0
    case = diagnostic["cases"][0]
    assert case["symbol"] == "SPY"
    assert case["verdict"] == "stop_too_early"
    assert case["recovered_to_entry"] is True
    assert case["would_have_won_by_lookahead"] is True
    assert case["best_after_stop_pnl"] == 80.0
    assert case["worst_after_stop_pnl"] == -60.0


def test_hard_stop_diagnostics_marque_stop_utile_si_le_marche_continue_contre_la_position(tmp_path) -> None:
    _write_perf(
        tmp_path,
        [
            {"ts": "2026-06-05T10:00:00+00:00", "symbol": "QQQ", "action": "BUY",
             "quantity": 10, "price": 100.0, "confidence": 0.8, "intent": "OPEN_LONG"},
            {"ts": "2026-06-05T11:00:00+00:00", "symbol": "QQQ", "action": "SELL",
             "quantity": 10, "price": 95.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
        ],
    )

    diagnostic = compute_hard_stop_diagnostics(
        tmp_path,
        {
            "QQQ": [
                {"ts": "2026-06-05T12:00:00+00:00", "open": 95.0, "high": 96.0, "low": 92.0, "close": 93.0},
                {"ts": "2026-06-05T13:00:00+00:00", "open": 93.0, "high": 94.0, "low": 88.0, "close": 90.0},
            ]
        },
        lookahead_bars=2,
    )

    assert diagnostic["summary"]["hard_stops"] == 1
    assert diagnostic["summary"]["stop_helped_or_neutral"] == 1
    case = diagnostic["cases"][0]
    assert case["verdict"] == "stop_helped_or_neutral"
    assert case["recovered_to_entry"] is False
    assert case["would_have_beaten_stop_by_lookahead"] is False
    assert case["hold_to_lookahead_pnl"] == -100.0
    assert case["hold_to_lookahead_delta_vs_actual"] == -50.0


def test_hard_stop_diagnostics_supporte_les_shorts(tmp_path) -> None:
    _write_perf(
        tmp_path,
        [
            {"ts": "2026-06-05T10:00:00+00:00", "symbol": "IWM", "action": "SELL",
             "quantity": 5, "price": 100.0, "confidence": 0.8, "intent": "OPEN_SHORT"},
            {"ts": "2026-06-05T11:00:00+00:00", "symbol": "IWM", "action": "BUY",
             "quantity": 5, "price": 105.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
        ],
    )

    diagnostic = compute_hard_stop_diagnostics(
        tmp_path,
        {
            "IWM": [
                {"ts": "2026-06-05T12:00:00+00:00", "open": 105.0, "high": 106.0, "low": 98.0, "close": 99.0},
                {"ts": "2026-06-05T13:00:00+00:00", "open": 99.0, "high": 100.0, "low": 90.0, "close": 92.0},
            ]
        },
        lookahead_bars=2,
    )

    case = diagnostic["cases"][0]
    assert case["side"] == "SHORT"
    assert case["actual_pnl"] == -25.0
    assert case["hold_to_lookahead_pnl"] == 40.0
    assert case["best_after_stop_pnl"] == 50.0
    assert case["worst_after_stop_pnl"] == -30.0
    assert case["recovered_to_entry"] is True
    assert case["verdict"] == "stop_too_early"


def test_hard_stop_diagnostics_marque_inconnu_sans_barres_futures(tmp_path) -> None:
    _write_perf(
        tmp_path,
        [
            {"ts": "2026-06-05T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.8, "intent": "OPEN_LONG"},
            {"ts": "2026-06-05T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 1, "price": 95.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
        ],
    )

    diagnostic = compute_hard_stop_diagnostics(
        tmp_path,
        {"SPY": [{"ts": "2026-06-05T10:30:00+00:00", "high": 101.0, "low": 99.0, "close": 100.0}]},
    )

    assert diagnostic["summary"]["hard_stops"] == 1
    assert diagnostic["summary"]["unknown"] == 1
    assert diagnostic["cases"][0]["verdict"] == "unknown_no_future_bars"
    assert diagnostic["cases"][0]["future_bars"] == 0


def test_compute_attribution_brut_frais_neutres_sans_fichier(tmp_path) -> None:
    attr = compute_attribution(tmp_path)
    assert attr["realized_gross_pnl"] == 0.0
    assert attr["total_commissions"] == 0.0


def test_compute_attribution_sans_fichier_renvoie_neutre(tmp_path) -> None:
    attr = compute_attribution(tmp_path)
    assert attr["n_closed_trades"] == 0
    assert attr["realized_pnl"] == 0.0
    assert attr["win_rate"] is None


def test_ignore_lignes_corrompues_non_dict_et_champs_manquants(tmp_path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "model_performance.jsonl").write_text(
        "\n".join(
            [
                "[1, 2, 3]",                      # JSON valide mais non-objet
                '"juste une string"',            # idem
                '{"ts": "2026-06-05T10:00:00+00:00", "symbol": "SPY", "action": "BUY", "quantity": 10}',  # price manquant
                '{"ts": "2026-06-05T10:05:00+00:00", "symbol": "SPY", "action": "BUY", "quantity": 10, "price": 100.0, "confidence": 0.8, "intent": "OPEN_LONG"}',
                '{"ts": "2026-06-05T11:00:00+00:00", "symbol": "SPY", "action": "SELL", "quantity": 10, "price": 110.0, "intent": "CLOSE"}',
            ]
        ),
        encoding="utf-8",
    )

    trips = compute_round_trips(tmp_path)
    assert len(trips) == 1
    assert trips[0]["pnl"] == 100.0
    # ne doit pas crasher malgré les lignes non-dict
    assert compute_attribution(tmp_path)["n_closed_trades"] == 1


def test_confidence_dentree_corrigee_apres_reduction_partielle(tmp_path) -> None:
    _write_perf(
        tmp_path,
        [
            {"ts": "2026-06-05T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 10, "price": 100.0, "confidence": 0.8, "intent": "OPEN_LONG"},
            {"ts": "2026-06-05T10:30:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 4, "price": 100.0, "confidence": None, "intent": "REDUCE"},
            {"ts": "2026-06-05T11:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 6, "price": 100.0, "confidence": 0.4, "intent": "OPEN_LONG"},
            {"ts": "2026-06-05T12:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 12, "price": 100.0, "confidence": None, "intent": "CLOSE"},
        ],
    )

    trips = compute_round_trips(tmp_path)
    # Reliquat 6 @0.8 + ajout 6 @0.4 -> confidence pondérée = 0.6 (pas 0.65).
    final = trips[-1]
    assert final["quantity"] == 12
    assert round(final["entry_confidence"], 6) == 0.6


def test_position_se_clot_proprement_malgre_les_flottants(tmp_path) -> None:
    _write_perf(
        tmp_path,
        [
            {"ts": "2026-06-05T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 0.1, "price": 100.0, "confidence": 0.7, "intent": "OPEN_LONG"},
            {"ts": "2026-06-05T10:01:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 0.1, "price": 100.0, "confidence": 0.7, "intent": "OPEN_LONG"},
            {"ts": "2026-06-05T10:02:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 0.1, "price": 100.0, "confidence": 0.7, "intent": "OPEN_LONG"},
            {"ts": "2026-06-05T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 0.3, "price": 110.0, "confidence": None, "intent": "CLOSE"},
            # Nouvelle entrée propre : ne doit PAS hériter d'une poussière flottante.
            {"ts": "2026-06-05T12:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.5, "intent": "OPEN_LONG"},
            {"ts": "2026-06-05T13:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 1, "price": 110.0, "confidence": None, "intent": "CLOSE"},
        ],
    )

    trips = compute_round_trips(tmp_path)
    assert len(trips) == 2
    assert trips[1]["entry_price"] == 100.0
    assert trips[1]["quantity"] == 1
    assert trips[1]["entry_confidence"] == 0.5


# ---------------------------------------------------------------------------
# Filtre min_entry_confidence — anomalies historiques pré-gate ou auto-exec
# ---------------------------------------------------------------------------

def test_filtre_low_confidence_exclut_trip_sous_seuil(tmp_path) -> None:
    """Un trip avec entry_confidence < seuil est exclu quand min_entry_confidence est fourni."""
    _write_perf(
        tmp_path,
        [
            # Confiance basse (0.6 < 0.7) : doit être exclu
            {"ts": "2026-06-10T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.6, "intent": "OPEN_LONG"},
            {"ts": "2026-06-10T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 1, "price": 90.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
            # Confiance suffisante (0.8 >= 0.7) : doit être conservé
            {"ts": "2026-06-10T12:00:00+00:00", "symbol": "QQQ", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.8, "intent": "OPEN_LONG"},
            {"ts": "2026-06-10T13:00:00+00:00", "symbol": "QQQ", "action": "SELL",
             "quantity": 1, "price": 110.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "take_profit"},
        ],
    )

    attr = compute_attribution(tmp_path, min_entry_confidence=0.7)

    assert attr["n_closed_trades"] == 1
    assert attr["realized_pnl"] == 10.0
    assert attr["regime"]["n_excluded_low_confidence"] == 1
    assert attr["regime"]["min_entry_confidence"] == 0.7


def test_filtre_low_confidence_conserve_trip_exactement_au_seuil(tmp_path) -> None:
    """entry_confidence == seuil : conservé (exclusion strictement inférieure)."""
    _write_perf(
        tmp_path,
        [
            {"ts": "2026-06-10T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.7, "intent": "OPEN_LONG"},
            {"ts": "2026-06-10T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 1, "price": 110.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "take_profit"},
        ],
    )

    attr = compute_attribution(tmp_path, min_entry_confidence=0.7)

    assert attr["n_closed_trades"] == 1
    assert attr["regime"]["n_excluded_low_confidence"] == 0


def test_filtre_low_confidence_none_desactive_le_filtre(tmp_path) -> None:
    """min_entry_confidence=None (défaut) ne filtre rien — rétrocompat."""
    _write_perf(
        tmp_path,
        [
            {"ts": "2026-06-10T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.5, "intent": "OPEN_LONG"},
            {"ts": "2026-06-10T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 1, "price": 90.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
        ],
    )

    attr = compute_attribution(tmp_path)  # min_entry_confidence absent → None

    assert attr["n_closed_trades"] == 1
    assert attr["regime"]["min_entry_confidence"] is None
    assert attr["regime"]["n_excluded_low_confidence"] == 0


def test_filtre_low_confidence_conserve_trip_avec_confidence_none(tmp_path) -> None:
    """Un trip dont entry_confidence est None est toujours conservé même si le filtre est actif.
    On n'exclut pas ce qu'on ne peut pas juger."""
    _write_perf(
        tmp_path,
        [
            # Pas de confidence sur l'entrée (champ absent) : conservé même avec filtre actif
            {"ts": "2026-06-10T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 1, "price": 100.0, "intent": "OPEN_LONG"},
            {"ts": "2026-06-10T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 1, "price": 110.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "take_profit"},
        ],
    )

    attr = compute_attribution(tmp_path, min_entry_confidence=0.7)

    assert attr["n_closed_trades"] == 1
    assert attr["regime"]["n_excluded_low_confidence"] == 0


def test_filtre_low_confidence_compte_separement_de_since_et_symbols(tmp_path) -> None:
    """n_excluded_low_confidence ne compte QUE les exclusions par confiance (pas since/symbols)."""
    _write_perf(
        tmp_path,
        [
            # Exclu par since (pas par confiance)
            {"ts": "2026-06-09T10:00:00+00:00", "symbol": "IWM", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.5, "intent": "OPEN_LONG"},
            {"ts": "2026-06-09T11:00:00+00:00", "symbol": "IWM", "action": "SELL",
             "quantity": 1, "price": 90.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
            # Exclu par symbole (pas par confiance)
            {"ts": "2026-06-10T10:00:00+00:00", "symbol": "CL=F", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.5, "intent": "OPEN_LONG"},
            {"ts": "2026-06-10T11:00:00+00:00", "symbol": "CL=F", "action": "SELL",
             "quantity": 1, "price": 90.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
            # Exclu par confiance basse (et dans la fenêtre since, pas exclu par symbole)
            {"ts": "2026-06-10T12:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.6, "intent": "OPEN_LONG"},
            {"ts": "2026-06-10T13:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 1, "price": 90.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
            # Conservé
            {"ts": "2026-06-10T14:00:00+00:00", "symbol": "QQQ", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.8, "intent": "OPEN_LONG"},
            {"ts": "2026-06-10T15:00:00+00:00", "symbol": "QQQ", "action": "SELL",
             "quantity": 1, "price": 110.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "take_profit"},
        ],
    )

    attr = compute_attribution(
        tmp_path,
        since="2026-06-10",
        exclude_symbols=("CL=F",),
        min_entry_confidence=0.7,
    )

    assert attr["n_closed_trades"] == 1
    assert attr["regime"]["n_excluded_trades"] == 2  # IWM (since) + CL=F (symbole)
    assert attr["regime"]["n_excluded_low_confidence"] == 1  # SPY (confiance)


def test_filtre_low_confidence_sur_select_hard_stop_symbols(tmp_path) -> None:
    """select_hard_stop_symbols honore min_entry_confidence : un hard_stop à confiance basse est exclu."""
    _write_perf(
        tmp_path,
        [
            # Hard stop à confiance basse : ne doit PAS apparaître dans la liste
            {"ts": "2026-06-10T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.6, "intent": "OPEN_LONG"},
            {"ts": "2026-06-10T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 1, "price": 95.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
            # Hard stop à confiance suffisante : doit apparaître
            {"ts": "2026-06-10T12:00:00+00:00", "symbol": "QQQ", "action": "BUY",
             "quantity": 1, "price": 100.0, "confidence": 0.8, "intent": "OPEN_LONG"},
            {"ts": "2026-06-10T13:00:00+00:00", "symbol": "QQQ", "action": "SELL",
             "quantity": 1, "price": 95.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
        ],
    )

    symbols = attribution.select_hard_stop_symbols(tmp_path, min_entry_confidence=0.7)

    assert symbols == ["QQQ"]


def test_filtre_low_confidence_sur_compute_hard_stop_diagnostics(tmp_path) -> None:
    """compute_hard_stop_diagnostics honore min_entry_confidence."""
    _write_perf(
        tmp_path,
        [
            # Hard stop confiance basse : exclu du diagnostic
            {"ts": "2026-06-05T10:00:00+00:00", "symbol": "SPY", "action": "BUY",
             "quantity": 10, "price": 100.0, "confidence": 0.6, "intent": "OPEN_LONG"},
            {"ts": "2026-06-05T11:00:00+00:00", "symbol": "SPY", "action": "SELL",
             "quantity": 10, "price": 95.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
            # Hard stop confiance suffisante : inclus
            {"ts": "2026-06-05T12:00:00+00:00", "symbol": "QQQ", "action": "BUY",
             "quantity": 10, "price": 100.0, "confidence": 0.8, "intent": "OPEN_LONG"},
            {"ts": "2026-06-05T13:00:00+00:00", "symbol": "QQQ", "action": "SELL",
             "quantity": 10, "price": 95.0, "confidence": None, "intent": "PLANNED_EXIT",
             "exit_reason": "hard_stop"},
        ],
    )

    diagnostic = compute_hard_stop_diagnostics(
        tmp_path,
        {"QQQ": [], "SPY": []},
        min_entry_confidence=0.7,
    )

    assert diagnostic["summary"]["hard_stops"] == 1
    assert diagnostic["regime"]["n_excluded_low_confidence"] == 1
