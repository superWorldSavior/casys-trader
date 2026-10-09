"""Command line interface for casys-trader."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Sequence

import yaml

from trader.support.metadata import code_version
from trader.infrastructure.files import decision_ledger, ledger_rotation
from trader.domain.market.features import DEFAULT_INDICATORS, build_indicator_snapshot, compute_indicator_values
from trader.market import market_data as market
from trader.runtime import daemon, news_macro_runtime, universe_intelligence_runtime
from trader.runtime.company_intelligence_runtime import CompanyIntelligenceRuntime
from trader.domain.semantic.catalog import (
    FAMILIES,
    describe_semantic_layer,
    find_indicators,
    list_indicators,
    normalize_temporal_query,
)
from trader.reporting.audit import decision_quality as decision_audit
from trader.reporting.bench import decision_bench
from trader.reporting import attribution
from trader.reporting.read_models import decision_flags

_DAEMON_FLAGS = {
    "--live",
    "--once",
    "--poll",
    "--default-wake-minutes",
    "--min-wake-minutes",
    "--max-wake-minutes",
    "--max-context-requests-per-symbol",
    "--max-indicators-per-request",
    "--max-model-calls-per-cycle",
    "--learning-consolidation-threshold",
    "--consolidator-acpx-bin",
    "--consolidator-acpx-agent",
    "--consolidator-model",
    "--consolidator-timeout-s",
    "--bootstrap-all",
    "--ib-host",
    "--ib-port",
    "--ib-client-id",
}

_PRICE_FETCH_ERROR_CODES = frozenset({"no_data", "fetch_failed", "market_error", "unexpected_error"})


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _parse_csv(value: str | None, default: list[str]) -> list[str]:
    if not value:
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


def _load_universe_symbols() -> list[str]:
    """Symboles d'universe.yaml — [] si absent (fichier généré par la rotation)."""
    path = daemon.ROOT / "config" / "universe.yaml"
    if not path.exists():
        return []
    cfg = yaml.safe_load(path.read_text()) or {}
    return list(cfg.get("symbols") or [])


def _read_state_json(filename: str) -> object | None:
    path = daemon.STATE_DIR / filename
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _cmd_company_intelligence_refresh(args: argparse.Namespace) -> int:
    runtime = CompanyIntelligenceRuntime(
        config_dir=daemon.ROOT / "config",
        state_dir=daemon.STATE_DIR,
    )
    try:
        result = runtime.refresh(
            symbols=tuple(args.symbol or ()),
            scope=args.scope,
            depth=args.depth,
            trigger="manual_cli",
            wait=bool(args.wait),
            wait_timeout_s=float(args.wait_timeout_s),
        )
    finally:
        runtime.stop()
    _print_json(result)
    return 0 if not result.get("errors") else 1


def _cmd_company_intelligence_status(args: argparse.Namespace) -> int:
    del args
    runtime = CompanyIntelligenceRuntime(
        config_dir=daemon.ROOT / "config",
        state_dir=daemon.STATE_DIR,
        start_workers=False,
    )
    try:
        result = runtime.status()
    finally:
        runtime.stop()
    _print_json(result)
    return 0


def _cmd_news_macro_refresh(args: argparse.Namespace) -> int:
    selected_venues = (*news_macro_runtime.VENUES, "GLOBAL") if args.all else tuple(dict.fromkeys(args.venue or ()))
    regional_venues = tuple(venue for venue in selected_venues if venue != "GLOBAL")
    result = news_macro_runtime.tick_news_macro_analysis(
        config_dir=daemon.ROOT / "config",
        state_dir=daemon.STATE_DIR,
        loop_now=datetime.now(timezone.utc),
        venues=regional_venues,
        include_global="GLOBAL" in selected_venues,
        force=bool(args.force),
    )
    payload = {
        "requested_venues": list(selected_venues),
        "force": bool(args.force),
        **result,
    }
    _print_json(payload)
    return 0 if not result.get("errors") else 1


def _cmd_universe_refresh(args: argparse.Namespace) -> int:
    selected_venues = universe_intelligence_runtime.VENUES if args.all else tuple(dict.fromkeys(args.venue or ()))
    result = universe_intelligence_runtime.refresh_universe_intelligence(
        config_dir=daemon.ROOT / "config",
        state_dir=daemon.STATE_DIR,
        loop_now=datetime.now(timezone.utc),
        venues=selected_venues,
        force=bool(args.force),
        refresh_macro=not bool(args.no_macro),
    )
    payload = {
        "requested_venues": list(selected_venues),
        "force": bool(args.force),
        "refresh_macro": not bool(args.no_macro),
        **result,
    }
    _print_json(payload)
    return 0 if not result.get("errors") else 1


def _read_broker_state() -> object | None:
    db_path = daemon.STATE_DIR / "casys.db"
    if db_path.exists():
        try:
            from trader.infrastructure.state_db.broker_store import SqliteBroker
            from trader.infrastructure.state_db.connection import open_state_db

            broker = SqliteBroker(open_state_db(db_path))
            return {
                "cash": broker.cash(),
                "positions": {
                    symbol: {
                        "symbol": position.symbol,
                        "quantity": position.quantity,
                        "avg_price": position.avg_price,
                    }
                    for symbol, position in broker.positions().items()
                },
                "fills": broker.fills(),
            }
        except Exception:
            return None
    return _read_state_json("broker.json")


def _read_scheduler_state() -> object | None:
    db_path = daemon.STATE_DIR / "casys.db"
    if db_path.exists():
        try:
            from trader.infrastructure.state_db.connection import open_state_db
            from trader.infrastructure.state_db.scheduler_store import SqliteScheduler

            scheduler = SqliteScheduler(open_state_db(db_path))
            default_next_wake, symbol_wakes = scheduler.wakes()
            return {
                "default_next_wake": default_next_wake,
                "symbols": symbol_wakes,
                "stale_streaks": scheduler.stale_streaks(),
                "indicator_watches": scheduler.watches(),
            }
        except Exception:
            return None
    return _read_state_json("scheduler.json")


def _read_trade_plans_state() -> object | None:
    db_path = daemon.STATE_DIR / "casys.db"
    if db_path.exists():
        try:
            from trader.infrastructure.state_db.connection import open_state_db
            from trader.infrastructure.state_db.trade_plan_store import SqliteTradePlanStore

            db = open_state_db(db_path)
            return {"plans": [p.model_dump() for p in SqliteTradePlanStore(db).open_plans()]}
        except Exception:
            return {"plans": []}
    return _read_state_json("trade_plans.json")


def _load_regime_filters(args: argparse.Namespace) -> tuple[str | None, tuple[str, ...]]:
    if getattr(args, "all_regimes", False):
        return args.since, tuple(args.exclude_symbol or ())

    cfg_path = daemon.ROOT / "config" / "regime.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    cfg = cfg or {}
    since = args.since if args.since is not None else cfg.get("attribution_since")
    exclude_symbols = [str(symbol) for symbol in (cfg.get("exclude_symbols") or [])]
    exclude_symbols.extend(str(symbol) for symbol in (args.exclude_symbol or []))
    return (None if since is None else str(since)), tuple(exclude_symbols)


def _fmt_number(value: object, decimals: int = 2) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return "n/a"


def _price_fetch_error(exc: Exception) -> dict[str, str]:
    if isinstance(exc, market.MarketError):
        error_code = exc.code if exc.code in _PRICE_FETCH_ERROR_CODES else "market_error"
    else:
        error_code = "unexpected_error"
    return {"error_code": error_code, "message": str(exc)}


