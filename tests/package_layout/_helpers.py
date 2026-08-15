from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _has_python_sources(path: Path) -> bool:
    if not path.exists():
        return False
    return any("__pycache__" not in candidate.parts for candidate in path.rglob("*.py"))


def _assert_application_submodule_layout(application_dir: Path, submodule: str, modules: list[str]) -> None:
    submodule_dir = application_dir / submodule

    assert (submodule_dir / "__init__.py").exists()
    for module in modules:
        new_path = submodule_dir / f"{module}.py"
        shim_path = application_dir / f"{module}.py"

        assert new_path.exists()
        assert not shim_path.exists()



def _domain_import_violations(paths: list[Path], repo_root: Path) -> list[str]:
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel_path = path.relative_to(repo_root)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level > 0 or node.module is None:
                    continue
                module = node.module
                root = module.split(".", 1)[0]
                if module.startswith("trader.") and not module.startswith("trader.domain"):
                    violations.append(f"{rel_path}: from {module} import ...")
                elif root != "trader" and root not in sys.stdlib_module_names:
                    violations.append(f"{rel_path}: from {module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name
                    root = module.split(".", 1)[0]
                    if module.startswith("trader.") and not module.startswith("trader.domain"):
                        violations.append(f"{rel_path}: import {module}")
                    elif root != "trader" and root not in sys.stdlib_module_names:
                        violations.append(f"{rel_path}: import {module}")
    return violations


def _annotation_mentions_scheduler_class(annotation: ast.AST | None) -> bool:
    if annotation is None:
        return False
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name) and node.id == "Scheduler":
            return True
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "Scheduler"
            and isinstance(node.value, ast.Name)
            and node.value.id == "scheduler"
        ):
            return True
    return False


def _module_imports(path: Path, module_name: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module_name:
            return True
        if isinstance(node, ast.Import):
            if any(alias.name == module_name for alias in node.names):
                return True
    return False

