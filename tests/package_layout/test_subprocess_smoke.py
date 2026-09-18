import subprocess
import sys

from tests.package_layout._helpers import (
    REPO_ROOT,
)


def test_rotation_python_m_core_smoke_has_no_preimport_warning() -> None:
    repo_root = REPO_ROOT

    for module_name in ("trader.market.rotation.core", "trader.rotation.core"):
        result = subprocess.run(
            [sys.executable, "-m", module_name, "--help"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        assert result.returncode == 0, f"{module_name}: {result.stderr}"
        assert "RuntimeWarning" not in result.stderr



def test_legacy_virtual_packages_support_from_trader_and_python_m() -> None:
    import trader
    from trader import config as legacy_config

    assert trader.config is legacy_config

    repo_root = REPO_ROOT
    for module_name in (
        "trader.agent_protocol.parsing",
        "trader.agent_protocol.prompts",
        "trader.agent_tools.core",
        "trader.agent_tools.registry",
        "trader.tools.memory",
        "trader.config.pool",
        "trader.config.portfolio",
        "trader.metadata.code_version",
        "trader.system.process_env",
        "trader.read_models.live_kpis",
        "trader.read_models.runtime_state",
        "trader.semantic.catalog",
        "trader.scheduling.scheduler",
        "trader.tools.scheduler",
        "trader.learnings.raw_store",
        "trader.learnings.store",
        "trader.learnings.embeddings",
        "trader.learnings.consolidator",
        "trader.rotation",
        "trader.rotation.bench",
        "trader.rotation.collectors",
        "trader.rotation.core",
        "trader.rotation.ledger",
        "trader.rotation.override",
        "trader.rotation.schedule",
        "trader.rotation.state",
        "trader.rotation.venues",
        "trader.rotation.wiring",
    ):
        result = subprocess.run(
            [sys.executable, "-m", module_name],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        assert result.returncode == 0, f"{module_name}: {result.stderr}"



def test_legacy_daemon_and_cli_python_m_entrypoints() -> None:
    repo_root = REPO_ROOT
    for module_name in ("trader.daemon", "trader.cli"):
        result = subprocess.run(
            [sys.executable, "-m", module_name, "--help"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout



def test_reporting_command_python_m_entrypoints() -> None:
    repo_root = REPO_ROOT
    for module_name in (
        "trader.interfaces.cli.attribution",
        "trader.interfaces.cli.stats",
        "trader.interfaces.cli.tool_usage",
        "trader.commands.attribution",
        "trader.commands.stats",
        "trader.commands.tool_usage",
        "trader.reporting.attribution",
        "trader.reporting.stats",
        "trader.attribution",
        "trader.stats",
        "trader.tool_usage",
    ):
        result = subprocess.run(
            [sys.executable, "-m", module_name, "--help"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout



def test_agent_protocol_prompts_do_not_import_agent_context() -> None:
    repo_root = REPO_ROOT
    code = (
        "import sys; "
        "import trader.agent.protocol.prompts; "
        "assert 'trader.agent.context' not in sys.modules, "
        "sorted(name for name in sys.modules if name.startswith('trader.agent'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr



def test_agent_protocol_type_import_stays_light() -> None:
    repo_root = REPO_ROOT
    code = """
import sys

from trader.agent.protocol import IndicatorRequest
from trader.domain.decisions import IndicatorRequest as DomainIndicatorRequest

assert IndicatorRequest is DomainIndicatorRequest
loaded = set(sys.modules)
for name in (
    "trader.market",
    "trader.planning",
    "trader.reporting",
    "trader.semantic",
    "trader.agent.context",
    "trader.agent.client",
    "trader.agent.llm",
):
    assert name not in loaded, sorted(m for m in loaded if m.startswith("trader."))
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

