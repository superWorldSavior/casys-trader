"""Command line interface for casys-trader."""

from __future__ import annotations

import argparse
import json
from datetime import timedelta
from typing import Sequence

import yaml

from . import code_version, daemon, decision_audit, decision_bench, decision_ledger
from .features import DEFAULT_INDICATORS, build_indicator_snapshot, compute_indicator_values
from .semantic.catalog import FAMILIES, describe_semantic_layer, find_indicators, list_indicators, normalize_temporal_query
from .tools import market

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


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _parse_csv(value: str | None, default: list[str]) -> list[str]:
    if not value:
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


def _load_universe_symbols() -> list[str]:
    cfg = yaml.safe_load((daemon.ROOT / "config" / "universe.yaml").read_text())
    return list(cfg["symbols"])


def _read_state_json(filename: str) -> object | None:
    path = daemon.STATE_DIR / filename
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _cmd_status(args: argparse.Namespace) -> int:
    payload = {
        "daemon_status": _read_state_json("daemon_status.json"),
        "current_report": _read_state_json("current_report.json"),
        "last_report": _read_state_json("last_report.json"),
        "broker": _read_state_json("broker.json"),
        "scheduler": _read_state_json("scheduler.json"),
        "trade_plans": _read_state_json("trade_plans.json"),
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
    print(f"model_calls: {status.get('model_calls_used', 0)}/{status.get('max_model_calls_per_cycle')}")
    print(f"cash: {broker.get('cash') if isinstance(broker, dict) else None}")
    print("positions:", ", ".join(positions.keys()) if positions else "none")
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
    store = decision_ledger.DecisionLedgerStore(daemon.STATE_DIR / decision_ledger.DEFAULT_LEDGER_FILENAME)
    refreshed = decision_audit.refresh_audit_payload(
        payload,
        ledger_rows=store.read_all(),
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
    parsed_times = [decision_audit.parse_ts(row.get("cycle_ts")) for row in rows]
    valid_times = [ts for ts in parsed_times if ts is not None]
    prices: dict[str, list[dict]] = {}
    if rows and valid_times:
        max_horizon = max((decision_audit.parse_horizon(horizon) for horizon in horizons), default=timedelta())
        start = (min(valid_times) - timedelta(days=1)).date().isoformat()
        end = (max(valid_times) + max_horizon + timedelta(days=1)).date().isoformat()
        symbols = sorted({str(row.get("symbol")) for row in rows if row.get("symbol")})
        prices = decision_audit.load_prices_yfinance(symbols, start=start, end=end, interval=args.interval)

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
        )
        context_symbols = _bench_context_symbols(args.context_symbols, preview_cases)
        context_history, context_metadata = decision_bench.load_reconstruction_history(
            preview_cases,
            symbols=context_symbols,
            interval=args.context_interval,
            padding_days=args.context_padding_days,
        )
        context_kwargs = {
            "context_history": context_history,
            "context_symbols": context_symbols,
            "context_interval": args.context_interval,
            "context_lookback_bars": args.context_lookback_bars,
            "cockpit_window": args.cockpit_window,
            "context_metadata": context_metadata,
        }

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
            **context_kwargs,
        )

    output_path = daemon.STATE_DIR / args.output
    daemon.STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload["output_path"] = str(output_path)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.json:
        _print_json(payload)
    elif args.dry_run:
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
    daemon_parser.add_argument("--max-indicators-per-request", type=int, default=4)
    daemon_parser.add_argument("--max-model-calls-per-cycle", type=int, default=25)
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

    decisions = sub.add_parser("decisions", help="journal des décisions agent")
    decisions_sub = decisions.add_subparsers(dest="decisions_command", required=True)
    decisions_list = decisions_sub.add_parser("list", help="liste les décisions persistées")
    decisions_list.add_argument("--symbol")
    decisions_list.add_argument("--limit", type=int)
    decisions_list.add_argument("--json", action="store_true")
    decisions_list.set_defaults(func=_cmd_decisions_list)

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
    decisions_bench.add_argument("--timeout-s", type=int, default=120)
    decisions_bench.add_argument("--hide-original", action="store_true")
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
