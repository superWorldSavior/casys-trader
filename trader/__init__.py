"""casys-trader — infrastructure de l'agent de trading autonome.

Le daemon (boucle runtime) appelle Codex pour décider, passe par le risk gate,
exécute en paper. La stratégie / les indicateurs / le calendrier de réveil ne
sont PAS codés ici : ils sont définis par l'agent via le mandat et la mémoire.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
import types

__version__ = "0.1.0"

_COMPAT_MODULES = {
    "agent_context": "trader.agent.context",
    "attribution": "trader.reporting.attribution",
    "cli": "trader.runtime.cli",
    "code_version": "trader.support.metadata.code_version",
    "codex_client": "trader.agent.client",
    "consolidator": "trader.learnings.consolidator",
    "daemon": "trader.runtime.daemon",
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
    "palette": "trader.interfaces.ui.palette",
    "pool_config": "trader.support.config.pool",
    "portfolio_config": "trader.support.config.portfolio",
    "process_env": "trader.support.system.process_env",
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
    "stats": "trader.reporting.stats",
    "trade_plan": "trader.planning.trade_plan",
    "tool_usage": "trader.reporting.tool_usage",
    "tool_trace": "trader.reporting.tool_trace",
    "tui": "trader.interfaces.ui.tui",
    "cockpit_events": "trader.interfaces.cockpit.events",
    "cockpit_supervisor": "trader.interfaces.cockpit.supervisor",
}

_COMPAT_MODULE_EXTRA_ATTRS = {
    "attribution": {"main": ("trader.interfaces.cli.attribution", "main")},
    "stats": {"main": ("trader.interfaces.cli.stats", "main")},
    "tool_usage": {"main": ("trader.interfaces.cli.tool_usage", "main")},
}

_COMPAT_PACKAGES = {
    "agent_protocol": {
        "parsing": "trader.agent.protocol.parsing",
        "prompts": "trader.agent.protocol.prompts",
        "types": "trader.agent.protocol.types",
    },
    "agent_tools": {
        "attribution": "trader.agent.tools.attribution",
        "core": "trader.agent.tools.core",
        "freshness": "trader.agent.tools.freshness",
        "indicators": "trader.agent.tools.indicators",
        "learnings": "trader.agent.tools.learnings",
        "plans": "trader.agent.tools.plans",
        "registry": "trader.agent.tools.registry",
        "risk": "trader.agent.tools.risk",
    },
    "cockpit": {
        "__main__": "trader.interfaces.cockpit.__main__",
        "aggregates": "trader.interfaces.cockpit.aggregates",
        "app": "trader.interfaces.cockpit.app",
        "events": "trader.interfaces.cockpit.events",
        "home": "trader.interfaces.cockpit.home",
        "overview": "trader.interfaces.cockpit.overview",
        "supervisor": "trader.interfaces.cockpit.supervisor",
    },
    "commands": {
        "attribution": "trader.interfaces.cli.attribution",
        "stats": "trader.interfaces.cli.stats",
        "tool_usage": "trader.interfaces.cli.tool_usage",
        "tui": "trader.interfaces.cli.tui",
    },
    "config": {
        "pool": "trader.support.config.pool",
        "portfolio": "trader.support.config.portfolio",
    },
    "metadata": {
        "code_version": "trader.support.metadata.code_version",
    },
    "queue": {
        "decide_pool": "trader.infrastructure.queue.decide_pool",
        "ledger": "trader.infrastructure.queue.ledger",
        "pools": "trader.infrastructure.queue.pools",
        "shadow": "trader.infrastructure.queue.shadow",
        "worker": "trader.infrastructure.queue.worker",
    },
    "read_models": {
        "live_kpis": "trader.reporting.read_models.live_kpis",
        "runtime_state": "trader.reporting.read_models.runtime_state",
    },
    "state_db": {
        "broker_factory": "trader.infrastructure.state_db.broker_factory",
        "broker_store": "trader.infrastructure.state_db.broker_store",
        "compare": "trader.infrastructure.state_db.compare",
        "connection": "trader.infrastructure.state_db.connection",
        "migrations": "trader.infrastructure.state_db.migrations",
        "scheduler_store": "trader.infrastructure.state_db.scheduler_store",
        "shadow": "trader.infrastructure.state_db.shadow",
        "trade_plan_store": "trader.infrastructure.state_db.trade_plan_store",
        "unit_of_work": "trader.infrastructure.state_db.unit_of_work",
    },
    "system": {
        "process_env": "trader.support.system.process_env",
    },
    "ui": {
        "palette": "trader.interfaces.ui.palette",
        "rich_panels": "trader.interfaces.ui.rich_panels",
        "tui": "trader.interfaces.ui.tui",
    },
}

_COMPAT_PACKAGE_ALIASES = {
    "agent_protocol": "trader.agent.protocol",
    "agent_tools": "trader.agent.tools",
    "cockpit": "trader.interfaces.cockpit",
    "commands": "trader.interfaces.cli",
    "queue": "trader.infrastructure.queue",
    "state_db": "trader.infrastructure.state_db",
    "ui": "trader.interfaces.ui",
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

    def __init__(
        self,
        alias_name: str,
        target_name: str,
        export_names: tuple[str, ...] | None = None,
        extra_attrs: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(target_name)
        super().__setattr__("_alias_name", alias_name)
        super().__setattr__("_target_name", target_name)
        super().__setattr__("_target_module", None)
        super().__setattr__("_export_names", export_names)
        super().__setattr__("_extra_attrs", extra_attrs or {})
        super().__setattr__("__package__", alias_name.rpartition(".")[0])

    def _target(self) -> types.ModuleType:
        target = super().__getattribute__("_target_module")
        if target is None:
            target = importlib.import_module(super().__getattribute__("_target_name"))
            super().__setattr__("_target_module", target)
        return target

    def _extra_attr(self, name: str):
        extra_attrs = super().__getattribute__("_extra_attrs")
        if name not in extra_attrs:
            raise AttributeError(name)
        module_name, attr_name = extra_attrs[name]
        return getattr(importlib.import_module(module_name), attr_name)

    def __getattr__(self, name: str):
        if name == "__all__":
            export_names = super().__getattribute__("_export_names")
            extra_names = set(super().__getattribute__("_extra_attrs"))
            if export_names is not None:
                return sorted(set(export_names) | extra_names)
            return sorted(
                {target_name for target_name in dir(self._target()) if not target_name.startswith("__")} | extra_names
            )
        extra_attrs = super().__getattribute__("_extra_attrs")
        if name in extra_attrs:
            return self._extra_attr(name)
        return getattr(self._target(), name)

    def __setattr__(self, name: str, value):
        if name.startswith("__") or name in {
            "_alias_name",
            "_target_name",
            "_target_module",
            "_export_names",
            "_extra_attrs",
            "_modules",
        }:
            return super().__setattr__(name, value)
        extra_attrs = super().__getattribute__("_extra_attrs")
        if name in extra_attrs:
            module_name, attr_name = extra_attrs[name]
            setattr(importlib.import_module(module_name), attr_name, value)
            return None
        setattr(self._target(), name, value)

    def __delattr__(self, name: str):
        extra_attrs = super().__getattribute__("_extra_attrs")
        if name in extra_attrs:
            module_name, attr_name = extra_attrs[name]
            delattr(importlib.import_module(module_name), attr_name)
            return None
        delattr(self._target(), name)

    def __dir__(self) -> list[str]:
        extra_names = set(super().__getattribute__("_extra_attrs"))
        return sorted(set(super().__dir__()) | set(dir(self._target())) | extra_names)


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


class _CompatPackage(types.ModuleType):
    """Virtual legacy package with explicitly mapped submodules."""

    def __init__(self, alias_name: str, modules: dict[str, str]) -> None:
        super().__init__(alias_name)
        super().__setattr__("_alias_name", alias_name)
        super().__setattr__("_modules", modules)
        super().__setattr__("__package__", alias_name)
        super().__setattr__("__path__", [])
        super().__setattr__("__file__", None)
        super().__setattr__("__all__", sorted(modules))

    def __getattr__(self, name: str):
        if name == "__file__":
            return None
        modules = super().__getattribute__("_modules")
        if name in modules:
            return importlib.import_module(f"{self.__name__}.{name}")
        raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

    def __dir__(self) -> list[str]:
        modules = super().__getattribute__("_modules")
        return sorted(set(super().__dir__()) | set(modules))


class _CompatPackageAliasModule(_CompatAliasModule):
    """Virtual legacy package proxying a canonical package."""

    def __init__(self, alias_name: str, target_name: str, modules: dict[str, str]) -> None:
        super().__init__(alias_name, target_name)
        super().__setattr__("_modules", modules)
        super().__setattr__("__package__", alias_name)
        super().__setattr__("__path__", [])

    def __getattr__(self, name: str):
        if name == "__file__":
            return None
        modules = super().__getattribute__("_modules")
        if name in modules:
            return importlib.import_module(f"{self.__name__}.{name}")
        return super().__getattr__(name)

    def __dir__(self) -> list[str]:
        modules = super().__getattribute__("_modules")
        return sorted(set(super().__dir__()) | set(modules))


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

    def _target_name_for(self, fullname: str) -> str | None:
        prefix = f"{__name__}."
        tools_name = f"{__name__}.tools"
        if fullname.startswith(f"{tools_name}."):
            old_tool_name = fullname.removeprefix(f"{tools_name}.")
            return _TOOLS_COMPAT_MODULES.get(old_tool_name)
        if not fullname.startswith(prefix):
            return None
        old_name = fullname.removeprefix(prefix)
        package_alias_target = _COMPAT_PACKAGE_ALIASES.get(old_name)
        if package_alias_target is not None:
            return package_alias_target
        if "." not in old_name:
            return _COMPAT_MODULES.get(old_name)
        package_name, _, submodule_name = old_name.partition(".")
        return _COMPAT_PACKAGES.get(package_name, {}).get(submodule_name)

    def _module_spec(self, fullname: str, *, is_package: bool) -> importlib.machinery.ModuleSpec:
        spec = importlib.machinery.ModuleSpec(fullname, self, is_package=is_package)
        if is_package:
            spec.submodule_search_locations = []
            return spec
        target_name = self._target_name_for(fullname)
        if target_name is not None:
            target_spec = importlib.util.find_spec(target_name)
            if target_spec is not None:
                spec.origin = target_spec.origin
        return spec

    def find_spec(self, fullname: str, path=None, target=None):  # noqa: ANN001
        prefix = f"{__name__}."
        if fullname.startswith(prefix):
            old_name = fullname.removeprefix(prefix)
            if "." not in old_name and old_name in _COMPAT_PACKAGE_ALIASES:
                return self._module_spec(fullname, is_package=True)
            if "." not in old_name and old_name in _COMPAT_PACKAGES:
                return self._module_spec(fullname, is_package=True)
            package_name, _, submodule_name = old_name.partition(".")
            if (
                package_name in _COMPAT_PACKAGES
                and submodule_name in _COMPAT_PACKAGES[package_name]
                and "." not in submodule_name
            ):
                return self._module_spec(fullname, is_package=False)

        tools_name = f"{__name__}.tools"
        if fullname == tools_name:
            return self._module_spec(fullname, is_package=True)
        if fullname.startswith(f"{tools_name}."):
            old_tool_name = fullname.removeprefix(f"{tools_name}.")
            if "." in old_tool_name or (old_tool_name not in _TOOLS_COMPAT_MODULES and old_tool_name != "memory"):
                return None
            return self._module_spec(fullname, is_package=False)

        if not fullname.startswith(prefix):
            return None
        old_name = fullname.removeprefix(prefix)
        if "." in old_name or old_name not in _COMPAT_MODULES:
            return None
        return self._module_spec(fullname, is_package=False)

    def get_code(self, fullname: str):
        target_name = self._target_name_for(fullname)
        if target_name is None:
            if fullname == f"{__name__}.tools.memory":
                return compile("", f"<{fullname}>", "exec")
            return None
        target_spec = importlib.util.find_spec(target_name)
        if target_spec is None or target_spec.loader is None:
            return None
        get_code = getattr(target_spec.loader, "get_code", None)
        if get_code is not None:
            return get_code(target_name)
        get_source = getattr(target_spec.loader, "get_source", None)
        if get_source is None:
            return None
        source = get_source(target_name)
        if source is None:
            return None
        filename = getattr(target_spec.loader, "get_filename", lambda _name: f"<{fullname}>")(target_name)
        return compile(source, filename, "exec")

    def create_module(self, spec):
        prefix = f"{__name__}."
        old_name = spec.name.removeprefix(prefix)
        if "." not in old_name and old_name in _COMPAT_PACKAGE_ALIASES:
            return _CompatPackageAliasModule(spec.name, _COMPAT_PACKAGE_ALIASES[old_name], _COMPAT_PACKAGES[old_name])
        if "." not in old_name and old_name in _COMPAT_PACKAGES:
            return _CompatPackage(spec.name, _COMPAT_PACKAGES[old_name])
        package_name, _, submodule_name = old_name.partition(".")
        if package_name in _COMPAT_PACKAGES and submodule_name in _COMPAT_PACKAGES[package_name]:
            return _CompatAliasModule(spec.name, _COMPAT_PACKAGES[package_name][submodule_name])

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
        return _CompatAliasModule(
            spec.name,
            _COMPAT_MODULES[old_name],
            extra_attrs=_COMPAT_MODULE_EXTRA_ATTRS.get(old_name),
        )

    def exec_module(self, module: types.ModuleType) -> None:
        alias_name = module.__dict__.get("_alias_name")
        tools_name = f"{__name__}.tools"
        prefix = f"{__name__}."
        old_name = alias_name.removeprefix(prefix) if isinstance(alias_name, str) else ""
        if "." not in old_name and old_name in _COMPAT_PACKAGE_ALIASES:
            setattr(sys.modules[__name__], old_name, module)
        elif "." not in old_name and old_name in _COMPAT_PACKAGES:
            setattr(sys.modules[__name__], old_name, module)
        elif isinstance(alias_name, str) and "." in old_name:
            package_name, _, submodule_name = old_name.partition(".")
            if package_name in _COMPAT_PACKAGES and submodule_name in _COMPAT_PACKAGES[package_name]:
                package_module = importlib.import_module(f"{__name__}.{package_name}")
                setattr(package_module, submodule_name, module)
                return None

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
    if name in _COMPAT_PACKAGE_ALIASES:
        return importlib.import_module(f"{__name__}.{name}")
    if name in _COMPAT_PACKAGES:
        return importlib.import_module(f"{__name__}.{name}")
    if name in _COMPAT_MODULES:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_install_compat_finder()
