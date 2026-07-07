from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("trader", "tests", "scripts")
IGNORED_PARTS = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__"}
DOMAIN_ARG_NAMES = {
    "commission",
    "fill",
    "order",
    "p",
    "p1",
    "p2",
    "plan",
    "position",
    "previous_plan",
    "profit_protection",
    "protection",
    "take_profit",
    "tp",
    "trailing_stop",
}
DOMAIN_FACTORY_NAMES = {
    "Commission",
    "Fill",
    "Order",
    "Position",
    "ProfitProtection",
    "TakeProfit",
    "TradePlan",
    "TrailingStop",
    "create_trade_plan",
    "create_trade_plan_from_order",
    "row_to_plan",
    "trade_plan_from_dict",
}


def _python_files() -> list[Path]:
    return [
        path
        for scan_root in SCAN_ROOTS
        for path in (ROOT / scan_root).rglob("*.py")
        if not (IGNORED_PARTS & set(path.relative_to(ROOT).parts))
    ]


def _called_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _uses_domain_model(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in DOMAIN_ARG_NAMES
    if isinstance(node, ast.Call):
        return (_called_name(node.func) or "") in DOMAIN_FACTORY_NAMES
    return False


# This is a heuristic guard, not a formal type checker: it catches obvious
# dataclass helper calls on domain-looking variable names or constructors,
# including import aliases, but cannot prove the runtime type of arbitrary
# expressions.
def _find_dataclass_helper_offenders(paths: list[Path]) -> list[str]:
    offenders: list[str] = []

    for path in paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        helper_names: dict[str, str] = {}
        dataclasses_module_names: set[str] = set()

        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "dataclasses":
                for alias in node.names:
                    if alias.name in {"asdict", "replace"}:
                        helper_names[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "dataclasses":
                        dataclasses_module_names.add(alias.asname or alias.name)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue

            canonical_helper = None
            if isinstance(node.func, ast.Name):
                canonical_helper = helper_names.get(node.func.id)
            elif (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in dataclasses_module_names
                and node.func.attr in {"asdict", "replace"}
            ):
                canonical_helper = node.func.attr

            if canonical_helper is None:
                continue

            if _uses_domain_model(node.args[0]):
                try:
                    display_path = str(path.relative_to(ROOT))
                except ValueError:
                    display_path = str(path)
                offenders.append(
                    f"{display_path}:{node.lineno}: dataclasses.{canonical_helper} on domain model"
                )

    return offenders


def test_static_scan_includes_scripts_directory() -> None:
    assert any(path.relative_to(ROOT).parts[0] == "scripts" for path in _python_files())


def test_static_guard_flags_dataclass_helper_aliases(tmp_path: Path) -> None:
    suspect = tmp_path / "suspect.py"
    suspect.write_text(
        "from dataclasses import asdict as dump, replace as swap\n"
        "dump(plan)\n"
        "swap(fill, quantity=1.0)\n"
    )

    assert _find_dataclass_helper_offenders([suspect]) == [
        f"{suspect}:2: dataclasses.asdict on domain model",
        f"{suspect}:3: dataclasses.replace on domain model",
    ]


def test_dataclass_helpers_are_not_called_on_domain_models() -> None:
    assert _find_dataclass_helper_offenders(_python_files()) == []
