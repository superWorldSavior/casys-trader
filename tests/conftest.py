"""Helpers de test partagés."""

import logging
import os
from types import SimpleNamespace

import pytest

from trader.runtime import daemon


_DAEMON_MAIN_AGENT_EXEC_ENV_KEYS = (
    "CASYS_AGENT_EXEC",
    "CASYS_AGENT_EXEC_CWD",
    "CODEX_HOME",
    "TRADER_REASONING_EFFORT",
)

# Les builders d'analystes appellent load_dotenv() : sans neutralisation, un test
# qui en instancie un injecte le .env de la machine dans os.environ, et un test
# ultérieur lisant un timeout hérite de la valeur locale au lieu du défaut code.
_ANALYST_TIMEOUT_ENV_KEYS = (
    "TRADER_UNIVERSE_TIMEOUT_S",
    "TRADER_GLOBAL_POSTURE_TIMEOUT_S",
    "TRADER_NEWS_MACRO_TIMEOUT_S",
    "TRADER_COMPANY_MICRO_TIMEOUT_S",
)


@pytest.fixture(autouse=True)
def _analyst_timeouts_use_code_defaults(monkeypatch):
    """Les timeouts analystes viennent du code, jamais du .env de la machine."""

    for key in _ANALYST_TIMEOUT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _news_macro_disabled_by_default(monkeypatch):
    """No test may accidentally launch real background ACPX agents."""

    monkeypatch.setenv("CASYS_NEWS_MACRO_ANALYST_ENABLED", "0")
    monkeypatch.setenv("CASYS_UNIVERSE_INTELLIGENCE_ENABLED", "0")
    monkeypatch.setenv("CASYS_COMPANY_MICRO_ANALYST_ENABLED", "0")


@pytest.fixture(autouse=True)
def _background_market_collectors_disabled_by_default(monkeypatch):
    """No daemon test may leak real network collector threads into later tests."""

    def disabled(*_args, **_kwargs):
        return {"triggered": False, "reason": "disabled_in_tests"}

    monkeypatch.setattr(daemon, "macro_series", SimpleNamespace(maybe_collect=disabled))
    monkeypatch.setattr(daemon, "gdelt", SimpleNamespace(maybe_collect=disabled))


@pytest.fixture(autouse=True)
def _restore_daemon_main_agent_exec_env():
    """Restaure les clés agent-exec que daemon.main() peut charger depuis .env.

    Couvre CASYS_AGENT_EXEC, CASYS_AGENT_EXEC_CWD et CODEX_HOME afin qu'un test
    appelant daemon.main() ne pollue pas l'environnement des tests suivants.
    """
    snapshot = {key: os.environ.get(key) for key in _DAEMON_MAIN_AGENT_EXEC_ENV_KEYS}
    yield
    for key, value in snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture(autouse=True)
def _restore_casys_trader_logger():
    """Restaure l'état des loggers 'casys-trader' ET 'trader' après chaque test.

    Nécessaire car certains tests appellent daemon.main() qui déclenche
    setup_logging() (propagate=False, handlers remplacés sur les deux loggers).
    Sans cette fixture, le caplog des tests suivants serait silencieux car les
    records de trader.application.* ne remonteraient plus jusqu'au root logger.
    """
    snapshots = {}
    for name in ("casys-trader", "trader"):
        lg = logging.getLogger(name)
        snapshots[name] = (list(lg.handlers), lg.propagate, lg.level)
    yield
    for name, (handlers, propagate, level) in snapshots.items():
        lg = logging.getLogger(name)
        lg.handlers.clear()
        for h in handlers:
            lg.addHandler(h)
        lg.propagate = propagate
        lg.level = level


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
        "max_order_value: 10000\nmin_equity: 50000\n"
    )
    (root / "mandate" / "mandate.md").write_text("# Mandat\n")
    (root / "mandate" / "memory.md").write_text("# Memoire\n")
