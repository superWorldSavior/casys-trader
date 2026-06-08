"""Helpers de test partagés."""

import pytest

from trader import daemon


class FakeDataSource:
    """Fake drop-in pour le daemon : même signature que market.get_bars."""

    def __init__(self, get_bars):
        self._get_bars = get_bars
        self.calls = []

    def get_bars(self, symbol: str, lookback: str, interval: str):
        self.calls.append((symbol, lookback, interval))
        return self._get_bars(symbol, lookback, interval)


@pytest.fixture
def make_data_source():
    return FakeDataSource


@pytest.fixture
def patch_batch(monkeypatch):
    """Installe un fake `codex_client.decide_batch` à partir d'un fake per-symbole
    `decide(**kwargs)` (comme avant le passage au batch). Pour chaque symbole, on
    reconstruit le contexte (shared + per_symbol) et on appelle le fake, en
    préservant la séquence d'appels (utile pour le round-trip REQUEST_CONTEXT)."""

    def install(decide_fn):
        def decide_batch(*, symbols, mandate, memory, shared_context, per_symbol, allow_context_request=False, **_):
            out = {}
            for sym in symbols:
                ctx = {**shared_context, "symbol": sym, **(per_symbol.get(sym) or {})}
                out[sym] = decide_fn(
                    symbol=sym,
                    mandate=mandate,
                    memory=memory,
                    context=ctx,
                    allow_context_request=allow_context_request,
                )
            return out

        monkeypatch.setattr(daemon.codex_client, "decide_batch", decide_batch)

    return install
