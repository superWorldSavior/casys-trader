"""T3 — câblage runtime : DataSourceHandle + throttle par-source dans build_data_source.

Spec queue tool-round §4/§5.3 : le handle expose la ref COURANTE du data_source aux
workers de file (jamais de capture), et build_data_source enveloppe les sources
nommées dans throttle_by_source (mécanisme agnostique, limite par fournisseur).
"""
from datetime import datetime, timezone

from trader.market.data_source import ThrottledDataSource, make_indirect_get_bars
from trader.runtime.data_source_runtime import (
    DataSourceHandle,
    DataSourceRuntimeConfig,
    build_data_source,
)


class _FakeYF:
    def get_bars(self, symbol: str, lookback: str = "5d", interval: str = "1h") -> list:
        return [("bar", symbol)]


def _paper_config() -> DataSourceRuntimeConfig:
    return DataSourceRuntimeConfig(
        use_composite=True,
        routes=[{"symbols": ["*"], "sources": ["yfinance"]}],
        profile="paper",
    )


def _failing_connect(*args, **kwargs):
    from trader.market import market_data as market

    raise market.MarketError("ib_connect_failed", "test: pas d'IB")


def test_handle_expose_la_ref_courante() -> None:
    handle = DataSourceHandle()
    assert handle.get() is None

    sentinel = object()
    handle.set(sentinel)
    assert handle.get() is sentinel

    handle.set(None)  # le daemon annule la ref sur MarketError
    assert handle.get() is None


def test_handle_compose_avec_make_indirect_get_bars() -> None:
    # Chaîne complète worker : handle.get -> indirection -> data_source courant.
    handle = DataSourceHandle()
    handle.set(_FakeYF())
    get_bars = make_indirect_get_bars(handle.get)

    assert get_bars("SPY") == [("bar", "SPY")]


def test_build_data_source_enveloppe_les_sources_throttlees() -> None:
    state = build_data_source(
        _paper_config(),
        host="127.0.0.1",
        port=0,
        client_id=0,
        attach_retry_seconds=60.0,
        now=datetime(2026, 7, 5, tzinfo=timezone.utc),
        connect_ib_fn=_failing_connect,
        yfinance_cls=_FakeYF,
        throttle_by_source={"yfinance": 3},
    )

    wrapped = state.composite_available["yfinance"]
    assert isinstance(wrapped, ThrottledDataSource)
    # Le composite route bien à travers la source enveloppée.
    assert state.data_source is not None


def test_build_data_source_sans_throttle_reste_nu() -> None:
    state = build_data_source(
        _paper_config(),
        host="127.0.0.1",
        port=0,
        client_id=0,
        attach_retry_seconds=60.0,
        now=datetime(2026, 7, 5, tzinfo=timezone.utc),
        connect_ib_fn=_failing_connect,
        yfinance_cls=_FakeYF,
    )

    assert isinstance(state.composite_available["yfinance"], _FakeYF)
