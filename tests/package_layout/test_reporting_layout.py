import ast

from tests.package_layout._helpers import (
    REPO_ROOT,
    _module_imports,
)


def test_universe_pipeline_read_model_is_canonical_runtime_state_dependency() -> None:
    repo_root = REPO_ROOT
    read_models_dir = repo_root / "trader" / "reporting" / "read_models"
    canonical_path = read_models_dir / "universe_pipeline.py"
    assembler_path = read_models_dir / "runtime_state.py"

    assert canonical_path.exists()

    assembler_tree = ast.parse(
        assembler_path.read_text(encoding="utf-8"), filename=str(assembler_path)
    )
    assembler_definitions = {
        node.name
        for node in ast.walk(assembler_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert assembler_definitions.isdisjoint(
        {
            "_read_projection_safe",
            "_latest_activations_by_venue_safe",
            "_scope_pipeline_entry",
            "_scout_pipeline_entry",
            "_brief_pipeline_entry",
            "_agent_pipeline_entry",
            "_activation_pipeline_entry",
        }
    )

    from trader.reporting.read_models import runtime_state
    from trader.reporting.read_models.universe_pipeline import load_universe_pipeline

    assert runtime_state._load_universe_pipeline_safe is load_universe_pipeline



def test_runnable_compatibility_facades_delegate_to_interface_command_modules() -> None:
    trader_dir = REPO_ROOT / "trader"
    command_modules = {
        "attribution": ("trader.reporting.attribution",),
        "stats": ("trader.reporting.read_models.live_kpis", "trader.reporting.renderers.live_kpis"),
        "tool_usage": ("trader.reporting.read_models.tool_usage", "trader.reporting.renderers.tool_usage"),
        "tui": ("trader.interfaces.ui.tui",),
    }

    for command_name, canonical_modules in command_modules.items():
        command_path = trader_dir / "interfaces" / "cli" / f"{command_name}.py"
        legacy_package_main_path = trader_dir / command_name / "__main__.py"
        legacy_module_path = trader_dir / f"{command_name}.py"

        assert command_path.exists(), f"missing canonical command module for {command_name}"
        for canonical_module in canonical_modules:
            assert _module_imports(command_path, canonical_module)
        if legacy_package_main_path.exists():
            assert _module_imports(legacy_package_main_path, f"trader.commands.{command_name}")
        assert not legacy_module_path.exists()



def test_tool_usage_cli_owner_is_command_module() -> None:
    from trader.commands import tool_usage as command_tool_usage
    from trader.reporting import tool_usage as reporting_tool_usage

    assert command_tool_usage.main.__module__ == "trader.interfaces.cli.tool_usage"
    assert not hasattr(reporting_tool_usage, "main")



def test_tool_usage_report_projection_is_read_model_canonical() -> None:
    import trader.tool_usage as legacy_tool_usage
    from trader.reporting import tool_usage as reporting_tool_usage
    from trader.reporting.read_models import tool_usage as read_model_tool_usage

    assert reporting_tool_usage.build_report is read_model_tool_usage.build_report
    assert reporting_tool_usage.domain_tool_usage is read_model_tool_usage.domain_tool_usage
    assert reporting_tool_usage.risk_observability is read_model_tool_usage.risk_observability
    assert legacy_tool_usage.build_report is read_model_tool_usage.build_report
    assert legacy_tool_usage.render_cli is reporting_tool_usage.render_cli



def test_reporting_renderers_are_canonical() -> None:
    from trader.reporting import stats as reporting_stats
    from trader.reporting import tool_usage as reporting_tool_usage
    from trader.reporting.renderers import live_kpis as live_kpis_renderer
    from trader.reporting.renderers import tool_usage as tool_usage_renderer

    assert reporting_stats.render_text is live_kpis_renderer.render_text
    assert reporting_tool_usage.render_cli is tool_usage_renderer.render_cli



def test_stats_cli_owner_is_command_module() -> None:
    from trader.commands import stats as command_stats
    from trader.reporting import stats as reporting_stats

    assert command_stats.main.__module__ == "trader.interfaces.cli.stats"
    assert not hasattr(reporting_stats, "main")



def test_attribution_cli_owner_is_command_module() -> None:
    from trader.commands import attribution as command_attribution
    from trader.reporting import attribution as reporting_attribution

    assert command_attribution.main.__module__ == "trader.interfaces.cli.attribution"
    assert not hasattr(reporting_attribution, "main")



def test_attribution_projection_is_read_model_canonical() -> None:
    import trader.attribution as legacy_attribution
    from trader.reporting import attribution as reporting_attribution
    from trader.reporting.read_models import attribution as read_model_attribution
    from trader.reporting.read_models import hard_stop_diagnostics, trade_history

    assert read_model_attribution.compute_round_trips is trade_history.compute_round_trips
    assert (
        read_model_attribution.compute_hard_stop_diagnostics
        is hard_stop_diagnostics.compute_hard_stop_diagnostics
    )
    assert (
        read_model_attribution.select_hard_stop_symbols
        is hard_stop_diagnostics.select_hard_stop_symbols
    )
    assert (
        read_model_attribution._filter_regime_trips
        is trade_history.filter_regime_trips
    )
    assert reporting_attribution.compute_round_trips is trade_history.compute_round_trips
    assert reporting_attribution.compute_attribution is read_model_attribution.compute_attribution
    assert (
        reporting_attribution.compute_hard_stop_diagnostics
        is hard_stop_diagnostics.compute_hard_stop_diagnostics
    )
    assert legacy_attribution.compute_attribution is read_model_attribution.compute_attribution
    assert legacy_attribution.render_text is reporting_attribution.render_text



def test_attribution_read_models_have_single_responsibility_boundaries() -> None:
    repo_root = REPO_ROOT
    read_models_dir = repo_root / "trader" / "reporting" / "read_models"
    attribution_path = read_models_dir / "attribution.py"
    trade_history_path = read_models_dir / "trade_history.py"
    hard_stop_path = read_models_dir / "hard_stop_diagnostics.py"

    assert trade_history_path.exists()
    assert hard_stop_path.exists()

    attribution_source = attribution_path.read_text(encoding="utf-8")
    attribution_tree = ast.parse(attribution_source, filename=str(attribution_path))
    attribution_definitions = {
        node.name
        for node in ast.walk(attribution_tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert attribution_definitions.isdisjoint(
        {
            "_OpenLeg",
            "compute_hard_stop_diagnostics",
            "compute_round_trips",
            "select_hard_stop_symbols",
        }
    )

    hard_stop_source = hard_stop_path.read_text(encoding="utf-8")
    assert "from trader.reporting.read_models import attribution" not in hard_stop_source
    assert "from trader.reporting.read_models.attribution" not in hard_stop_source
    assert "from trader.reporting.read_models.trade_history import" in hard_stop_source



def test_meta_performance_projection_is_read_model_canonical() -> None:
    from trader.reporting import meta_performance as reporting_meta_performance
    from trader.reporting.read_models import meta_performance as read_model_meta_performance

    assert reporting_meta_performance.compute_meta_performance is read_model_meta_performance.compute_meta_performance
    assert reporting_meta_performance.DEFAULT_HORIZONS is read_model_meta_performance.DEFAULT_HORIZONS
    assert reporting_meta_performance._AUDIT_CACHE is read_model_meta_performance._AUDIT_CACHE



def test_decision_audit_engine_is_audit_package_canonical() -> None:
    from trader.reporting import decision_audit as reporting_decision_audit
    from trader.reporting.audit import decision_quality

    assert reporting_decision_audit.audit_rows is decision_quality.audit_rows
    assert reporting_decision_audit.refresh_audit_payload is decision_quality.refresh_audit_payload
    assert reporting_decision_audit.summarize_audited_rows is decision_quality.summarize_audited_rows
    assert reporting_decision_audit.load_prices_yfinance is decision_quality.load_prices_yfinance
    assert reporting_decision_audit.parse_ts is decision_quality.parse_ts
    assert reporting_decision_audit.parse_horizon is decision_quality.parse_horizon



def test_decision_bench_engine_is_bench_package_canonical() -> None:
    from trader.reporting import decision_bench as reporting_decision_bench
    from trader.reporting.bench import decision_bench as bench_decision_bench

    assert reporting_decision_bench.ModelSpec is bench_decision_bench.ModelSpec
    assert reporting_decision_bench.ModelCompletion is bench_decision_bench.ModelCompletion
    assert reporting_decision_bench.parse_model_specs is bench_decision_bench.parse_model_specs
    assert reporting_decision_bench.select_cases is bench_decision_bench.select_cases
    assert reporting_decision_bench.run_bench is bench_decision_bench.run_bench
    assert reporting_decision_bench.dry_run_payload is bench_decision_bench.dry_run_payload
    assert reporting_decision_bench.render_summary is bench_decision_bench.render_summary



def test_decision_ledger_owners_and_reporting_facades_are_canonical() -> None:
    from trader.application.record import decision_ledger_rows
    from trader.domain import decision_identity
    from trader.infrastructure.files import decision_ledger as infrastructure_decision_ledger
    from trader.reporting import decision_ledger as reporting_decision_ledger
    from trader.reporting.ledger import decision_ledger as ledger_decision_ledger

    assert reporting_decision_ledger.DecisionLedgerStore is infrastructure_decision_ledger.DecisionLedgerStore
    assert ledger_decision_ledger.DecisionLedgerStore is infrastructure_decision_ledger.DecisionLedgerStore
    assert reporting_decision_ledger.build_decision_row is decision_ledger_rows.build_decision_row
    assert ledger_decision_ledger.build_decision_row is decision_ledger_rows.build_decision_row
    assert reporting_decision_ledger.build_legacy_event_row is infrastructure_decision_ledger.build_legacy_event_row
    assert reporting_decision_ledger.seed_existing_reports is infrastructure_decision_ledger.seed_existing_reports
    assert reporting_decision_ledger.seed_existing_events is infrastructure_decision_ledger.seed_existing_events
    assert reporting_decision_ledger.backfill_code_versions is infrastructure_decision_ledger.backfill_code_versions
    assert reporting_decision_ledger.DEFAULT_LEDGER_FILENAME is infrastructure_decision_ledger.DEFAULT_LEDGER_FILENAME
    assert reporting_decision_ledger._decision_id is decision_identity.decision_id
    assert ledger_decision_ledger._decision_id is decision_identity.decision_id
    assert reporting_decision_ledger.code_version is infrastructure_decision_ledger.code_version
    assert ledger_decision_ledger.code_version is infrastructure_decision_ledger.code_version

