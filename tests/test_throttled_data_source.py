"""T2 (throttle) — `ThrottledDataSource` : borne la concurrence des fetchs, agnostique.

Spec §5.3 / avis Codex : décorateur par-source, sémaphore local + timeout d'acquisition
(pas de blocage indéfini), délègue disconnect. Un hang consomme 1 permit sur N.
"""
import threading
import time

import pytest

from trader.domain.market_data import MarketError
from trader.market.data_source import ThrottledDataSource


class _SlowSource:
    def __init__(self, delay: float = 0.03) -> None:
        self.delay = delay
        self.concurrent = 0
        self.max_concurrent = 0
        self.disconnected = False
        self._probe = threading.Lock()

    def get_bars(self, symbol: str, lookback: str, interval: str) -> list:
        with self._probe:
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        time.sleep(self.delay)
        with self._probe:
            self.concurrent -= 1
        return [("bar", symbol, lookback, interval)]

    def disconnect(self) -> None:
        self.disconnected = True


def test_borne_la_concurrence_a_max_concurrent() -> None:
    source = _SlowSource()
    throttled = ThrottledDataSource(source, max_concurrent=2)

    threads = [threading.Thread(target=lambda: throttled.get_bars("SPY")) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert source.max_concurrent <= 2  # jamais plus de 2 fetchs simultanés
    assert source.max_concurrent >= 1


def test_delegue_get_bars_avec_les_arguments() -> None:
    source = _SlowSource(delay=0.0)
    throttled = ThrottledDataSource(source, max_concurrent=1)

    result = throttled.get_bars("AAPL", lookback="1mo", interval="4h")

    assert result == [("bar", "AAPL", "1mo", "4h")]


def test_saturation_leve_data_source_throttled() -> None:
    source = _SlowSource(delay=0.2)
    throttled = ThrottledDataSource(source, max_concurrent=1, acquire_timeout_s=0.05)

    holder = threading.Thread(target=lambda: throttled.get_bars("SPY"))
    holder.start()
    time.sleep(0.02)  # laisse le holder prendre l'unique permit

    with pytest.raises(MarketError) as exc:
        throttled.get_bars("QQQ")  # permit indisponible → timeout d'acquisition
    assert exc.value.code == "data_source_throttled"

    holder.join()


def test_delegue_disconnect() -> None:
    source = _SlowSource()
    throttled = ThrottledDataSource(source, max_concurrent=1)

    throttled.disconnect()

    assert source.disconnected is True


def test_max_concurrent_invalide_leve_valueerror() -> None:
    with pytest.raises(ValueError):
        ThrottledDataSource(_SlowSource(), max_concurrent=0)
