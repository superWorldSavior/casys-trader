import json
from pathlib import Path

from trader.attribution import compute_round_trips, compute_attribution


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
