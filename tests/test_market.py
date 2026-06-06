from trader.tools.market import Bar, aggregate_bars


def _bar(index: int, close: float) -> Bar:
    return Bar(
        ts=f"t{index}",
        open=close - 0.5,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=100.0 + index,
    )


def test_aggregate_bars_4h_regroupe_les_barres_1h_par_paquets_de_quatre() -> None:
    bars = [_bar(index, 100.0 + index) for index in range(8)]

    aggregated = aggregate_bars(bars, target_interval="4h")

    assert len(aggregated) == 2
    assert aggregated[0].ts == "t3"
    assert aggregated[0].open == 99.5
    assert aggregated[0].high == 104.0
    assert aggregated[0].low == 99.0
    assert aggregated[0].close == 103.0
    assert aggregated[0].volume == 406.0
    assert aggregated[1].ts == "t7"
    assert aggregated[1].close == 107.0


def test_aggregate_bars_4h_est_ancre_sur_les_bornes_horaires_si_timestamps_iso() -> None:
    bars = [
        Bar(
            ts=f"2026-06-05T{hour:02d}:00:00+00:00",
            open=100.0 + hour - 0.5,
            high=100.0 + hour + 1.0,
            low=100.0 + hour - 1.0,
            close=100.0 + hour,
            volume=100.0 + hour,
        )
        for hour in range(1, 9)
    ]

    aggregated = aggregate_bars(bars, target_interval="4h")

    assert len(aggregated) == 1
    assert aggregated[0].ts == "2026-06-05T07:00:00+00:00"
    assert aggregated[0].open == 103.5
    assert aggregated[0].high == 108.0
    assert aggregated[0].low == 103.0
    assert aggregated[0].close == 107.0
    assert aggregated[0].volume == 422.0
