from __future__ import annotations

import ast
from pathlib import Path


BROKER_FACADE_EXPORTS = [
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
]


def test_sim_broker_lives_in_state_db_with_execution_broker_facade() -> None:
    from trader.execution import broker as facade
    from trader.infrastructure.state_db.sim_broker import SimBroker

    assert SimBroker.__module__ == "trader.infrastructure.state_db.sim_broker"
    assert facade.SimBroker is SimBroker
    assert facade.__all__ == BROKER_FACADE_EXPORTS


def test_commission_logic_lives_in_execution_commission_with_broker_facade() -> None:
    from trader.execution import broker as facade
    from trader.execution.commission import (
        IbkrCommissionModel,
        NoCommissionModel,
        commission_model_from_name,
        compute_fill_effect,
        round_trip_cost,
    )

    assert IbkrCommissionModel.__module__ == "trader.execution.commission"
    assert facade.IbkrCommissionModel is IbkrCommissionModel
    assert facade.NoCommissionModel is NoCommissionModel
    assert facade.commission_model_from_name is commission_model_from_name
    assert facade.compute_fill_effect is compute_fill_effect
    assert facade.round_trip_cost is round_trip_cost


def test_broker_factory_imports_json_broker_from_infrastructure_top_level() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    module_path = repo_root / "trader" / "infrastructure" / "state_db" / "broker_factory.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))

    top_level_imports = [node for node in tree.body if isinstance(node, ast.ImportFrom)]
    assert any(
        node.module == "trader.infrastructure.state_db.sim_broker"
        and any(alias.name == "SimBroker" for alias in node.names)
        for node in top_level_imports
    )

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "trader.execution.broker":
            imported = {alias.name for alias in node.names}
            if "SimBroker" in imported:
                violations.append("broker_factory imports SimBroker from execution.broker")

    assert violations == []


def test_commission_and_sim_broker_do_not_import_runtime_or_application() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    checked_modules = [
        repo_root / "trader" / "execution" / "commission.py",
        repo_root / "trader" / "infrastructure" / "state_db" / "sim_broker.py",
    ]
    forbidden_prefixes = (
        "trader.agent",
        "trader.application",
        "trader.interfaces",
        "trader.reporting",
        "trader.runtime",
    )
    violations: list[str] = []

    for module_path in checked_modules:
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        rel_path = module_path.relative_to(repo_root)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith(forbidden_prefixes):
                    violations.append(f"{rel_path}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(forbidden_prefixes):
                        violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []
