"""Helpers de test partagés."""

import logging

import pytest

from trader import daemon


@pytest.fixture(autouse=True)
def _restore_casys_trader_logger():
    """Restaure l'état du logger 'casys-trader' après chaque test.

    Nécessaire car certains tests appellent daemon.main() qui déclenche
    setup_logging() (propagate=False, handlers remplacés). Sans cette
    fixture, le caplog des tests suivants serait silencieux.
    """
    logger = logging.getLogger("casys-trader")
    original_handlers = list(logger.handlers)
    original_propagate = logger.propagate
    original_level = logger.level
    yield
    logger.handlers.clear()
    for h in original_handlers:
        logger.addHandler(h)
    logger.propagate = original_propagate
    logger.level = original_level


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


def write_runtime_config(root, *, symbols=("SPY",)) -> None:
    """Harnais runtime minimal (config + mandate) partagé par les tests daemon.

    Évite les copies par fichier de test : tout champ ajouté à risk.yaml ou
    mandate/ se fait ICI une seule fois.
    """
    (root / "config").mkdir(exist_ok=True)
    (root / "mandate").mkdir(exist_ok=True)
    symbols_yaml = "".join(f"  - {s}\n" for s in symbols)
    (root / "config" / "universe.yaml").write_text(
        f"starting_cash: 100000\nsymbols:\n{symbols_yaml}"
    )
    (root / "config" / "risk.yaml").write_text(
        "max_position_value: 20000\nmax_gross_exposure: 100000\n"
        "max_order_value: 10000\nmax_orders_per_cycle: 5\nmin_equity: 50000\n"
    )
    (root / "mandate" / "mandate.md").write_text("# Mandat\n")
    (root / "mandate" / "memory.md").write_text("# Memoire\n")
