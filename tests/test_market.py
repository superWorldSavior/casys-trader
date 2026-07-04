import math

import pytest

from trader.market.market_data import Bar, aggregate_bars


def _bar(index: int, close: float) -> Bar:
    return Bar(
        ts=f"t{index}",
        open=close - 0.5,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=100.0 + index,
    )


def _patch_yf(monkeypatch, df) -> None:
    import yfinance

    class _FakeTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def history(self, **kwargs):
            return df

    monkeypatch.setattr(yfinance, "Ticker", _FakeTicker)


def test_get_bars_jette_les_barres_close_zero_ou_nan(monkeypatch) -> None:
    """yfinance crache par moments des barres à close=0/NaN (titres EU peu
    liquides, intraday). Une telle barre n'est PAS un prix : get_bars doit la
    jeter, sinon elle empoisonne la valorisation (falaise d'équité STMN.SW,
    30/06 : prix 0 → position valorisée $0 → équité -8 k le temps d'un cycle)."""
    import pandas as pd

    from trader.market import market_data as market

    idx = pd.to_datetime(
        ["2026-06-30 09:00", "2026-06-30 09:15", "2026-06-30 09:30", "2026-06-30 09:45"],
        utc=True,
    )
    df = pd.DataFrame(
        {
            "Open": [100.0, 0.0, 101.0, 102.0],
            "High": [101.0, 0.0, 102.0, 103.0],
            "Low": [99.0, 0.0, 100.0, 101.0],
            "Close": [100.5, 0.0, math.nan, 102.5],  # barres 2 et 3 = pourries
            "Volume": [10.0, 0.0, 12.0, 13.0],
        },
        index=idx,
    )
    _patch_yf(monkeypatch, df)

    bars = market.get_bars("STMN.SW", lookback="1d", interval="15m")

    closes = [b.close for b in bars]
    assert closes == [100.5, 102.5]  # 0 et NaN jetées
    assert all(c > 0 and math.isfinite(c) for c in closes)


def test_get_bars_toutes_barres_invalides_leve_no_data(monkeypatch) -> None:
    """Si TOUTES les barres sont à 0/NaN, c'est de la donnée morte → no_data
    (comme un df vide), pas une liste de barres fantômes."""
    import pandas as pd

    from trader.market import market_data as market

    idx = pd.to_datetime(["2026-06-30 09:00", "2026-06-30 09:15"], utc=True)
    df = pd.DataFrame(
        {
            "Open": [0.0, 0.0],
            "High": [0.0, 0.0],
            "Low": [0.0, 0.0],
            "Close": [0.0, math.nan],
            "Volume": [0.0, 0.0],
        },
        index=idx,
    )
    _patch_yf(monkeypatch, df)

    with pytest.raises(market.MarketError) as exc:
        market.get_bars("STMN.SW", lookback="1d", interval="15m")
    assert exc.value.code == "no_data"


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


def test_aggregate_bars_1h_regroupe_les_barres_15m_par_paquets_de_quatre() -> None:
    bars = [_bar(index, 100.0 + index) for index in range(8)]

    aggregated = aggregate_bars(bars, target_interval="1h")

    assert len(aggregated) == 2
    assert aggregated[0].ts == "t3"
    assert aggregated[0].open == 99.5
    assert aggregated[0].high == 104.0
    assert aggregated[0].low == 99.0
    assert aggregated[0].close == 103.0
    assert aggregated[0].volume == 406.0
    assert aggregated[1].ts == "t7"
    assert aggregated[1].close == 107.0


def test_aggregate_bars_4h_depuis_15m_attend_seize_barres_et_ignore_bucket_incomplet() -> None:
    bars = [
        Bar(
            ts=f"2026-06-05T{8 + index // 4:02d}:{(index % 4) * 15:02d}:00+00:00",
            open=100.0 + index - 0.5,
            high=100.0 + index + 1.0,
            low=100.0 + index - 1.0,
            close=100.0 + index,
            volume=100.0 + index,
        )
        for index in range(20)
    ]

    aggregated = aggregate_bars(bars, target_interval="4h")

    assert len(aggregated) == 1
    assert aggregated[0].ts == "2026-06-05T11:45:00+00:00"
    assert aggregated[0].open == 99.5
    assert aggregated[0].high == 116.0
    assert aggregated[0].low == 99.0
    assert aggregated[0].close == 115.0
    assert aggregated[0].volume == 1720.0


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