def _cmd_diagnostics_hard_stops(args: argparse.Namespace) -> int:
    if args.lookahead_bars <= 0:
        raise SystemExit("--lookahead-bars doit être > 0")
    since, exclude_symbols = _load_regime_filters(args)

    hard_stop_symbols = attribution.select_hard_stop_symbols(
        daemon.STATE_DIR,
        since=since,
        exclude_symbols=exclude_symbols,
    )
    bars_by_symbol: dict[str, list[object]] = {}
    fetch_errors: dict[str, dict[str, str]] = {}
    for symbol in hard_stop_symbols:
        try:
            bars_by_symbol[symbol] = market.get_bars(symbol, lookback=args.lookback, interval=args.interval)
        except Exception as exc:  # noqa: BLE001 - diagnostic post-mortem, on garde les autres symboles.
            bars_by_symbol[symbol] = []
            fetch_errors[symbol] = _price_fetch_error(exc)

    payload = attribution.compute_hard_stop_diagnostics(
        daemon.STATE_DIR,
        bars_by_symbol,
        since=since,
        exclude_symbols=exclude_symbols,
        lookahead_bars=args.lookahead_bars,
        interval=args.interval,
    )
    payload["price_fetch"] = {
        "lookback": args.lookback,
        "symbols": hard_stop_symbols,
        "errors": fetch_errors,
    }

    if args.json:
        _print_json(payload)
        return 0

    summary = payload["summary"]
    print(f"Hard-stop diagnostics interval={args.interval} lookahead={args.lookahead_bars} bars")
    print(
        f"hard_stops={summary['hard_stops']} diagnosed={summary['diagnosed']} "
        f"unknown={summary['unknown']} stop_too_early={summary['stop_too_early']} "
        f"helped_or_neutral={summary['stop_helped_or_neutral']}"
    )
    print(
        f"actual={_fmt_number(summary['actual_pnl'])} "
        f"hold={_fmt_number(summary['hold_to_lookahead_pnl'])} "
        f"delta={_fmt_number(summary['hold_to_lookahead_delta_vs_actual'])} "
        f"best={_fmt_number(summary['best_after_stop_pnl'])} "
        f"worst={_fmt_number(summary['worst_after_stop_pnl'])}"
    )
    if fetch_errors:
        print("fetch_errors:", ", ".join(f"{symbol}={error['message']}" for symbol, error in fetch_errors.items()))
    for case in payload["cases"][: args.limit]:
        print(
            f"{case.get('exit_ts')} {case.get('symbol')} {case.get('side')} "
            f"actual={_fmt_number(case.get('actual_pnl'))} "
            f"hold={_fmt_number(case.get('hold_to_lookahead_pnl'))} "
            f"best={_fmt_number(case.get('best_after_stop_pnl'))} "
            f"worst={_fmt_number(case.get('worst_after_stop_pnl'))} "
            f"recovered={case.get('recovered_to_entry')} verdict={case.get('verdict')}"
        )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from trader.reporting.read_models.world_dynamics_status import read_world_dynamics_status

    payload = {
        "daemon_status": _read_state_json("daemon_status.json"),
        "current_report": _read_state_json("current_report.json"),
        "last_report": _read_state_json("last_report.json"),
        "broker": _read_broker_state(),
        "scheduler": _read_scheduler_state(),
        "trade_plans": _read_trade_plans_state(),
        "world_dynamics": read_world_dynamics_status(daemon.STATE_DIR),
    }
    if args.json:
        _print_json(payload)
        return 0

    status = payload["daemon_status"] or {}
    broker = payload["broker"] or {}
    positions = broker.get("positions", {}) if isinstance(broker, dict) else {}
    print(f"phase: {status.get('phase', 'unknown')}")
    print(f"current_symbol: {status.get('current_symbol')}")
    print(f"progress: {status.get('decisions_done', 0)}/{status.get('symbols_total', 0)}")
    calls_used = status.get("model_calls_used", 0)
    calls_limit = status.get("max_model_calls_per_cycle")
    calls_text = f"{calls_used}/{calls_limit}" if calls_limit is not None else str(calls_used)
    print(f"model_calls: {calls_text}")
    print(f"cash: {broker.get('cash') if isinstance(broker, dict) else None}")
    print("positions:", ", ".join(positions.keys()) if positions else "none")
    dynamics = payload["world_dynamics"]
    print(f"world_dynamics: {dynamics.get('status')} shadow_only reason={dynamics.get('reason')}")
    print(f"world_dynamics_status: {dynamics['report_command']}")
    return 0


def _cmd_world_status(args: argparse.Namespace) -> int:
    from trader.interfaces.cli.world_model import read_world_graph_status, read_world_model_status

    world = read_world_model_status(daemon.STATE_DIR)
    payload = {**world, "graph": read_world_graph_status(daemon.STATE_DIR, world_status=world)}
    if args.json:
        _print_json(payload)
    else:
        counts = payload.get("counts", {})
        evaluation = payload.get("evaluation", {})
        print(
            f"world_model: {payload.get('status')} authority={payload.get('authority')} "
            f"episodes={counts.get('episodes', 0)} labels={counts.get('outcome_events', 0)} "
            f"predictions={counts.get('predictions', 0)} matched={evaluation.get('matched', 0)}"
        )
        dynamics = payload.get("dynamics", {})
        print(f"world_dynamics: {dynamics.get('status', 'not_started')} shadow_only "
              f"reason={dynamics.get('reason', 'status_not_written')}")
        print("world_dynamics_status: casys-trader world dynamics status --json")
    return 0 if payload.get("status") not in {"unavailable", "schema_unavailable"} else 1


def _cmd_world_cohort(args: argparse.Namespace) -> int:
    from trader.interfaces.cli.world_model import dispatch_world_cohort

    payload, code = dispatch_world_cohort(args, state_dir=daemon.STATE_DIR)
    _print_json(payload)
    return code


def _cmd_world_macro(args: argparse.Namespace) -> int:
    from trader.interfaces.cli.world_model import dispatch_world_macro

    payload, code = dispatch_world_macro(args, state_dir=daemon.STATE_DIR)
    _print_json(payload)
    return code


def _cmd_world_graph(args: argparse.Namespace) -> int:
    from trader.interfaces.cli.world_model import dispatch_world_graph

    payload, code = dispatch_world_graph(args, state_dir=daemon.STATE_DIR)
    _print_json(payload)
    return code


def _cmd_world_scope(args: argparse.Namespace) -> int:
    from trader.interfaces.cli.world_model import dispatch_world_scope

    payload, code = dispatch_world_scope(args, config_dir=daemon.ROOT / "config")
    _print_json(payload)
    return code


def _cmd_world_pattern(args: argparse.Namespace) -> int:
    from trader.interfaces.cli.world_model import dispatch_world_pattern

    payload, code = dispatch_world_pattern(args, state_dir=daemon.STATE_DIR)
    _print_json(payload)
    return code


def _cmd_world_dynamics(args: argparse.Namespace) -> int:
    from trader.interfaces.cli.world_dynamics import dispatch_world_dynamics

    payload, code = dispatch_world_dynamics(args, state_dir=daemon.STATE_DIR)
    _print_json(payload)
    return code


def _dashboard_url(path: object) -> str:
    return f"http://127.0.0.1:8137/{getattr(path, 'name', path)}"


def _dashboard_paths_payload() -> dict[str, dict[str, str]]:
    return {
        "index": {
            "path": "state/dashboards.html",
            "url": "http://127.0.0.1:8137/dashboards.html",
            "command": "casys-trader dashboards all",
        },
        "portfolio_timeline": {
            "path": "state/portfolio_timeline.html",
            "url": "http://127.0.0.1:8137/portfolio_timeline.html",
            "command": "casys-trader dashboards portfolio",
        },
        "portfolio_allocation": {
            "path": "state/allocation_dashboard.html",
            "url": "http://127.0.0.1:8137/allocation_dashboard.html",
            "command": "casys-trader dashboards portfolio",
        },
        "decisions": {
            "path": "state/decisions_dashboard.html",
            "url": "http://127.0.0.1:8137/decisions_dashboard.html",
            "command": "casys-trader dashboards decisions",
        },
        "decision_charts": {
            "path": "state/charts/01_allocation_familles.png",
            "url": "http://127.0.0.1:8137/charts/01_allocation_familles.png",
            "command": "casys-trader dashboards decisions",
        },
    }


