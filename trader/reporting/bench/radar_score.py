"""Walk-forward bench for production and shadow radar rankings."""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from trader.domain.market_data import Bar
from trader.domain.semantic.catalog import family_for_symbol
from trader.market.radar_config import load_radar_params
from trader.market.radar_data import download_daily_batch
from trader.market.rotation.wiring import build_rank_fn, venue_of
from trader.support.config.pool import load_pool

BENCH_SCHEMA_VERSION = 1
BENCH_VERSION = "radar_score_walk_forward_v1"
DEFAULT_HORIZONS = (1, 3, 5)
DEFAULT_TOP_K = 40
MIN_RECOMMENDATION_SESSIONS = 60
VARIANTS = ("production", "shadow")
VENUES = ("TW", "EU", "US")


def run_cache_bench(
    *,
    config_dir: str | Path,
    cache_dir: str | Path,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    """Replay every cached radar snapshot without downloading market data."""

    config_path = Path(config_dir)
    cache_path = Path(cache_dir)
    normalized_horizons = _normalize_horizons(horizons)
    limit = max(1, int(top_k))
    files = sorted(cache_path.glob("????-??-??.json"))
    pool = load_pool(config_path)
    params = load_radar_params(config_path)
    benchmark_symbols = sorted(set(params.benchmarks.values()) | {params.default_benchmark})
    requested_symbols = list(dict.fromkeys([*pool.symbols, *benchmark_symbols]))
    snapshots: list[dict[str, Any]] = []
    master_bars: dict[str, dict[str, Bar]] = defaultdict(dict)
    seen_signatures: set[str] = set()
    failures: list[dict[str, str]] = []

    for cache_file in files:
        as_of = cache_file.stem
        try:
            cached_bars = download_daily_batch(
                requested_symbols,
                as_of=as_of,
                cache_dir=cache_path,
            )
            for symbol, bars in cached_bars.items():
                for bar in bars:
                    if _valid_bar(bar):
                        master_bars[str(symbol)][str(bar.ts)] = bar

            def _fetch(symbols: list[str]) -> dict[str, list[Bar]]:
                return {symbol: cached_bars[symbol] for symbol in symbols if symbol in cached_bars}

            rank_result = build_rank_fn(
                config_path,
                fetch_fn=_fetch,
                as_of=f"{as_of}T23:59:59+00:00",
            )()
            audit = rank_result.get("score_audit")
            if not isinstance(audit, Mapping):
                failures.append({"as_of": as_of, "reason": "score_audit_missing"})
                continue
            signature = str(audit.get("input_signature") or "")
            if signature and signature in seen_signatures:
                continue
            if signature:
                seen_signatures.add(signature)
            snapshots.append(
                _snapshot_from_rank_result(
                    as_of=as_of,
                    rank_result=rank_result,
                    cached_bars=cached_bars,
                    top_k=limit,
                )
            )
        except Exception as exc:  # noqa: BLE001 - bench records individual cache failures
            failures.append({"as_of": as_of, "reason": exc.__class__.__name__})

    ordered_bars = {
        symbol: sorted(by_ts.values(), key=lambda bar: str(bar.ts))
        for symbol, by_ts in master_bars.items()
    }
    forward = evaluate_forward_performance(
        snapshots,
        bars_by_symbol=ordered_bars,
        horizons=normalized_horizons,
    )
    stability = evaluate_selection_stability(snapshots)
    session_count = len(snapshots)
    return {
        "schema_version": BENCH_SCHEMA_VERSION,
        "bench_version": BENCH_VERSION,
        "status": "insufficient_history"
        if session_count < MIN_RECOMMENDATION_SESSIONS
        else "shadow_evaluation",
        "production_switch_recommended": False,
        "configuration": {
            "top_k": limit,
            "horizons_sessions": list(normalized_horizons),
            "minimum_recommendation_sessions": MIN_RECOMMENDATION_SESSIONS,
            "production_score_version": "legacy_raw_v1",
            "shadow_score_version": "balanced_percentile_v1",
        },
        "coverage": {
            "cache_file_count": len(files),
            "unique_snapshot_count": session_count,
            "first_as_of": snapshots[0]["as_of"] if snapshots else None,
            "last_as_of": snapshots[-1]["as_of"] if snapshots else None,
            "failure_count": len(failures),
            "failures": failures,
        },
        "latest_score_audit": snapshots[-1].get("score_audit") if snapshots else None,
        "forward_performance": forward,
        "selection_stability": stability,
        "limitations": [
            "cache history is observational and shorter than a production calibration window",
            "returns are gross directional close-to-close proxies without costs or sizing",
            "no production switch is automatic even when the shadow variant leads",
        ],
    }


def evaluate_forward_performance(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    horizons: Sequence[int] = DEFAULT_HORIZONS,
) -> dict[str, Any]:
    """Measure gross directional forward returns for selected top-k symbols."""

    normalized_horizons = _normalize_horizons(horizons)
    indexed_bars: dict[str, tuple[list[Bar], dict[str, int]]] = {}
    for symbol, bars in bars_by_symbol.items():
        ordered = sorted((bar for bar in bars if _valid_bar(bar)), key=lambda bar: str(bar.ts))
        indexed_bars[str(symbol)] = (
            ordered,
            {str(bar.ts): index for index, bar in enumerate(ordered)},
        )

    observations: dict[tuple[str, str, int], list[float]] = defaultdict(list)
    for snapshot in snapshots:
        venues = snapshot.get("venues")
        if not isinstance(venues, Mapping):
            continue
        base_ts_by_symbol = snapshot.get("base_ts_by_symbol")
        if not isinstance(base_ts_by_symbol, Mapping):
            continue
        for venue in VENUES:
            venue_payload = venues.get(venue)
            if not isinstance(venue_payload, Mapping):
                continue
            for variant in VARIANTS:
                selected = venue_payload.get(variant)
                if not isinstance(selected, Sequence) or isinstance(selected, (str, bytes)):
                    continue
                for row in selected:
                    if not isinstance(row, Mapping):
                        continue
                    symbol = str(row.get("symbol") or "").strip()
                    bias = str(row.get("bias") or "").strip()
                    base_ts = str(base_ts_by_symbol.get(symbol) or "")
                    series = indexed_bars.get(symbol)
                    if not symbol or not base_ts or series is None or bias not in {"long", "short"}:
                        continue
                    bars, index_by_ts = series
                    base_index = index_by_ts.get(base_ts)
                    if base_index is None:
                        continue
                    base_close = float(bars[base_index].close)
                    if base_close <= 0:
                        continue
                    for horizon in normalized_horizons:
                        future_index = base_index + horizon
                        if future_index >= len(bars):
                            continue
                        raw_return = float(bars[future_index].close) / base_close - 1.0
                        directional_return = raw_return if bias == "long" else -raw_return
                        observations[(variant, venue, horizon)].append(directional_return)

    result: dict[str, Any] = {variant: {} for variant in VARIANTS}
    for variant in VARIANTS:
        for venue in VENUES:
            by_horizon: dict[str, Any] = {}
            for horizon in normalized_horizons:
                values = observations[(variant, venue, horizon)]
                by_horizon[str(horizon)] = _return_summary(values)
            result[variant][venue] = by_horizon
    return result


def evaluate_selection_stability(
    snapshots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Measure top-k turnover, directional balance, and family concentration."""

    result: dict[str, Any] = {variant: {} for variant in VARIANTS}
    for variant in VARIANTS:
        for venue in VENUES:
            previous: set[str] | None = None
            turnovers: list[float] = []
            long_shares: list[float] = []
            largest_family_shares: list[float] = []
            observed_selections = 0
            for snapshot in snapshots:
                venues = snapshot.get("venues")
                venue_payload = venues.get(venue) if isinstance(venues, Mapping) else None
                selected = venue_payload.get(variant) if isinstance(venue_payload, Mapping) else None
                if not isinstance(selected, Sequence) or isinstance(selected, (str, bytes)):
                    continue
                rows = [row for row in selected if isinstance(row, Mapping) and row.get("symbol")]
                if not rows:
                    continue
                observed_selections += 1
                symbols = {str(row["symbol"]) for row in rows}
                if previous is not None:
                    denominator = max(len(previous), len(symbols), 1)
                    turnovers.append(1.0 - len(previous & symbols) / denominator)
                previous = symbols
                long_shares.append(
                    sum(1 for row in rows if row.get("bias") == "long") / len(rows)
                )
                family_counts: dict[str, int] = defaultdict(int)
                for row in rows:
                    family_counts[str(row.get("family") or "unclassified")] += 1
                largest_family_shares.append(max(family_counts.values()) / len(rows))
            result[variant][venue] = {
                "selection_count": observed_selections,
                "transition_count": len(turnovers),
                "mean_turnover": statistics.fmean(turnovers) if turnovers else None,
                "median_turnover": statistics.median(turnovers) if turnovers else None,
                "mean_long_share": statistics.fmean(long_shares) if long_shares else None,
                "mean_largest_family_share": (
                    statistics.fmean(largest_family_shares)
                    if largest_family_shares
                    else None
                ),
            }
    return result


def write_bench(path: str | Path, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Radar score shadow walk-forward bench")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--cache-dir", default="state/radar_cache")
    parser.add_argument("--output", default="state/radar_score_bench.json")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--horizons", default="1,3,5")
    args = parser.parse_args(list(argv) if argv is not None else None)
    horizons = tuple(int(item.strip()) for item in args.horizons.split(",") if item.strip())
    payload = run_cache_bench(
        config_dir=args.config_dir,
        cache_dir=args.cache_dir,
        horizons=horizons,
        top_k=args.top_k,
    )
    write_bench(args.output, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _snapshot_from_rank_result(
    *,
    as_of: str,
    rank_result: Mapping[str, Any],
    cached_bars: Mapping[str, Sequence[Bar]],
    top_k: int,
) -> dict[str, Any]:
    audit = rank_result["score_audit"]
    audit_venues = audit.get("venues") if isinstance(audit, Mapping) else {}
    current_ranked = rank_result.get("ranked") or []
    venues: dict[str, Any] = {}
    for venue in VENUES:
        current = [
            {
                "symbol": str(row.get("symbol") or ""),
                "bias": str(row.get("bias") or ""),
                "family": family_for_symbol(str(row.get("symbol") or "")) or "unclassified",
            }
            for row in current_ranked
            if isinstance(row, Mapping) and venue_of(str(row.get("symbol") or "")) == venue
        ][:top_k]
        venue_audit = audit_venues.get(venue) if isinstance(audit_venues, Mapping) else None
        shadow_details = (
            venue_audit.get("shadow_top_details") if isinstance(venue_audit, Mapping) else []
        )
        shadow = [
            {
                "symbol": str(row.get("symbol") or ""),
                "bias": str(row.get("bias") or ""),
                "family": str(row.get("family") or "unclassified"),
            }
            for row in shadow_details or []
            if isinstance(row, Mapping) and row.get("symbol")
        ][:top_k]
        venues[venue] = {"production": current, "shadow": shadow}

    base_ts_by_symbol = {
        str(symbol): str(valid[-1].ts)
        for symbol, bars in cached_bars.items()
        if (valid := [bar for bar in bars if _valid_bar(bar)])
    }
    return {
        "as_of": as_of,
        "input_signature": audit.get("input_signature"),
        "score_audit": {
            "global_component_balance": audit.get("global_component_balance"),
            "venues": {
                venue: {
                    "component_balance": audit_venues.get(venue, {}).get("component_balance"),
                    "top_k_overlap_count": audit_venues.get(venue, {}).get(
                        "top_k_overlap_count"
                    ),
                }
                for venue in VENUES
            },
        },
        "venues": venues,
        "base_ts_by_symbol": base_ts_by_symbol,
    }


def _return_summary(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {
            "observation_count": 0,
            "mean_directional_return": None,
            "median_directional_return": None,
            "hit_rate": None,
        }
    return {
        "observation_count": len(values),
        "mean_directional_return": statistics.fmean(values),
        "median_directional_return": statistics.median(values),
        "hit_rate": sum(value > 0 for value in values) / len(values),
    }


def _normalize_horizons(horizons: Iterable[int]) -> tuple[int, ...]:
    normalized = tuple(sorted({int(horizon) for horizon in horizons if int(horizon) > 0}))
    if not normalized:
        raise ValueError("at_least_one_positive_horizon_required")
    return normalized


def _valid_bar(bar: Bar) -> bool:
    try:
        return float(bar.close) == float(bar.close) and float(bar.close) > 0
    except (AttributeError, TypeError, ValueError):
        return False


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "evaluate_forward_performance",
    "evaluate_selection_stability",
    "main",
    "run_cache_bench",
    "write_bench",
]
