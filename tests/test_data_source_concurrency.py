"""T2 — thread-safety `CompositeDataSource` + indirection `make_indirect_get_bars`.

W3 : le daemon et N workers de file appellent `get_bars` en parallèle sur un
`data_source` qui mute son état interne sans verrou, et dont la référence change en
cours de run. Approche B : l'I/O réseau est HORS verrou (fetchs parallèles, un hang
n'en gèle qu'un) ; seules les mutations d'état passent par un verrou court.
"""
import threading
import time

import pytest

from trader.domain.market_data import MarketError
from trader.market.data_source import CompositeDataSource, make_indirect_get_bars


class _SlowSource:
    """Source espionne : mesure la concurrence réelle d'exécution de get_bars."""

    def __init__(self) -> None:
        self.concurrent = 0
        self.max_concurrent = 0
        self._probe = threading.Lock()

    def get_bars(self, symbol: str, lookback: str, interval: str) -> list:
        with self._probe:
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        time.sleep(0.02)  # fenêtre de course
        with self._probe:
            self.concurrent -= 1
        return []


def test_les_fetchs_ne_sont_plus_serialises() -> None:
    # Approche B : l'I/O est hors verrou → les fetchs concurrents s'exécutent en parallèle.
    source = _SlowSource()
    cds = CompositeDataSource(routes=[{"symbols": ["*"], "sources": ["fake"]}], sources={"fake": source})

    def _call() -> None:
        try:
            cds.get_bars("SPY")
        except MarketError:
            pass  # barres vides → stale/all_failed ; seule la concurrence nous intéresse

    threads = [threading.Thread(target=_call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert source.max_concurrent > 1  # I/O non sérialisé (un hang n'en gèle qu'un)


def test_mutations_concurrentes_ne_corrompent_pas_letat() -> None:
    # Le verrou court protège les mutations : N threads marquent des symboles distincts
    # en parallèle, chaque last_source doit rester cohérent (pas de write perdu/écrasé).
    cds = CompositeDataSource(routes=[{"symbols": ["*"], "sources": ["fake"]}], sources={})
    symbols = [f"S{i}" for i in range(50)]

    def _mark(sym: str) -> None:
        cds._mark_served(sym, "src")

    threads = [threading.Thread(target=_mark, args=(s,)) for s in symbols]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(cds.last_source(s) == "src" for s in symbols)


def test_indirect_get_bars_leve_si_ref_absente() -> None:
    get_bars = make_indirect_get_bars(lambda: None)
    with pytest.raises(MarketError):
        get_bars("SPY", lookback="5d", interval="1h")


def test_indirect_get_bars_suit_la_ref_courante() -> None:
    calls: list[tuple[str, str]] = []

    class _DS:
        def __init__(self, tag: str) -> None:
            self.tag = tag

        def get_bars(self, symbol: str, lookback: str, interval: str) -> list:
            calls.append((self.tag, symbol))
            return []

    current = {"ds": _DS("a")}
    get_bars = make_indirect_get_bars(lambda: current["ds"])

    get_bars("SPY", lookback="5d", interval="1h")
    current["ds"] = _DS("b")  # le daemon remplace la référence en cours de run
    get_bars("QQQ", lookback="5d", interval="1h")

    assert calls == [("a", "SPY"), ("b", "QQQ")]  # a suivi la ref courante, pas capturé l'objet