def _print_dashboard_paths(paths: dict[str, dict[str, str]]) -> None:
    print("Dashboards locaux")
    for name, info in paths.items():
        print(f"- {name}: {info['path']}")
        print(f"  {info['url']}")
        print(f"  regen: {info['command']}")


def _cmd_dashboards_list(args: argparse.Namespace) -> int:
    payload = _dashboard_paths_payload()
    if args.json:
        _print_json(payload)
    else:
        _print_dashboard_paths(payload)
    return 0


def _render_dashboard_index() -> object:
    from trader.interfaces.dashboards import index

    return index.render_index()


def _cmd_dashboards_portfolio(args: argparse.Namespace) -> int:
    from trader.interfaces.dashboards import portfolio_allocation, portfolio_timeline

    allocation = portfolio_allocation.build_and_render()
    timeline = portfolio_timeline.build_and_render()
    index_result = _render_dashboard_index()
    if args.json:
        _print_json(
            {
                "index": str(index_result.html_path),
                "allocation": {"html": str(allocation.html_path), "png": str(allocation.png_path)},
                "timeline": {"html": str(timeline.html_path), "png": str(timeline.png_path)},
            }
        )
    else:
        print(f"OK — portefeuille régénéré ({allocation.positions_count} positions, {timeline.fills_count} fills)")
        print(f"  Index    : {index_result.html_path}  ({_dashboard_url(index_result.html_path)})")
        print(f"  Snapshot : {allocation.html_path}  ({_dashboard_url(allocation.html_path)})")
        print(f"  Temps    : {timeline.html_path}  ({_dashboard_url(timeline.html_path)})")
    return 0


def _cmd_dashboards_decisions(args: argparse.Namespace) -> int:
    from trader.interfaces.dashboards import decision_charts, decision_dashboard

    dashboard = decision_dashboard.build_and_render()
    charts = decision_charts.build_and_render()
    index_result = _render_dashboard_index()
    if args.json:
        _print_json(
            {
                "index": str(index_result.html_path),
                "decisions": {"html": str(dashboard.html_path), "rows": dashboard.rows_count},
                "charts": [str(path) for path in charts.paths],
            }
        )
    else:
        print(f"OK — décisions régénérées ({dashboard.rows_count} décisions)")
        print(f"  Index      : {index_result.html_path}  ({_dashboard_url(index_result.html_path)})")
        print(f"  Interactif : {dashboard.html_path}  ({_dashboard_url(dashboard.html_path)})")
        for path in charts.paths:
            print(f"  Chart      : {path}")
    return 0


def _cmd_dashboards_all(args: argparse.Namespace) -> int:
    from trader.interfaces.dashboards import (
        decision_charts,
        decision_dashboard,
        portfolio_allocation,
        portfolio_timeline,
    )

    allocation = portfolio_allocation.build_and_render()
    timeline = portfolio_timeline.build_and_render()
    dashboard = decision_dashboard.build_and_render()
    charts = decision_charts.build_and_render()
    index_result = _render_dashboard_index()
    if args.json:
        _print_json(
            {
                "index": str(index_result.html_path),
                "allocation": {"html": str(allocation.html_path), "png": str(allocation.png_path)},
                "timeline": {"html": str(timeline.html_path), "png": str(timeline.png_path)},
                "decisions": {"html": str(dashboard.html_path), "rows": dashboard.rows_count},
                "charts": [str(path) for path in charts.paths],
            }
        )
    else:
        print("OK — tous les dashboards régénérés")
        print(f"  Index      : {index_result.html_path}  ({_dashboard_url(index_result.html_path)})")
        print(f"  Snapshot   : {allocation.html_path}")
        print(f"  Temps      : {timeline.html_path}")
        print(f"  Décisions  : {dashboard.html_path}")
        print(f"  Charts PNG : {', '.join(str(path) for path in charts.paths)}")
    return 0


def _cmd_decisions_funnel(args: argparse.Namespace) -> int:
    from trader.reporting.read_models.strategy_funnel import (
        StrategyFunnelSourceError,
        load_strategy_funnel,
        render_strategy_funnel,
    )

    since = None
    if args.since:
        since = datetime.fromisoformat(str(args.since).replace("Z", "+00:00"))
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
    try:
        funnel = load_strategy_funnel(
            daemon.STATE_DIR,
            since=since,
            hours=None if since is not None else args.hours,
        )
    except StrategyFunnelSourceError as exc:
        print(f"strategy funnel unavailable: {exc}")
        return 2
    if args.json:
        _print_json(funnel.as_dict() | {"since": funnel.since, "until": funnel.until})
        return 0
    print(render_strategy_funnel(funnel))
    return 0


def _cmd_decisions_list(args: argparse.Namespace) -> int:
    store = decision_ledger.DecisionLedgerStore(daemon.STATE_DIR / decision_ledger.DEFAULT_LEDGER_FILENAME)
    rows = store.read_all(symbol=args.symbol, limit=args.limit)
    if args.json:
        _print_json(rows)
    else:
        for row in rows:
            print(
                f"{row.get('cycle_ts')} {row.get('symbol')} "
                f"{row.get('action')} intent={row.get('intent')} "
                f"reason={row.get('reason')} price={row.get('price')}"
            )
    return 0


def _cmd_decisions_flags(args: argparse.Namespace) -> int:
    store = decision_ledger.DecisionLedgerStore(daemon.STATE_DIR / decision_ledger.DEFAULT_LEDGER_FILENAME)
    rows = store.read_all(symbol=args.symbol)
    flags = decision_flags.collect_flags(
        rows,
        symbol=args.symbol,
        code=args.code,
        limit=args.limit,
    )
    if args.json:
        _print_json(flags)
        return 0

    for flag in flags:
        context = flag.get("context")
        context_text = f" context={context}" if context is not None else ""
        print(
            f"{flag.get('cycle_ts')} {flag.get('symbol')} {flag.get('tool')} "
            f"outcome={flag.get('outcome')} code={flag.get('code')} "
            f"field={flag.get('field')} executed={flag.get('executed')} "
            f"reason={flag.get('reason')}{context_text}"
        )
    return 0


def _cmd_decisions_seed_existing(args: argparse.Namespace) -> int:
    result = decision_ledger.seed_existing_reports(daemon.STATE_DIR)
    if args.json:
        _print_json(result)
    else:
        print(
            "seed-existing: "
            f"reports={result['reports']} candidates={result['candidates']} "
            f"appended={result['appended']} skipped={result['skipped']}"
        )
    return 0


def _cmd_decisions_seed_events(args: argparse.Namespace) -> int:
    event_paths = [daemon.STATE_DIR / "events.jsonl"]
    if args.include_archives:
        event_paths.extend(sorted(daemon.ROOT.glob("state_archive_*/events.jsonl")))
    result = decision_ledger.seed_existing_events(daemon.STATE_DIR, event_paths=event_paths)
    if args.json:
        _print_json(result)
    else:
        print(
            "seed-events: "
            f"event_files={result['event_files']} candidates={result['candidates']} "
            f"appended={result['appended']} skipped={result['skipped']}"
        )
    return 0


