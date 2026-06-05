from trader.tools.market import Bar

from backtest.data import HistoryStore


def _bar(ts: str, close: float) -> Bar:
    return Bar(ts=ts, open=close - 1.0, high=close + 1.0, low=close - 2.0, close=close, volume=100.0)


def test_history_store_expose_la_timeline_triee_union_des_symboles() -> None:
    store = HistoryStore.from_bars(
        {
            "MSFT": [_bar("2026-01-01T10:00:00", 200.0), _bar("2026-01-01T12:00:00", 202.0)],
            "AAPL": [_bar("2026-01-01T11:00:00", 101.0), _bar("2026-01-01T12:00:00", 102.0)],
        }
    )

    assert store.symbols() == ["AAPL", "MSFT"]
    assert store.timeline() == [
        "2026-01-01T10:00:00",
        "2026-01-01T11:00:00",
        "2026-01-01T12:00:00",
    ]


def test_price_asof_ne_voit_jamais_les_barres_futures() -> None:
    store = HistoryStore.from_bars(
        {
            "AAPL": [
                _bar("2026-01-01T10:00:00", 100.0),
                _bar("2026-01-01T11:00:00", 110.0),
                _bar("2026-01-01T12:00:00", 120.0),
            ]
        }
    )

    assert store.price_asof("AAPL", "2026-01-01T09:59:59") is None
    assert store.price_asof("AAPL", "2026-01-01T10:00:00") == 100.0
    assert store.price_asof("AAPL", "2026-01-01T10:30:00") == 100.0
    assert store.price_asof("AAPL", "2026-01-01T11:00:00") == 110.0
    assert store.price_asof("AAPL", "2026-01-01T11:59:59") == 110.0
    assert store.price_asof("AAPL", "2026-01-01T13:00:00") == 120.0


def test_bars_asof_retourne_un_lookback_strictement_historique_en_ordre_croissant() -> None:
    bars = [
        _bar("2026-01-01T10:00:00", 100.0),
        _bar("2026-01-01T11:00:00", 110.0),
        _bar("2026-01-01T12:00:00", 120.0),
    ]
    store = HistoryStore.from_bars({"AAPL": bars})

    assert store.bars_asof("AAPL", "2026-01-01T09:59:59", lookback=3) == []
    assert store.bars_asof("AAPL", "2026-01-01T10:00:00", lookback=3) == [bars[0]]
    assert store.bars_asof("AAPL", "2026-01-01T10:30:00", lookback=3) == [bars[0]]
    assert store.bars_asof("AAPL", "2026-01-01T12:00:00", lookback=2) == [bars[1], bars[2]]
    assert store.bars_asof("AAPL", "2026-01-01T12:30:00", lookback=10) == bars


def test_symbole_inconnu_retourne_des_absences_explicites() -> None:
    store = HistoryStore.from_bars({"AAPL": [_bar("2026-01-01T10:00:00", 100.0)]})

    assert store.price_asof("MSFT", "2026-01-01T10:00:00") is None
    assert store.bars_asof("MSFT", "2026-01-01T10:00:00", lookback=5) == []
