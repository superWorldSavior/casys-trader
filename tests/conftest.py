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

# Knobs de routage/modèle/preset que load_dotenv() et daemon.main() peuvent
# injecter dans os.environ. Snapshot/restore process-level : une mutation
# volontaire (monkeypatch) reste visible pendant le test, mais ne fuit pas.
_LLM_ROUTING_ROLE_PREFIXES = (
    "",
    "CONSOLIDATOR_",
    "UNIVERSE_",
    "COMPANY_MICRO_",
    "NEWS_MACRO_",
)
_LLM_ROUTING_FIELDS = (
    "ACPX_AGENT",
    "MODEL",
    "ACPX_BIN",
    "ACPX_SESSION_LABEL",
    "CODEX_HOME",
    "GROK_HOME",
    "REASONING_EFFORT",
    "FALLBACK_ACPX_AGENT",
    "FALLBACK_MODEL",
)
_LLM_ROUTING_ENV_KEYS = tuple(
    f"TRADER_{prefix}{field}" for prefix in _LLM_ROUTING_ROLE_PREFIXES for field in _LLM_ROUTING_FIELDS
) + (
    "TRADER_SPARK_FALLBACK_MODEL",
    "CODEX_HOME",
    "GROK_HOME",
    "KIMI_CODE_HOME",
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


def _snapshot_environ(keys: tuple[str, ...]) -> dict[str, str | None]:
    return {key: os.environ.get(key) for key in keys}


def _restore_environ(snapshot: dict[str, str | None]) -> None:
    for key, value in snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


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
    snapshot = _snapshot_environ(_DAEMON_MAIN_AGENT_EXEC_ENV_KEYS)
    yield
    _restore_environ(snapshot)


@pytest.fixture(autouse=True)
def _restore_llm_routing_env():
    """Restaure les knobs LLM après chaque test, sans les vider au départ.

    daemon.main() et les builders analystes appellent load_dotenv() sur le .env
    du dépôt. Sans restore, TRADER_ACPX_AGENT/TRADER_MODEL/fallback/rôle fuient
    vers les builders « défaut » suivants. monkeypatch du test reste prioritaire
    pendant le test ; le teardown rétablit le baseline process.
    """
    snapshot = _snapshot_environ(_LLM_ROUTING_ENV_KEYS)
    yield
    _restore_environ(snapshot)


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


def evaluate_batch_test_decisions(kwargs: dict, decisions: dict) -> dict:
    """Make fake batch outputs obey the production entry-evaluation contract."""

    from dataclasses import replace

    from trader.application.execute.trade_plan_evaluation import (
        candidate_from_decision,
    )
    from trader.domain.decisions import Decision

    provider = kwargs.get("trade_plan_evaluator_provider")
    if provider is None:
        return decisions
    evaluated = {}
    for symbol, value in decisions.items():
        if isinstance(value, Decision) and value.trade_evaluation_id is None:
            evaluator = provider(str(symbol))
            candidate = candidate_from_decision(
                value,
                position_quantity=(None if evaluator is None else getattr(evaluator, "position_quantity", None)),
            )
            if candidate is not None and evaluator is not None:
                evaluation = evaluator.evaluate(candidate)
                if evaluation.valid and evaluation.evaluation_id:
                    value = replace(
                        value,
                        trade_evaluation_id=evaluation.evaluation_id,
                    )
        evaluated[symbol] = value
    return evaluated


@pytest.fixture
def patch_batch(monkeypatch):
    """Installe un fake `codex_client.decide_batch` à partir d'un fake per-symbole
    `decide(**kwargs)` (comme avant le passage au batch). Pour chaque symbole, on
    reconstruit le contexte (shared + per_symbol) et on appelle le fake, en
    préservant la séquence d'appels (utile pour le round-trip REQUEST_CONTEXT)."""

    from trader.application.decide import planner_batch

    original_enforce = planner_batch._enforce_trade_evaluations

    def enforce_evaluated_test_decisions(
        responses,
        *,
        evaluator_provider,
    ):
        if isinstance(responses, dict):
            responses = evaluate_batch_test_decisions(
                {"trade_plan_evaluator_provider": evaluator_provider},
                responses,
            )
        return original_enforce(
            responses,
            evaluator_provider=evaluator_provider,
        )

    monkeypatch.setattr(
        planner_batch,
        "_enforce_trade_evaluations",
        enforce_evaluated_test_decisions,
    )

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


def write_runtime_config(
    root,
    *,
    symbols=("SPY",),
    starting_cash: float | int = 100_000,
    max_position_value: float | int = 20_000,
    max_gross_exposure: float | int = 100_000,
    max_order_value: float | int = 10_000,
    min_equity: float | int = 50_000,
    max_risk_per_trade_pct: float | None = None,
    extra_risk: dict[str, object] | None = None,
    write_mandate: bool = True,
    write_memory: bool = True,
    write_fx: bool = False,
    mandate_text: str = "# Mandat\n",
    memory_text: str = "# Memoire\n",
) -> None:
    """Harnais runtime minimal (config + mandate) partagé par les tests daemon.

    Factory paramétrée : univers, seuils de risque, clés de risque additionnelles,
    présence mandate/memory, et fx.yaml optionnel. Tout champ ajouté à risk.yaml
    ou mandate/ se fait ICI une seule fois.
    """
    (root / "config").mkdir(exist_ok=True)
    (root / "mandate").mkdir(exist_ok=True)
    symbols_yaml = "".join(f"  - {s}\n" for s in symbols)
    (root / "config" / "universe.yaml").write_text(f"starting_cash: {starting_cash}\nsymbols:\n{symbols_yaml}")
    risk_lines = [
        f"max_position_value: {max_position_value}",
        f"max_gross_exposure: {max_gross_exposure}",
        f"max_order_value: {max_order_value}",
    ]
    if max_risk_per_trade_pct is not None:
        risk_lines.append(f"max_risk_per_trade_pct: {max_risk_per_trade_pct}")
    if extra_risk:
        for key, value in extra_risk.items():
            risk_lines.append(f"{key}: {value}")
    risk_lines.append(f"min_equity: {min_equity}")
    (root / "config" / "risk.yaml").write_text("\n".join(risk_lines) + "\n")
    if write_fx:
        (root / "config" / "fx.yaml").write_text(
            "TWD:\n  yahoo: TWD=X\n  invert: true\n  fallback: 0.031\n"
            "EUR:\n  yahoo: EURUSD=X\n  invert: false\n  fallback: 1.08\n"
        )
    if write_mandate:
        (root / "mandate" / "mandate.md").write_text(mandate_text)
    if write_memory:
        (root / "mandate" / "memory.md").write_text(memory_text)


@pytest.fixture(name="write_runtime_config")
def _write_runtime_config_fixture():
    """Expose la factory en injection pytest (évite `from conftest import ...`)."""
    return write_runtime_config
