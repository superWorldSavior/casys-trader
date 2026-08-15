import ast

from tests.package_layout._helpers import (
    REPO_ROOT,
)


def test_cockpit_universe_projection_is_canonical_and_textual_free() -> None:
    repo_root = REPO_ROOT
    cockpit_dir = repo_root / "trader" / "interfaces" / "cockpit"
    projection_path = cockpit_dir / "projections" / "universe.py"
    page_path = cockpit_dir / "pages" / "universe.py"

    assert projection_path.exists()
    projection_source = projection_path.read_text(encoding="utf-8")
    assert "textual" not in projection_source
    assert "rich." not in projection_source

    page_source = page_path.read_text(encoding="utf-8")
    page_tree = ast.parse(page_source, filename=str(page_path))
    page_definitions = {
        node.name
        for node in ast.walk(page_tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert page_definitions.isdisjoint(
        {"SymbolRowData", "build_symbol_rows", "_venue_of_safe"}
    )

    populate = next(
        node
        for node in page_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_populate_universe_table"
    )
    populate_calls = {
        node.func.id
        for node in ast.walk(populate)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "build_symbol_rows" in populate_calls
    assert "_safe_float" not in populate_calls

    from trader.interfaces.cockpit.pages import universe as page
    from trader.interfaces.cockpit.projections.universe import (
        SymbolRowData,
        build_symbol_rows,
        venue_of_safe,
    )

    assert page.SymbolRowData is SymbolRowData
    assert page.build_symbol_rows is build_symbol_rows
    assert page._venue_of_safe is venue_of_safe



def test_cockpit_decision_projection_is_canonical_and_textual_free() -> None:
    repo_root = REPO_ROOT
    cockpit_dir = repo_root / "trader" / "interfaces" / "cockpit"
    projection_path = cockpit_dir / "projections" / "decisions.py"
    page_path = cockpit_dir / "pages" / "decisions.py"

    assert projection_path.exists()
    projection_source = projection_path.read_text(encoding="utf-8")
    assert "textual" not in projection_source
    assert "rich." not in projection_source

    page_source = page_path.read_text(encoding="utf-8")
    page_tree = ast.parse(page_source, filename=str(page_path))
    page_definitions = {
        node.name
        for node in ast.walk(page_tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert page_definitions.isdisjoint({"_fmt_conf", "_has_detail"})
    assert "_count_filters" not in page_source
    assert "_filter_rows" not in page_source
    assert "_group_into_ledger_rows" not in page_source

    populate = next(
        node
        for node in page_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "populate_ledger_table"
    )
    populate_calls = {
        node.func.id
        for node in ast.walk(populate)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "build_ledger_rows" in populate_calls

    refresh = next(
        node
        for node in ast.walk(page_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_refresh_ledger"
    )
    refresh_calls = {
        node.func.id
        for node in ast.walk(refresh)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "project_decision_ledger" in refresh_calls

    from trader.interfaces.cockpit.pages import decisions as page
    from trader.interfaces.cockpit.projections.decisions import (
        format_confidence,
        has_decision_detail,
    )
    from trader.reporting.read_models import decision_filters

    assert page._fmt_conf is format_confidence
    assert page._has_detail is has_decision_detail
    assert decision_filters._count_filters is decision_filters.count_filters
    assert decision_filters._filter_rows is decision_filters.filter_rows
    assert (
        decision_filters._group_into_ledger_rows
        is decision_filters.group_into_ledger_rows
    )



def test_cockpit_portfolio_projection_is_canonical_and_textual_free() -> None:
    repo_root = REPO_ROOT
    cockpit_dir = repo_root / "trader" / "interfaces" / "cockpit"
    projection_path = cockpit_dir / "projections" / "portfolio.py"
    page_path = cockpit_dir / "pages" / "portfolio.py"

    assert projection_path.exists()
    projection_source = projection_path.read_text(encoding="utf-8")
    assert "textual" not in projection_source
    assert "rich." not in projection_source

    page_source = page_path.read_text(encoding="utf-8")
    page_tree = ast.parse(page_source, filename=str(page_path))
    page_definitions = {
        node.name
        for node in ast.walk(page_tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert page_definitions.isdisjoint(
        {
            "PortfolioPositionsProjection",
            "PositionRow",
            "_pnl_pct",
            "_sort_holdings",
            "build_positions_rows",
        }
    )

    populate = next(
        node
        for node in ast.walk(page_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_populate_positions"
    )
    populate_calls = {
        node.func.id
        for node in ast.walk(populate)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "project_portfolio_positions" in populate_calls
    assert "_safe_float" not in populate_calls
    assert "positions_by_pnl" not in page_source

    from trader.interfaces.cockpit.pages import portfolio as page
    from trader.interfaces.cockpit.projections import portfolio as projection

    assert page._pnl_pct is projection.pnl_pct
    assert page._sort_holdings is projection.sort_holdings
    assert page.build_positions_rows is projection.build_positions_rows



def test_cockpit_exit_plan_projection_is_canonical_and_textual_free() -> None:
    repo_root = REPO_ROOT
    cockpit_dir = repo_root / "trader" / "interfaces" / "cockpit"
    projection_path = cockpit_dir / "projections" / "plans.py"
    page_path = cockpit_dir / "pages" / "plans.py"

    assert projection_path.exists()
    projection_source = projection_path.read_text(encoding="utf-8")
    assert "textual" not in projection_source
    assert "rich." not in projection_source

    page_source = page_path.read_text(encoding="utf-8")
    page_tree = ast.parse(page_source, filename=str(page_path))
    page_definitions = {
        node.name
        for node in ast.walk(page_tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert page_definitions.isdisjoint(
        {
            "ActiveWatchRow",
            "ActiveWatchesProjection",
            "ArmedOrderRow",
            "ArmedOrdersProjection",
            "ExitPlanRow",
            "ExitPlansProjection",
            "ExitWatchRow",
            "ExitWatchesProjection",
            "_exit_update_rejected_symbols",
            "_price_fmt",
            "_stop_distance_sort_key",
            "_stop_pct",
            "_tp_label",
        }
    )

    for function_name in ("build_exit_plans", "build_exit_plans_compact"):
        builder = next(
            node
            for node in page_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == function_name
        )
        calls = {
            node.func.id
            for node in ast.walk(builder)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "project_exit_plans" in calls
        assert "_safe_float" not in calls

    from trader.interfaces.cockpit.pages import plans as page
    from trader.interfaces.cockpit.projections import plans as projection

    assert page._price_fmt is projection.format_price
    assert page._stop_pct is projection.stop_pct
    assert page._tp_label is projection.take_profit_label
    assert (
        page._exit_update_rejected_symbols
        is projection.exit_update_rejected_symbols
    )
    assert page._stop_distance_sort_key is projection.stop_distance_sort_key

    builders_to_projection = {
        "build_armed": "project_armed_orders",
        "build_watches": "project_active_watches",
        "build_exit_watches": "project_exit_watches",
    }
    for function_name, projection_name in builders_to_projection.items():
        builder = next(
            node
            for node in page_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == function_name
        )
        calls = {
            node.func.id
            for node in ast.walk(builder)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert projection_name in calls
    assert "armed_watches" not in page_source
    assert "plain_watches" not in page_source

    decisions_path = cockpit_dir / "pages" / "decisions.py"
    decisions_source = decisions_path.read_text(encoding="utf-8")
    assert "state.get('armed_plans')" not in decisions_source
    for projection_name in (
        "project_active_watches",
        "project_armed_orders",
        "project_exit_plans",
        "project_exit_watches",
    ):
        assert projection_name in decisions_source



def test_cockpit_format_is_rich_free_and_meter_renderer_is_explicit() -> None:
    repo_root = REPO_ROOT
    cockpit_dir = repo_root / "trader" / "interfaces" / "cockpit"
    format_path = cockpit_dir / "format.py"
    meter_path = cockpit_dir / "renderers" / "meters.py"
    home_path = cockpit_dir / "pages" / "home.py"

    assert meter_path.exists()
    format_source = format_path.read_text(encoding="utf-8")
    assert "from rich" not in format_source
    assert "interfaces.ui.palette" not in format_source

    home_source = home_path.read_text(encoding="utf-8")
    assert "from trader.interfaces.cockpit.renderers.meters import confidence_meter" in home_source
    assert "f.conf_meter" not in home_source

    from trader.interfaces.cockpit import format as cockpit_format
    from trader.interfaces.cockpit.renderers.meters import confidence_meter

    assert cockpit_format.conf_meter(0.5).plain == confidence_meter(0.5).plain



def test_cockpit_health_projection_is_canonical_and_textual_free() -> None:
    repo_root = REPO_ROOT
    cockpit_dir = repo_root / "trader" / "interfaces" / "cockpit"
    projection_path = cockpit_dir / "projections" / "health.py"
    page_path = cockpit_dir / "pages" / "health.py"

    assert projection_path.exists()
    projection_source = projection_path.read_text(encoding="utf-8")
    assert "textual" not in projection_source
    assert "rich." not in projection_source
    assert "os.getenv" not in projection_source

    page_source = page_path.read_text(encoding="utf-8")
    page_tree = ast.parse(page_source, filename=str(page_path))
    page_definitions = {
        node.name
        for node in ast.walk(page_tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_symbols_by_venue" not in page_definitions

    builders_to_projection = {
        "build_freshness": "project_freshness",
        "build_fx_rates": "project_fx_rates",
        "build_learnings": "project_learnings",
        "build_llm": "project_llm_health",
        "build_sources": "project_sources",
        "build_universe": "project_universe_health",
    }
    for function_name, projection_name in builders_to_projection.items():
        builder = next(
            node
            for node in page_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == function_name
        )
        calls = {
            node.func.id
            for node in ast.walk(builder)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert projection_name in calls

    from trader.interfaces.cockpit.pages import health as page
    from trader.interfaces.cockpit.projections import health as projection

    assert page._symbols_by_venue is projection.symbols_by_venue



def test_cockpit_uses_support_coercion_instead_of_private_reporting_helpers() -> None:
    repo_root = REPO_ROOT
    cockpit_dir = repo_root / "trader" / "interfaces" / "cockpit"
    coercion_path = repo_root / "trader" / "support" / "coercion.py"
    runtime_state_path = (
        repo_root / "trader" / "reporting" / "read_models" / "runtime_state.py"
    )

    assert coercion_path.exists()
    violations: list[str] = []
    for path in sorted(cockpit_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "trader.reporting.read_models.runtime_state":
                continue
            private_names = [
                alias.name for alias in node.names if alias.name.startswith("_safe_")
            ]
            if private_names:
                violations.append(
                    f"{path.relative_to(repo_root)}: {', '.join(private_names)}"
                )
    assert violations == []

    runtime_tree = ast.parse(
        runtime_state_path.read_text(encoding="utf-8"),
        filename=str(runtime_state_path),
    )
    runtime_definitions = {
        node.name
        for node in runtime_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_safe_float" not in runtime_definitions
    assert "_safe_list_of_dicts" not in runtime_definitions

    from trader.reporting.read_models import runtime_state
    from trader.support.coercion import dict_list, finite_float

    assert runtime_state._safe_float is finite_float
    assert runtime_state._safe_list_of_dicts is dict_list

