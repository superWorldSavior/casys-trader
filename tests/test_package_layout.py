from pathlib import Path


def test_capability_modules_are_not_flat_files() -> None:
    trader_dir = Path(__file__).resolve().parents[1] / "trader"
    forbidden = {
        "attribution.py",
        "cockpit.py",
        "cockpit_events.py",
        "cockpit_supervisor.py",
        "decision_audit.py",
        "decision_bench.py",
        "decision_ledger.py",
        "family_regime.py",
        "features.py",
        "fx.py",
        "fx_rates.py",
        "gross_priority.py",
        "macro_calendar.py",
        "macro_series.py",
        "meta_performance.py",
        "palette.py",
        "pool_config.py",
        "portfolio_config.py",
        "process_env.py",
        "radar.py",
        "radar_config.py",
        "radar_data.py",
        "regime.py",
        "rotation.py",
        "rotation_bench.py",
        "rotation_collectors.py",
        "rotation_daemon.py",
        "rotation_ledger.py",
        "rotation_override.py",
        "rotation_schedule.py",
        "rotation_state.py",
        "rotation_venues.py",
        "rotation_wiring.py",
        "stats.py",
        "tool_trace.py",
        "tool_usage.py",
        "tui.py",
    }

    flat_files = {path.name for path in trader_dir.glob("*.py")}

    assert forbidden.isdisjoint(flat_files)


def test_legacy_flat_module_imports_remain_compatible() -> None:
    import trader.cockpit_events as legacy_cockpit_events
    import trader.fx as legacy_fx
    import trader.palette as legacy_palette
    import trader.rotation_schedule as legacy_schedule
    import trader.stats as legacy_stats
    from trader.cockpit import CockpitApp
    from trader import decision_ledger
    from trader.tui import build_view

    assert legacy_cockpit_events.__name__ == "trader.cockpit.events"
    assert legacy_fx.__name__ == "trader.market.fx"
    assert legacy_palette.__name__ == "trader.ui.palette"
    assert legacy_schedule.__name__ == "trader.rotation.schedule"
    assert legacy_stats.compute_live_kpis.__module__ == "trader.reporting.stats"
    assert decision_ledger.__name__ == "trader.reporting.decision_ledger"
    assert CockpitApp.__module__ == "trader.cockpit.app"
    assert build_view.__module__ == "trader.ui.rich_panels"