def _refresh_decision_audit_from_ledger() -> dict:
    audit_path = daemon.STATE_DIR / "decision_audit.json"
    if not audit_path.exists():
        return {"audit_updated": False}
    try:
        payload = json.loads(audit_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"audit_updated": False, "audit_error": "invalid_json"}
    if not isinstance(payload, dict):
        return {"audit_updated": False, "audit_error": "invalid_payload"}
    ledger_path = daemon.STATE_DIR / decision_ledger.DEFAULT_LEDGER_FILENAME
    archive_dir = daemon.STATE_DIR / "archive"
    ledger_rows = list(ledger_rotation.read_rows_with_archive(ledger_path, archive_dir))
    refreshed = decision_audit.refresh_audit_payload(
        payload,
        ledger_rows=ledger_rows,
        audit_code_version=code_version.current_code_version(daemon.ROOT),
        include_missing_ledger_rows=True,
    )
    audit_path.write_text(json.dumps(refreshed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"audit_updated": True, "audit_rows": len(refreshed.get("rows", []))}


def _cmd_decisions_backfill_code_version(args: argparse.Namespace) -> int:
    result = decision_ledger.backfill_code_versions(
        daemon.STATE_DIR,
        repo_root=daemon.ROOT,
        overwrite=args.overwrite,
        ref=args.ref,
    )
    result.update(_refresh_decision_audit_from_ledger())
    if args.json:
        _print_json(result)
    else:
        print(
            "backfill-code-version: "
            f"rows={result['rows']} updated={result['updated']} "
            f"skipped={result['skipped']} unresolved={result['unresolved']} "
            f"audit_updated={result.get('audit_updated')}"
        )
    return 0


def _optional_pct(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct_text(value: object) -> str:
    parsed = _optional_pct(value)
    if parsed is None:
        return "n/a"
    return f"{parsed:.2f}".rstrip("0").rstrip(".")


def _load_decision_audit() -> dict:
    audit_path = daemon.STATE_DIR / "decision_audit.json"
    if not audit_path.exists():
        raise SystemExit("decision audit missing: run `casys-trader decisions audit` first")
    try:
        payload = json.loads(audit_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid decision audit: {audit_path}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"invalid decision audit: {audit_path}")
    if payload.get("benchmark_semantics_version") != decision_audit.BENCHMARK_SEMANTICS_VERSION:
        payload = decision_audit.refresh_audit_payload(payload)
    return payload


def _short_date(raw: object) -> str | None:
    if not raw:
        return None
    text = str(raw)
    if len(text) >= 10:
        return text[:10]
    return text


def _commit_dates_from_rows(rows: object) -> dict[str, str]:
    dates: dict[str, str] = {}
    if not isinstance(rows, list):
        return dates
    for row in rows:
        if not isinstance(row, dict):
            continue
        commit_key = row.get("decision_commit_key")
        version = row.get("code_version")
        if not commit_key or not isinstance(version, dict):
            continue
        raw_date = version.get("git_commit_date")
        inference = version.get("inference")
        if raw_date is None and isinstance(inference, dict):
            raw_date = inference.get("commit_date")
        date = _short_date(raw_date)
        if date:
            dates.setdefault(str(commit_key), date)
    return dates


def _decision_stats_payload(audit: dict, *, horizon: str, min_known: int) -> dict:
    global_metrics = _as_metrics_dict(audit.get("metrics"), horizon)
    by_commit = _as_metrics_dict(audit.get("metrics_by_commit"), horizon)
    dates_by_commit = _commit_dates_from_rows(audit.get("rows"))
    commits = []
    for commit, metrics in by_commit.items():
        if not isinstance(metrics, dict):
            continue
        known = int(metrics.get("known") or 0)
        if known < min_known:
            continue
        commits.append(
            {
                "commit": str(commit),
                "date": dates_by_commit.get(str(commit)),
                "total": int(metrics.get("total") or 0),
                "known": known,
                "unknown": int(metrics.get("unknown") or 0),
                "good": int(metrics.get("good") or 0),
                "bad": int(metrics.get("bad") or 0),
                "neutral": int(metrics.get("neutral") or 0),
                "missed": int(metrics.get("missed") or 0),
                "good_pct": _optional_pct(metrics.get("good_known_pct")),
                "nonbad_pct": _optional_pct(metrics.get("nonbad_known_pct")),
                "bad_pct": _optional_pct(metrics.get("bad_known_pct")),
                "missed_pct": _optional_pct(metrics.get("missed_known_pct")),
            }
        )
    commits.sort(key=lambda row: (row["known"], row["total"], row["commit"]), reverse=True)
    return {
        "horizon": horizon,
        "min_known": min_known,
        "global": global_metrics,
        "commits": commits,
    }


def _as_metrics_dict(payload: object, horizon: str) -> dict:
    if not isinstance(payload, dict):
        raise SystemExit(f"decision audit has no metrics for horizon {horizon}")
    metrics = payload.get(horizon)
    if not isinstance(metrics, dict):
        raise SystemExit(f"decision audit has no metrics for horizon {horizon}")
    return metrics


def _cmd_decisions_stats(args: argparse.Namespace) -> int:
    payload = _decision_stats_payload(
        _load_decision_audit(),
        horizon=args.horizon,
        min_known=args.min_known,
    )
    if args.json:
        _print_json(payload)
        return 0

    global_metrics = payload["global"]
    print(
        f"horizon={payload['horizon']} "
        f"global total={global_metrics.get('total')} known={global_metrics.get('known')} "
        f"unknown={global_metrics.get('unknown')} "
        f"good%={_pct_text(global_metrics.get('good_known_pct'))} "
        f"nonbad%={_pct_text(global_metrics.get('nonbad_known_pct'))} "
        f"bad%={_pct_text(global_metrics.get('bad_known_pct'))} "
        f"missed%={_pct_text(global_metrics.get('missed_known_pct'))}"
    )
    print("commit           date       total known unknown good% nonbad% bad% missed% good bad neutral missed")
    for row in payload["commits"]:
        print(
            f"{row['commit']:<16} "
            f"{row.get('date') or 'n/a':<10} "
            f"{row['total']:>5} {row['known']:>5} {row['unknown']:>7} "
            f"{_pct_text(row['good_pct']):>5} {_pct_text(row['nonbad_pct']):>7} {_pct_text(row['bad_pct']):>5} "
            f"{_pct_text(row['missed_pct']):>7} "
            f"{row['good']:>4} {row['bad']:>3} {row['neutral']:>7} {row['missed']:>6}"
        )
    return 0


def _parse_csv_arg(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _unique_symbol_list(symbols: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = str(raw or "").strip()
        if symbol and symbol not in seen:
            out.append(symbol)
            seen.add(symbol)
    return out


def _bench_context_symbols(raw: str | None, cases: list[dict]) -> list[str]:
    value = (raw or "universe").strip()
    case_symbols = [str(case.get("symbol")) for case in cases if case.get("symbol")]
    if value == "universe":
        return _unique_symbol_list([*_load_universe_symbols(), *case_symbols])
    if value == "case":
        return sorted(set(case_symbols))
    return _unique_symbol_list([*_parse_csv_arg(value), *case_symbols])


def _cmd_decisions_audit(args: argparse.Namespace) -> int:
    store = decision_ledger.DecisionLedgerStore(daemon.STATE_DIR / decision_ledger.DEFAULT_LEDGER_FILENAME)
    rows = store.read_all(symbol=args.symbol, limit=args.limit)
    horizons = _parse_csv_arg(args.horizons)
    prices = decision_audit.load_prices_for_audit(
        rows,
        horizons=horizons,
        interval=args.interval,
        load_prices=decision_audit.load_prices_yfinance,
    )

    result = decision_audit.audit_rows(
        rows,
        prices,
        horizons=horizons,
        threshold_pct=args.threshold_pct,
        audit_code_version=code_version.current_code_version(daemon.ROOT),
    )
    daemon.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (daemon.STATE_DIR / "decision_audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    if args.json:
        _print_json(result)
    else:
        print("Summary:")
        print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
        print("Metrics:")
        print(json.dumps(result["metrics"], ensure_ascii=False, indent=2))
        print("Metrics by decision commit:")
        print(json.dumps(result["metrics_by_commit"], ensure_ascii=False, indent=2))
        print(f"Rapport complet: {daemon.STATE_DIR / 'decision_audit.json'}")
    return 0


def _cmd_decisions_bench(args: argparse.Namespace) -> int:
    audit = _load_decision_audit()
    try:
        models = decision_bench.parse_model_specs(args.models)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    verdicts = decision_bench.parse_verdicts(args.verdicts)
    try:
        actions = decision_bench.parse_actions(args.actions)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    include_original = not args.hide_original
    context_kwargs = {}
    if args.reconstruct_context:
        preview_cases = decision_bench.select_cases(
            audit,
            horizon=args.horizon,
            limit=args.limit,
            offset=args.offset,
            verdicts=verdicts,
            symbol=args.symbol,
            include_original=include_original,
            actions=actions,
        )
        context_symbols = _bench_context_symbols(args.context_symbols, preview_cases)
        context_history, context_metadata = decision_bench.load_reconstruction_history(
            preview_cases,
            symbols=context_symbols,
            interval=args.context_interval,
            padding_days=args.context_padding_days,
        )
        fx_history, fx_metadata = decision_bench.load_fx_history(
            preview_cases,
            padding_days=args.context_padding_days,
        )
        context_kwargs = {
            "context_history": context_history,
            "context_symbols": context_symbols,
            "context_interval": args.context_interval,
            "context_lookback_bars": args.context_lookback_bars,
            "cockpit_window": args.cockpit_window,
            "context_metadata": context_metadata,
            "risk_limits": decision_bench.AUDIT_WINDOW_RISK_LIMITS,
            "fx_history": fx_history,
            "fx_metadata": fx_metadata,
        }

    try:
        contract = decision_bench.parse_contract(args.contract)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    doctrine = None
    if contract == "production" and not args.no_doctrine:
        try:
            doctrine = decision_bench.load_planner_doctrine(daemon.ROOT)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    if args.dry_run:
        payload = decision_bench.dry_run_payload(
            audit,
            models=models,
            horizon=args.horizon,
            limit=args.limit,
            offset=args.offset,
            verdicts=verdicts,
            symbol=args.symbol,
            include_original=include_original,
            actions=actions,
            contract=contract,
            batch_size=args.batch_size,
            doctrine=doctrine,
            **context_kwargs,
        )
    else:
        payload = decision_bench.run_bench(
            audit,
            models=models,
            horizon=args.horizon,
            limit=args.limit,
            offset=args.offset,
            verdicts=verdicts,
            timeout_s=args.timeout_s,
            symbol=args.symbol,
            include_original=include_original,
            actions=actions,
            contract=contract,
            batch_size=args.batch_size,
            doctrine=doctrine,
            **context_kwargs,
        )

    output_path = daemon.STATE_DIR / args.output
    daemon.STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload["output_path"] = str(output_path)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.json:
        _print_json(payload)
    else:
        if payload.get("audit_stale"):
            print(
                "Attention: decision_audit.json est périmé "
                f"(as_of={payload.get('audit_as_of')}, > {decision_bench.AUDIT_STALE_AFTER_DAYS} jours). "
                "Rafraîchir avec `casys-trader decisions audit` — ne pas régénérer à la main le fichier."
            )
        if args.dry_run:
            print(
                f"Decision bench dry-run horizon={payload['horizon']} "
                f"cases={len(payload['cases'])} models={len(payload['models'])}"
            )
            print(f"Prompt chars: {len(payload['prompt'])}")
            print(f"Rapport complet: {output_path}")
        else:
            print(decision_bench.render_summary(payload))
            print(f"Rapport complet: {output_path}")
    return 0


def _cmd_semantic_describe(args: argparse.Namespace) -> int:
    payload = describe_semantic_layer()
    if args.json:
        _print_json(payload)
    else:
        print("Semantic layer:", ", ".join(payload["levels"]))
    return 0


def _cmd_indicators_list(args: argparse.Namespace) -> int:
    payload = {"indicators": list_indicators()}
    if args.concept:
        payload = {"indicators": find_indicators(args.concept)}
    if args.json:
        _print_json(payload)
    else:
        for indicator in payload["indicators"]:
            print(f"{indicator['name']}: {indicator['description']}")
    return 0


def _cmd_indicators_get(args: argparse.Namespace) -> int:
    names = _parse_csv(args.names, DEFAULT_INDICATORS)
    temporal = normalize_temporal_query(
        timeframe=args.timeframe,
        lookback=args.lookback,
        window=args.window,
        as_of=args.as_of,
    )
    bars = market.get_bars(args.symbol, lookback=temporal["lookback"], interval=temporal["timeframe"])
    payload = {
        "symbol": args.symbol,
        **temporal,
        "indicators": compute_indicator_values(bars, names=names, window=temporal["window"]),
    }
    if args.json:
        _print_json(payload)
    else:
        for name, value in payload["indicators"].items():
            print(f"{name}: {value}")
    return 0


def _cmd_indicators_compare(args: argparse.Namespace) -> int:
    symbols = FAMILIES.get(args.family)
    if not symbols:
        raise SystemExit(f"unknown family: {args.family}")
    names = _parse_csv(args.metrics, DEFAULT_INDICATORS)
    temporal = normalize_temporal_query(
        timeframe=args.timeframe,
        lookback=args.lookback,
        window=args.window,
        as_of=args.as_of,
    )
    bars_by_symbol = {
        symbol: market.get_bars(symbol, lookback=temporal["lookback"], interval=temporal["timeframe"])
        for symbol in symbols
    }
    payload = {
        "family": args.family,
        **temporal,
        "symbols": build_indicator_snapshot(bars_by_symbol, symbols=symbols, names=names, window=temporal["window"]),
    }
    if args.json:
        _print_json(payload)
    else:
        for symbol, row in payload["symbols"].items():
            print(f"{symbol}: {row['indicators']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="casys-trader")
    sub = parser.add_subparsers(dest="command")

    daemon_parser = sub.add_parser("daemon", help="lance la boucle runtime")
    daemon_parser.add_argument("--live", action="store_true", help="exécute réellement les ordres")
    daemon_parser.add_argument("--once", action="store_true", help="un seul cycle puis sortie")
    daemon_parser.add_argument("--poll", type=float, default=30.0)
    daemon_parser.add_argument("--default-wake-minutes", type=float, default=30.0)
    daemon_parser.add_argument("--min-wake-minutes", type=float, default=5.0)
    daemon_parser.add_argument("--max-wake-minutes", type=float, default=240.0)
    daemon_parser.add_argument("--max-context-requests-per-symbol", type=int, default=2)
    daemon_parser.add_argument(
        "--max-indicators-per-request",
        type=int,
        default=len(DEFAULT_INDICATORS),
        help="cap opérateur optionnel ; par défaut tout le catalogue d'indicateurs est retourné",
    )
    daemon_parser.add_argument(
        "--max-model-calls-per-cycle",
        type=int,
        default=25,
        help="cap du mode batch legacy uniquement ; ignoré par la queue/free-iteration (no call cap)",
    )
    daemon_parser.add_argument(
        "--learning-consolidation-threshold",
        type=int,
        default=daemon.DEFAULT_LEARNING_CONSOLIDATION_THRESHOLD,
    )
    daemon_parser.add_argument("--consolidator-acpx-bin", default="acpx")
    daemon_parser.add_argument("--consolidator-acpx-agent", default=daemon.consolidator.DEFAULT_CONSOLIDATOR_ACPX_AGENT)
    daemon_parser.add_argument("--consolidator-model", default=daemon.consolidator.DEFAULT_CONSOLIDATOR_MODEL)
    daemon_parser.add_argument(
        "--consolidator-timeout-s",
        type=int,
        default=daemon.consolidator.DEFAULT_CONSOLIDATOR_TIMEOUT_S,
    )
    daemon_parser.add_argument("--bootstrap-all", action="store_true")

    status = sub.add_parser("status", help="état courant du daemon")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=_cmd_status)

    world = sub.add_parser("world", help="world model de marché shadow")
    world_sub = world.add_subparsers(dest="world_command", required=True)
    world_status = world_sub.add_parser("status", help="couverture et métriques préquentielles")
    world_status.add_argument("--json", action="store_true")
    world_status.set_defaults(func=_cmd_world_status)

    from trader.application.world_model.capture import SAMPLING_POLICY_VERSION
    from trader.domain.world_episode import MARKET_FEATURE_CONTRACT_ID

    dynamics = world_sub.add_parser("dynamics", help="statut du worker automatique ou replay OHLCV borné")
    dynamics.add_argument("dynamics_command", nargs="?", choices=("status",),
                          help="status : dernier rapport du worker automatique, sans entraînement")
    dynamics.add_argument("--venue")
    dynamics.add_argument("--symbol")
    dynamics.add_argument("--interval", default="1h")
    dynamics.add_argument("--market-contract-version", default=MARKET_FEATURE_CONTRACT_ID)
    dynamics.add_argument("--sampling-policy-version", default=SAMPLING_POLICY_VERSION)
    dynamics.add_argument("--start", help="début inclusif des ancres ; horodatage UTC explicite ; requis pour replay")
    dynamics.add_argument("--as-of", help="cutoff causal ; horodatage UTC explicite ; requis pour replay")
    dynamics.add_argument("--limit", type=int, default=5000, help="borne des épisodes ; maximum 20000")
    dynamics.add_argument("--steps", type=int, default=4, help="barres futures simulées ; maximum 12")
    dynamics.add_argument("--paths", type=int, default=200, help="trajectoires simulées ; maximum 1000")
    dynamics.add_argument("--min-support", type=int, default=40)
    dynamics.add_argument("--seed", type=int, default=0)
    dynamics.add_argument("--json", action="store_true")
    dynamics.set_defaults(func=_cmd_world_dynamics)

    from trader.interfaces.cli.world_model import WORLD_COHORT_INVALIDATION_REASONS

    cohort = world_sub.add_parser("cohort", help="cohorte prospective shadow")
    cohort_sub = cohort.add_subparsers(dest="cohort_command", required=True)

    cohort_validate = cohort_sub.add_parser("validate", help="valide un manifeste sans écrire")
    cohort_validate.add_argument("--manifest", required=True)
    cohort_validate.add_argument("--json", action="store_true")
    cohort_validate.set_defaults(func=_cmd_world_cohort)

    cohort_register = cohort_sub.add_parser("register", help="enregistre un manifeste durable")
    cohort_register.add_argument("--manifest", required=True)
    cohort_register.add_argument("--json", action="store_true")
    cohort_register.set_defaults(func=_cmd_world_cohort)

    cohort_arm = cohort_sub.add_parser("arm", help="arme une cohorte enregistrée")
    cohort_arm.add_argument("cohort_id")
    cohort_arm.add_argument("--sensor", action="append")
    cohort_arm.add_argument("--json", action="store_true")
    cohort_arm.set_defaults(func=_cmd_world_cohort)

    cohort_start = cohort_sub.add_parser("start", help="démarre la collecte après armement")
    cohort_start.add_argument("cohort_id")
    cohort_start.add_argument("--json", action="store_true")
    cohort_start.set_defaults(func=_cmd_world_cohort)

    cohort_status = cohort_sub.add_parser("status", help="cycle de vie d'une cohorte")
    cohort_status.add_argument("cohort_id")
    cohort_status.add_argument("--json", action="store_true")
    cohort_status.set_defaults(func=_cmd_world_cohort)

    cohort_report = cohort_sub.add_parser("report", help="rapport reconstructible")
    cohort_report.add_argument("cohort_id")
    cohort_report.add_argument("--json", action="store_true")
    cohort_report.set_defaults(func=_cmd_world_cohort)

    cohort_close = cohort_sub.add_parser("close", help="ferme la collecte")
    cohort_close.add_argument("cohort_id")
    cohort_close.add_argument("--reason", required=True)
    cohort_close.add_argument("--json", action="store_true")
    cohort_close.set_defaults(func=_cmd_world_cohort)

    cohort_invalidate = cohort_sub.add_parser("invalidate", help="invalide le protocole")
    cohort_invalidate.add_argument("cohort_id")
    cohort_invalidate.add_argument("--reason", required=True, choices=WORLD_COHORT_INVALIDATION_REASONS)
    cohort_invalidate.add_argument("--scope")
    cohort_invalidate.add_argument("--proof", action="append")
    cohort_invalidate.add_argument("--occurred-at")
    cohort_invalidate.add_argument("--json", action="store_true")
    cohort_invalidate.set_defaults(func=_cmd_world_cohort)

    macro = world_sub.add_parser("macro", help="collecte macro source-only shadow")
    macro_sub = macro.add_subparsers(dest="macro_command", required=True)
    macro_status = macro_sub.add_parser("status", help="couverture et fraîcheur source-only")
    macro_status.add_argument("--json", action="store_true")
    macro_status.set_defaults(func=_cmd_world_macro)

    graph = world_sub.add_parser("graph", help="graphe shadow")
    graph_sub = graph.add_subparsers(dest="graph_command", required=True)
    graph_status = graph_sub.add_parser("status", help="budgets et gaps graphe")
    graph_status.add_argument("--json", action="store_true")
    graph_status.set_defaults(func=_cmd_world_graph)
    graph_report = graph_sub.add_parser("report", help="rapport graphe sans claim causal")
    graph_report.add_argument("cohort_id", nargs="?")
    graph_report.add_argument("--json", action="store_true")
    graph_report.set_defaults(func=_cmd_world_graph)

    pattern = world_sub.add_parser("pattern", help="hypothèses de patterns graphe shadow")
    pattern_sub = pattern.add_subparsers(dest="pattern_command", required=True)
    pattern_discover = pattern_sub.add_parser(
        "discover",
        help="découverte as-of ; dry-run par défaut, --apply enregistre et démarre",
    )
    pattern_discover.add_argument("--formation-cutoff", required=True)
    pattern_discover.add_argument("--evaluation-start-not-before", required=True)
    pattern_discover.add_argument("--ontology-revision", help="tampon de révision apposé aux hypothèses formées")
    pattern_discover.add_argument("--horizon", action="append")
    pattern_discover.add_argument("--min-support", type=int, default=20)
    pattern_discover.add_argument("--min-association", type=float, default=0.10)
    pattern_discover.add_argument("--max-candidates", type=int, default=20)
    pattern_discover.add_argument("--smoothing-alpha", type=float, default=1.0)
    pattern_discover.add_argument("--hypothesis-id", action="append")
    pattern_discover.add_argument(
        "--apply",
        action="store_true",
        help="enregistre et démarre les candidats affichés (défaut: dry-run)",
    )
    pattern_discover.add_argument("--evaluation-cohort-id")
    pattern_discover.add_argument("--evaluation-dataset-fingerprint")
    pattern_discover.add_argument(
        "--include-source-evidence",
        action="store_true",
        help="inclut la liste complète source_evidence_ids (défaut: compte + empreinte)",
    )
    pattern_discover.add_argument("--json", action="store_true")
    pattern_discover.set_defaults(func=_cmd_world_pattern)
    pattern_evaluate = pattern_sub.add_parser(
        "evaluate",
        help="matching prospectif unlabeled borné à la cohorte d'évaluation ; dry-run par défaut, --apply persiste prévisions et occurrences",
    )
    pattern_evaluate.add_argument("--as-of", required=True)
    pattern_evaluate.add_argument("--evaluation-cohort-id", required=True)
    pattern_evaluate.add_argument("--evaluation-dataset-fingerprint", required=True)
    pattern_evaluate.add_argument("--hypothesis-id", action="append")
    pattern_evaluate.add_argument(
        "--apply",
        action="store_true",
        help="persiste WorldPrediction shadow et PatternOccurrence (défaut: dry-run)",
    )
    pattern_evaluate.add_argument("--json", action="store_true")
    pattern_evaluate.set_defaults(func=_cmd_world_pattern)
    pattern_link = pattern_sub.add_parser(
        "link-outcomes",
        help="lie ou corrige les feuilles 4h/1d/3d déjà observées ; replay idempotent ; dry-run par défaut, --apply persiste",
    )
    pattern_link.add_argument("--as-of", required=True)
    pattern_link.add_argument("--evaluation-cohort-id")
    pattern_link.add_argument("--hypothesis-id", action="append")
    pattern_link.add_argument("--occurrence-id", action="append")
    pattern_link.add_argument(
        "--apply",
        action="store_true",
        help="persiste les PatternOutcomeLink disponibles (défaut: dry-run)",
    )
    pattern_link.add_argument("--json", action="store_true")
    pattern_link.set_defaults(func=_cmd_world_pattern)
    pattern_status = pattern_sub.add_parser("status", help="catalogue hypothèse-premier, y compris zéro occurrence")
    pattern_status.add_argument("cohort_id", nargs="?")
    pattern_status.add_argument("--json", action="store_true")
    pattern_status.set_defaults(func=_cmd_world_pattern)
    pattern_report = pattern_sub.add_parser("report", help="rapport reconstructible")
    pattern_report.add_argument("cohort_id")
    pattern_report.add_argument("--json", action="store_true")
    pattern_report.set_defaults(func=_cmd_world_pattern)

    scope = world_sub.add_parser("scope", help="mapping de scopes World shadow")
    scope_sub = scope.add_subparsers(dest="scope_command", required=True)
    scope_reconcile = scope_sub.add_parser("reconcile", help="reconcilie universe.yaml vers world_scope_mapping.v1")
    scope_reconcile.add_argument(
        "--apply",
        action="store_true",
        help="persiste une nouvelle generation de mapping (defaut: dry-run)",
    )
    scope_reconcile.add_argument("--json", action="store_true")
    scope_reconcile.set_defaults(func=_cmd_world_scope)

    news_macro = sub.add_parser("news-macro", help="briefs macro/news par marché")
    news_macro_sub = news_macro.add_subparsers(dest="news_macro_command", required=True)
    news_macro_refresh = news_macro_sub.add_parser("refresh", help="lance immédiatement un point macro/news")
    venue_group = news_macro_refresh.add_mutually_exclusive_group(required=True)
    venue_group.add_argument(
        "--venue",
        action="append",
        choices=(*news_macro_runtime.VENUES, "GLOBAL"),
        help="marché ciblé ; option répétable",
    )
    venue_group.add_argument("--all", action="store_true", help="cible TW, EU, US et GLOBAL")
    news_macro_refresh.add_argument(
        "--force",
        action="store_true",
        help="ignore fraîcheur, cooldown et backoff d'échec",
    )
    news_macro_refresh.set_defaults(func=_cmd_news_macro_refresh)

    universe = sub.add_parser("universe", help="composition régionale de la hotlist")
    universe_sub = universe.add_subparsers(dest="universe_command", required=True)
    universe_refresh = universe_sub.add_parser(
        "refresh",
        help="compose la hotlist du pack courant ; FORCE enchaîne le macro si le brief n'est pas le bon",
    )
    universe_venue = universe_refresh.add_mutually_exclusive_group(required=True)
    universe_venue.add_argument(
        "--venue",
        action="append",
        choices=universe_intelligence_runtime.VENUES,
        help="marché ciblé ; option répétable",
    )
    universe_venue.add_argument("--all", action="store_true", help="cible TW, EU et US")
    universe_refresh.add_argument(
        "--force",
        action="store_true",
        help="ignore fraîcheur, cooldown et backoff ; enchaîne le macro du pack courant si besoin",
    )
    universe_refresh.add_argument(
        "--no-macro",
        action="store_true",
        help="ne rafraîchit pas le brief ; un brief d'un autre pack laisse waiting_brief",
    )
    universe_refresh.set_defaults(func=_cmd_universe_refresh)

    company = sub.add_parser(
        "company-intelligence",
        help="recherche fondamentale longitudinale par symbole",
    )
    company_sub = company.add_subparsers(dest="company_intelligence_command", required=True)
    company_refresh = company_sub.add_parser("refresh", help="collecte et analyse le scope entreprise")
    company_refresh.add_argument("--scope", default="current", choices=("current", "active"))
    company_refresh.add_argument("--symbol", action="append", default=[])
    company_refresh.add_argument("--depth", default="screen", choices=("screen", "deep"))
    company_refresh.add_argument("--wait", action="store_true")
    company_refresh.add_argument("--wait-timeout-s", type=float, default=600.0)
    company_refresh.set_defaults(func=_cmd_company_intelligence_refresh)
    company_status = company_sub.add_parser("status", help="état de la file et derniers runs")
    company_status.set_defaults(func=_cmd_company_intelligence_status)

    dashboards = sub.add_parser("dashboards", help="dashboards HTML/PNG locaux")
    dashboards_sub = dashboards.add_subparsers(dest="dashboards_command", required=True)
    dashboards_list = dashboards_sub.add_parser("list", help="liste les dashboards et chemins locaux")
    dashboards_list.add_argument("--json", action="store_true")
    dashboards_list.set_defaults(func=_cmd_dashboards_list)

    dashboards_portfolio = dashboards_sub.add_parser("portfolio", help="régénère les dashboards portefeuille")
    dashboards_portfolio.add_argument("--json", action="store_true")
    dashboards_portfolio.set_defaults(func=_cmd_dashboards_portfolio)

    dashboards_decisions = dashboards_sub.add_parser("decisions", help="régénère les dashboards décisions")
    dashboards_decisions.add_argument("--json", action="store_true")
    dashboards_decisions.set_defaults(func=_cmd_dashboards_decisions)

    dashboards_all = dashboards_sub.add_parser("all", help="régénère tous les dashboards")
    dashboards_all.add_argument("--json", action="store_true")
    dashboards_all.set_defaults(func=_cmd_dashboards_all)

    diagnostics = sub.add_parser("diagnostics", help="diagnostics post-mortem")
    diagnostics_sub = diagnostics.add_subparsers(dest="diagnostics_command", required=True)
    hard_stops = diagnostics_sub.add_parser("hard-stops", help="diagnostique les sorties hard_stop")
    hard_stops.add_argument("--lookahead-bars", type=int, default=8)
    hard_stops.add_argument("--interval", default="1h")
    hard_stops.add_argument("--lookback", default="10d")
    hard_stops.add_argument("--since")
    hard_stops.add_argument("--exclude-symbol", action="append", default=[])
    hard_stops.add_argument("--all-regimes", action="store_true")
    hard_stops.add_argument("--limit", type=int, default=20)
    hard_stops.add_argument("--json", action="store_true")
    hard_stops.set_defaults(func=_cmd_diagnostics_hard_stops)

    decisions = sub.add_parser("decisions", help="journal des décisions agent")
    decisions_sub = decisions.add_subparsers(dest="decisions_command", required=True)
    decisions_funnel = decisions_sub.add_parser(
        "funnel",
        help="funnel LLM → watches → triggers → ordres → fills",
    )
    decisions_funnel.add_argument("--since", help="ISO 8601, borne inférieure")
    decisions_funnel.add_argument(
        "--hours",
        type=float,
        default=24.0,
        help="fenêtre glissante si --since est absent (défaut 24h)",
    )
    decisions_funnel.add_argument("--json", action="store_true")
    decisions_funnel.set_defaults(func=_cmd_decisions_funnel)

    decisions_list = decisions_sub.add_parser("list", help="liste les décisions persistées")
    decisions_list.add_argument("--symbol")
    decisions_list.add_argument("--limit", type=int)
    decisions_list.add_argument("--json", action="store_true")
    decisions_list.set_defaults(func=_cmd_decisions_list)

    decisions_flags = decisions_sub.add_parser("flags", help="liste les flags décisionnels persistés")
    decisions_flags.add_argument("--symbol")
    decisions_flags.add_argument("--code")
    decisions_flags.add_argument("--limit", type=int)
    decisions_flags.add_argument("--json", action="store_true")
    decisions_flags.set_defaults(func=_cmd_decisions_flags)

    decisions_seed = decisions_sub.add_parser("seed-existing", help="récupère les décisions des rapports existants")
    decisions_seed.add_argument("--json", action="store_true")
    decisions_seed.set_defaults(func=_cmd_decisions_seed_existing)

    decisions_seed_events = decisions_sub.add_parser("seed-events", help="récupère les décisions résumées des events")
    decisions_seed_events.add_argument("--include-archives", action="store_true")
    decisions_seed_events.add_argument("--json", action="store_true")
    decisions_seed_events.set_defaults(func=_cmd_decisions_seed_events)

    decisions_backfill_code = decisions_sub.add_parser(
        "backfill-code-version",
        help="associe les anciennes décisions au commit git historique le plus proche",
    )
    decisions_backfill_code.add_argument("--overwrite", action="store_true")
    decisions_backfill_code.add_argument("--ref", default="HEAD")
    decisions_backfill_code.add_argument("--json", action="store_true")
    decisions_backfill_code.set_defaults(func=_cmd_decisions_backfill_code_version)

    decisions_stats = decisions_sub.add_parser("stats", help="résume le dernier audit par commit")
    decisions_stats.add_argument("--horizon", default="1h")
    decisions_stats.add_argument("--min-known", type=int, default=0)
    decisions_stats.add_argument("--json", action="store_true")
    decisions_stats.set_defaults(func=_cmd_decisions_stats)

    decisions_audit = decisions_sub.add_parser("audit", help="évalue les décisions contre les prix futurs")
    decisions_audit.add_argument("--symbol")
    decisions_audit.add_argument("--limit", type=int)
    decisions_audit.add_argument("--horizons", default="1h,4h,1d")
    decisions_audit.add_argument("--threshold-pct", type=float, default=0.5)
    decisions_audit.add_argument("--interval", default="1h")
    decisions_audit.add_argument("--json", action="store_true")
    decisions_audit.set_defaults(func=_cmd_decisions_audit)

    decisions_bench = decisions_sub.add_parser(
        "bench",
        help="demande un avis contrefactuel à un petit banc de modèles",
    )
    decisions_bench.add_argument("--horizon", default="4h")
    decisions_bench.add_argument("--limit", type=int, default=10)
    decisions_bench.add_argument("--offset", type=int, default=0)
    decisions_bench.add_argument("--symbol")
    decisions_bench.add_argument(
        "--models",
        default=",".join(decision_bench.DEFAULT_BENCH_MODELS),
        help="CSV provider:model, ex. acpx:gpt-5.5,ollama-cloud:glm-5.1:cloud",
    )
    decisions_bench.add_argument("--verdicts", default="good,bad,missed,neutral")
    decisions_bench.add_argument(
        "--actions",
        default="",
        help="filtre d'actions originales CSV (ex. BUY,SELL) ; vide = toutes",
    )
    decisions_bench.add_argument("--timeout-s", type=int, default=120)
    decisions_bench.add_argument(
        "--contract",
        choices=("reviews", "production"),
        default="reviews",
        help="reviews = avis BUY/SELL/HOLD (défaut sûr); production = contrat live Pine-like",
    )
    decisions_bench.add_argument(
        "--batch-size",
        type=int,
        default=1,
        help="cas par appel modèle (défaut 1; 0 = un seul prompt pour tout le lot)",
    )
    decisions_bench.add_argument("--hide-original", action="store_true")
    decisions_bench.add_argument(
        "--no-doctrine",
        action="store_true",
        help="contrat production sans doctrine live (mandat/mémoire/guidance/vocabulaire) : reproduit les prompts historiques",
    )
    decisions_bench.add_argument("--reconstruct-context", action="store_true")
    decisions_bench.add_argument(
        "--context-symbols",
        default="universe",
        help="'universe', 'case' ou CSV de symboles pour le contexte reconstruit",
    )
    decisions_bench.add_argument("--context-interval", default=daemon.DEFAULT_RUNTIME_INTERVAL)
    decisions_bench.add_argument("--context-lookback-bars", type=int, default=160)
    decisions_bench.add_argument("--context-padding-days", type=int, default=10)
    decisions_bench.add_argument("--cockpit-window", type=int, default=48)
    decisions_bench.add_argument("--dry-run", action="store_true")
    decisions_bench.add_argument("--output", default="last_decision_bench.json")
    decisions_bench.add_argument("--json", action="store_true")
    decisions_bench.set_defaults(func=_cmd_decisions_bench)

    semantic = sub.add_parser("semantic", help="semantic layer")
    semantic_sub = semantic.add_subparsers(dest="semantic_command", required=True)
    describe = semantic_sub.add_parser("describe", help="décrit le catalogue")
    describe.add_argument("--json", action="store_true")
    describe.set_defaults(func=_cmd_semantic_describe)

    indicators = sub.add_parser("indicators", help="indicateurs gouvernés")
    indicators_sub = indicators.add_subparsers(dest="indicators_command", required=True)
    list_cmd = indicators_sub.add_parser("list", help="liste les indicateurs")
    list_cmd.add_argument("--concept")
    list_cmd.add_argument("--json", action="store_true")
    list_cmd.set_defaults(func=_cmd_indicators_list)

    get_cmd = indicators_sub.add_parser("get", help="calcule des indicateurs pour un symbole")
    get_cmd.add_argument("--symbol", required=True)
    get_cmd.add_argument("--timeframe", default="1h")
    get_cmd.add_argument("--lookback", default="5d")
    get_cmd.add_argument("--window", type=int, default=48)
    get_cmd.add_argument("--as-of", default="latest")
    get_cmd.add_argument("--names")
    get_cmd.add_argument("--json", action="store_true")
    get_cmd.set_defaults(func=_cmd_indicators_get)

    compare_cmd = indicators_sub.add_parser("compare", help="compare une famille de symboles")
    compare_cmd.add_argument("--family", required=True)
    compare_cmd.add_argument("--timeframe", default="1h")
    compare_cmd.add_argument("--lookback", default="5d")
    compare_cmd.add_argument("--window", type=int, default=48)
    compare_cmd.add_argument("--as-of", default="latest")
    compare_cmd.add_argument("--metrics")
    compare_cmd.add_argument("--json", action="store_true")
    compare_cmd.set_defaults(func=_cmd_indicators_compare)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(argv) if argv is not None else None
    if args_list is None:
        import sys

        args_list = sys.argv[1:]

    if not args_list:
        daemon.main([])
        return 0

    if args_list[0] == "daemon":
        daemon.main(args_list[1:])
        return 0
    if args_list[0] in _DAEMON_FLAGS:
        daemon.main(args_list)
        return 0

    parser = build_parser()
    args = parser.parse_args(args_list)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
