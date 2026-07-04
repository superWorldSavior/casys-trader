"""casys-trader — infrastructure de l'agent de trading autonome.

Le daemon (boucle runtime) appelle Codex pour décider, passe par le risk gate,
exécute en paper. La stratégie / les indicateurs / le calendrier de réveil ne
sont PAS codés ici : ils sont définis par l'agent via le mandat et la mémoire.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import sys
import types

__version__ = "0.1.0"

_COMPAT_MODULES = {
    "agent_context": "trader.agent.context",
    "code_version": "trader.metadata.code_version",
    "codex_client": "trader.agent.client",
    "consolidator": "trader.learnings.consolidator",
    "decision_audit": "trader.reporting.decision_audit",
    "decision_bench": "trader.reporting.decision_bench",
    "decision_ledger": "trader.reporting.decision_ledger",
    "decision_reason": "trader.reporting.decision_reason",
    "embeddings": "trader.learnings.embeddings",
    "exit_engine": "trader.planning.exit_engine",
    "family_regime": "trader.market.family_regime",
    "features": "trader.market.features",
    "fx": "trader.market.fx",
    "fx_rates": "trader.market.fx_rates",
    "gross_priority": "trader.market.gross_priority",
    "ib_attach": "trader.runtime.ib_attach",
    "indicator_watch": "trader.planning.indicator_watch",
    "learnings_store": "trader.learnings.store",
    "ledger_rotation": "trader.runtime.ledger_rotation",
    "llm": "trader.agent.llm",
    "logging_setup": "trader.runtime.logging_setup",
    "macro_calendar": "trader.market.macro_calendar",
    "macro_series": "trader.market.macro_series",
    "meta_performance": "trader.reporting.meta_performance",
    "palette": "trader.ui.palette",
    "pool_config": "trader.config.pool",
    "portfolio_config": "trader.config.portfolio",
    "process_env": "trader.system.process_env",
    "radar": "trader.market.radar",
    "radar_config": "trader.market.radar_config",
    "radar_data": "trader.market.radar_data",
    "regime": "trader.market.regime",
    "relevance_gate": "trader.planning.relevance_gate",
    "risk": "trader.execution.risk",
    "rotation_bench": "trader.rotation.bench",
    "rotation_collectors": "trader.rotation.collectors",
    "rotation_daemon": "trader.rotation.daemon",
    "rotation_ledger": "trader.rotation.ledger",
    "rotation_override": "trader.rotation.override",
    "rotation_schedule": "trader.rotation.schedule",
    "rotation_state": "trader.rotation.state",
    "rotation_venues": "trader.rotation.venues",
    "rotation_wiring": "trader.rotation.wiring",
    "trade_plan": "trader.planning.trade_plan",
    "tool_trace": "trader.reporting.tool_trace",
    "cockpit_events": "trader.cockpit.events",
    "cockpit_supervisor": "trader.cockpit.supervisor",
}

_TOOLS_COMPAT_MODULES = {
    "data_source": "trader.market.data_source",
    "execution": "trader.execution.broker",
    "ib_source": "trader.market.ib_source",
    "market": "trader.market.market_data",
    "news_feed": "trader.market.news_feed",
    "portfolio": "trader.execution.portfolio",
    "scheduler": "trader.scheduling.scheduler",
}

_TOOLS_MEMORY_NAMES = {
    "Memory": ("trader.agent.memory", "Memory"),
    "LearningsStore": ("trader.learnings.raw_store", "LearningsStore"),
    "RawLearningsStore": ("trader.learnings.raw_store", "RawLearningsStore"),
}

_TOOLS_COMPAT_EXPORTS = {
    "execution": (
        "Broker",
        "Commission",
        "CommissionModel",
        "CommissionModelName",
        "Fill",
        "IbkrCommissionModel",
        "NoCommissionModel",
        "Order",
        "Position",
        "Side",
        "SimBroker",
        "commission_model_from_name",
        "compute_fill_effect",
        "round_trip_cost",
    ),
    "scheduler": (
        "STALE_BACKOFF_BASE_MULTIPLIER",
        "STALE_BACKOFF_MAX_MINUTES",
        "STALE_BACKOFF_MAX_STREAK",
        "Scheduler",
    ),
}


class _CompatAliasModule(types.ModuleType):
    """Lazy proxy for historical flat modules moved into capability packages."""

    def __init__(self, alias_name: str, target_name: str, export_names: tuple[str, ...] | None = None) -> None:
        super().__init__(target_name)
        super().__setattr__("_alias_name", alias_name)
        super().__setattr__("_target_name", target_name)
        super().__setattr__("_target_module", None)
        super().__setattr__("_export_names", export_names)
        super().__setattr__("__package__", alias_name.rpartition(".")[0])

    def _target(self) -> types.ModuleType:
        target = super().__getattribute__("_target_module")
        if target is None:
            target = importlib.import_module(super().__getattribute__("_target_name"))
            super().__setattr__("_target_module", target)
        return target

    def __getattr__(self, name: str):
        if name == "__all__":
            export_names = super().__getattribute__("_export_names")
            if export_names is not None:
                return list(export_names)
            return [target_name for target_name in dir(self._target()) if not target_name.startswith("__")]
        return getattr(self._target(), name)

    def __setattr__(self, name: str, value):
        if name.startswith("__") or name in {"_alias_name", "_target_name", "_target_module", "_export_names"}:
            return super().__setattr__(name, value)
        setattr(self._target(), name, value)

    def __delattr__(self, name: str):
        delattr(self._target(), name)

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(dir(self._target())))


class _CompatToolsPackage(types.ModuleType):
    """Virtual ``trader.tools`` package kept for historical imports."""

    def __init__(self, alias_name: str) -> None:
        super().__init__(alias_name)
        super().__setattr__("_alias_name", alias_name)
        super().__setattr__("__package__", alias_name)
        super().__setattr__("__path__", [])
        super().__setattr__("__all__", sorted(set(_TOOLS_COMPAT_MODULES) | {"memory"}))

    def __getattr__(self, name: str):
        if name in _TOOLS_COMPAT_MODULES or name == "memory":
            return importlib.import_module(f"{self.__name__}.{name}")
        raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(_TOOLS_COMPAT_MODULES) | {"memory"})


class _CompatToolsMemoryModule(types.ModuleType):
    """Compatibility module for names split between agent memory and raw learnings."""

    def __init__(self, alias_name: str) -> None:
        super().__init__(alias_name)
        super().__setattr__("_alias_name", alias_name)
        super().__setattr__("__package__", alias_name.rpartition(".")[0])
        super().__setattr__("__all__", sorted(_TOOLS_MEMORY_NAMES))

    def __getattr__(self, name: str):
        target = _TOOLS_MEMORY_NAMES.get(name)
        if target is None:
            raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")
        module_name, attr = target
        return getattr(importlib.import_module(module_name), attr)

    def __setattr__(self, name: str, value):
        if name.startswith("__") or name in {"_alias_name"}:
            return super().__setattr__(name, value)
        if name == "Memory":
            setattr(importlib.import_module("trader.agent.memory"), "Memory", value)
            return None
        if name in {"LearningsStore", "RawLearningsStore"}:
            raw_store = importlib.import_module("trader.learnings.raw_store")
            setattr(raw_store, "LearningsStore", value)
            setattr(raw_store, "RawLearningsStore", value)
            return None
        return super().__setattr__(name, value)

    def __delattr__(self, name: str):
        if name == "Memory":
            delattr(importlib.import_module("trader.agent.memory"), "Memory")
            return None
        if name in {"LearningsStore", "RawLearningsStore"}:
            raw_store = importlib.import_module("trader.learnings.raw_store")
            delattr(raw_store, "LearningsStore")
            delattr(raw_store, "RawLearningsStore")
            return None
        return super().__delattr__(name)


class _CompatAliasFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    _compat_package = __name__

    def find_spec(self, fullname: str, path=None, target=None):  # noqa: ANN001
        tools_name = f"{__name__}.tools"
        if fullname == tools_name:
            spec = importlib.machinery.ModuleSpec(fullname, self, is_package=True)
            spec.submodule_search_locations = []
            return spec
        if fullname.startswith(f"{tools_name}."):
            old_tool_name = fullname.removeprefix(f"{tools_name}.")
            if "." in old_tool_name or (old_tool_name not in _TOOLS_COMPAT_MODULES and old_tool_name != "memory"):
                return None
            return importlib.machinery.ModuleSpec(fullname, self, is_package=False)

        prefix = f"{__name__}."
        if not fullname.startswith(prefix):
            return None
        old_name = fullname.removeprefix(prefix)
        if "." in old_name or old_name not in _COMPAT_MODULES:
            return None
        return importlib.machinery.ModuleSpec(fullname, self, is_package=False)

    def create_module(self, spec):
        tools_name = f"{__name__}.tools"
        if spec.name == tools_name:
            return _CompatToolsPackage(spec.name)
        if spec.name.startswith(f"{tools_name}."):
            old_tool_name = spec.name.rsplit(".", 1)[1]
            if old_tool_name == "memory":
                return _CompatToolsMemoryModule(spec.name)
            return _CompatAliasModule(
                spec.name,
                _TOOLS_COMPAT_MODULES[old_tool_name],
                _TOOLS_COMPAT_EXPORTS.get(old_tool_name),
            )

        old_name = spec.name.rsplit(".", 1)[1]
        return _CompatAliasModule(spec.name, _COMPAT_MODULES[old_name])

    def exec_module(self, module: types.ModuleType) -> None:
        alias_name = module.__dict__.get("_alias_name")
        tools_name = f"{__name__}.tools"
        if alias_name == tools_name:
            setattr(sys.modules[__name__], "tools", module)
        elif isinstance(alias_name, str) and alias_name.startswith(f"{tools_name}."):
            tools_module = importlib.import_module(tools_name)
            setattr(tools_module, alias_name.rsplit(".", 1)[1], module)
        elif isinstance(alias_name, str):
            setattr(sys.modules[__name__], alias_name.rsplit(".", 1)[1], module)


def _install_compat_finder() -> None:
    for finder in sys.meta_path:
        if getattr(finder, "_compat_package", None) == __name__:
            return
    sys.meta_path.insert(0, _CompatAliasFinder())


def __getattr__(name: str):
    if name in _COMPAT_MODULES:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_install_compat_finder()
