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


def test_runtime_code_version_import_remains_a_metadata_shim() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    code = """
from trader.metadata import code_version as metadata_code_version
from trader.runtime import code_version as runtime_code_version

assert runtime_code_version.SCHEMA_VERSION == metadata_code_version.SCHEMA_VERSION
assert runtime_code_version.MAX_DIRTY_FILES == metadata_code_version.MAX_DIRTY_FILES
assert runtime_code_version.current_code_version is metadata_code_version.current_code_version
assert runtime_code_version.historical_code_version is metadata_code_version.historical_code_version
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
