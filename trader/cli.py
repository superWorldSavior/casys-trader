"""Command line interface for casys-trader."""

from __future__ import annotations

import argparse
import json
from typing import Sequence

import yaml

from . import daemon
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
    "--bootstrap-all",
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
    daemon_parser.add_argument("--bootstrap-all", action="store_true")

    status = sub.add_parser("status", help="état courant du daemon")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=_cmd_status)

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
