import subprocess
import sys
from pathlib import Path


def test_decision_ledger_import_does_not_load_runtime_package() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    code = """
import sys

from trader.reporting import decision_ledger

assert decision_ledger.__name__ == "trader.reporting.decision_ledger"
loaded = set(sys.modules)
assert "trader.runtime" not in loaded, sorted(
    name for name in loaded if name.startswith("trader.")
)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_runtime_code_version_and_process_env_proxies_are_removed() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    assert not (repo_root / "trader" / "runtime" / "code_version.py").exists()
    assert not (repo_root / "trader" / "runtime" / "process_env.py").exists()

    code = """
import ast
from pathlib import Path

import trader.runtime as runtime

repo_root = Path.cwd()
runtime_exports = set(runtime.__all__)
assert "code_version" not in runtime_exports
assert "process_env" not in runtime_exports

expected_imports = {
    "trader/runtime/daemon.py": {
        "trader.support.metadata": {"code_version"},
    },
    "trader/runtime/cli.py": {
        "trader.support.metadata": {"code_version"},
    },
    "trader/interfaces/cockpit/supervisor.py": {
        "trader.support.system.process_env": {"sanitized_runtime_env"},
    },
}

for rel_path, expected_modules in expected_imports.items():
    tree = ast.parse((repo_root / rel_path).read_text())
    imports = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.setdefault(node.module, set()).update(alias.name for alias in node.names)

    for module, names in expected_modules.items():
        assert names <= imports.get(module, set()), (rel_path, imports)

    forbidden_modules = {"trader.runtime.code_version", "trader.runtime.process_env"}
    assert forbidden_modules.isdisjoint(imports), (rel_path, imports)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
