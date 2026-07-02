"""Runtime ownership checks for daemon PID-file primitives."""
from __future__ import annotations

import ast
import importlib
import importlib.util
import sys
from pathlib import Path


def _load_supervisor_without_cockpit_package_init():
    supervisor_path = Path(__file__).resolve().parents[1] / "trader" / "cockpit" / "supervisor.py"
    spec = importlib.util.spec_from_file_location("_casys_test_cockpit_supervisor", supervisor_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_pid_file_primitives_live_in_runtime_and_cockpit_delegates() -> None:
    spec = importlib.util.find_spec("trader.runtime.pid_file")
    assert spec is not None, "PID-file ownership primitives must live in trader.runtime.pid_file"

    runtime_pid_file = importlib.import_module("trader.runtime.pid_file")
    cockpit_supervisor = _load_supervisor_without_cockpit_package_init()

    assert cockpit_supervisor.claim_pid_file is runtime_pid_file.claim_pid_file
    assert cockpit_supervisor.release_pid_file is runtime_pid_file.release_pid_file


def test_daemon_imports_pid_file_primitives_from_runtime_not_cockpit() -> None:
    daemon_path = Path(__file__).resolve().parents[1] / "trader" / "runtime" / "daemon.py"
    tree = ast.parse(daemon_path.read_text(encoding="utf-8"))

    forbidden_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "trader.cockpit.supervisor"
        and {alias.name for alias in node.names} & {"claim_pid_file", "release_pid_file"}
    ]

    assert forbidden_imports == []
