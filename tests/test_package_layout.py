import subprocess
import sys
from pathlib import Path


def test_capability_modules_are_not_flat_files() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"

    flat_files = sorted(path.name for path in trader_dir.glob("*.py"))

    assert flat_files == ["__init__.py"]


def test_legacy_flat_module_imports_remain_compatible() -> None:
    import trader.cockpit_events as legacy_cockpit_events
    import trader.codex_client as legacy_codex_client
    import trader.daemon as legacy_daemon
    import trader.fx as legacy_fx
    import trader.llm as legacy_llm
    import trader.palette as legacy_palette
    import trader.rotation_schedule as legacy_schedule
    import trader.stats as legacy_stats
    from trader.cockpit import CockpitApp
    from trader import decision_ledger
    from trader.indicator_watch import WATCH_VALID_OPERATORS
    from trader.risk import RiskGate
    from trader.tui import build_view

    assert legacy_cockpit_events.__name__ == "trader.cockpit.events"
    assert legacy_codex_client.Decision.__module__ == "trader.agent_protocol.types"
    assert legacy_daemon.run_cycle.__module__ == "trader.runtime.daemon"
    assert legacy_fx.__name__ == "trader.market.fx"
    assert legacy_llm.LlmRouter.__module__ == "trader.agent.llm"
    assert legacy_palette.__name__ == "trader.ui.palette"
    assert legacy_schedule.__name__ == "trader.rotation.schedule"
    assert legacy_stats.compute_live_kpis.__module__ == "trader.reporting.stats"
    assert decision_ledger.__name__ == "trader.reporting.decision_ledger"
    assert CockpitApp.__module__ == "trader.cockpit.app"
    assert ">" in WATCH_VALID_OPERATORS
    assert RiskGate.__module__ == "trader.execution.risk"
    assert build_view.__module__ == "trader.ui.rich_panels"


def test_legacy_daemon_and_cli_packages_proxy_mutations(monkeypatch, tmp_path) -> None:
    import trader.cli as legacy_cli
    import trader.daemon as legacy_daemon
    from trader.runtime import cli as runtime_cli
    from trader.runtime import daemon as runtime_daemon

    monkeypatch.setattr(legacy_daemon, "STATE_DIR", tmp_path)

    assert runtime_daemon.STATE_DIR == tmp_path
    assert legacy_cli.daemon is runtime_cli.daemon


def test_legacy_daemon_and_cli_python_m_entrypoints() -> None:
    repo_root = Path(__file__).resolve().parents[1]
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


def test_agent_protocol_prompts_do_not_import_agent_context() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    code = (
        "import sys; "
        "import trader.agent_protocol.prompts; "
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
