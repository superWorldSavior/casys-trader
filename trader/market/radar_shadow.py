"""Shadow-only radar scoring diagnostics.

The production radar keeps using :func:`trader.market.radar.score_symbol`.
This module computes a counterfactual ranking from the exact same eligible
symbols so score calibration can be measured before any production switch.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from trader.market.radar_config import RadarParams

AUDIT_SCHEMA_VERSION = 1
AUDIT_VERSION = "radar_score_shadow_v1"
PRODUCTION_SCORE_VERSION = "legacy_raw_v1"
SHADOW_SCORE_VERSION = "balanced_percentile_v1"
DEFAULT_TOP_K = 40
DEFAULT_SHADOW_TREND_WEIGHT = 0.5
DEFAULT_SHADOW_RELATIVE_STRENGTH_WEIGHT = 0.5
SUPPORTED_VENUES = ("TW", "EU", "US")


def percentile_ranks(values: Mapping[str, float]) -> dict[str, float]:
    """Return deterministic average-tie percentile ranks in ``[0, 1]``."""

    ordered = sorted(
        (
            (str(symbol), float(value))
            for symbol, value in values.items()
            if str(symbol) and math.isfinite(float(value))
        ),
        key=lambda item: (item[1], item[0]),
    )
    if not ordered:
        return {}
    if len(ordered) == 1:
        return {ordered[0][0]: 1.0}

    out: dict[str, float] = {}
    index = 0
    denominator = len(ordered) - 1
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        average_zero_based_rank = (index + end - 1) / 2.0
        percentile = average_zero_based_rank / denominator
        for position in range(index, end):
            out[ordered[position][0]] = percentile
        index = end
    return out


def build_shadow_rankings(
    *,
    current_ranked: Sequence[Mapping[str, Any]],
    components_by_symbol: Mapping[str, Mapping[str, Any]],
    benchmark_return_by_symbol: Mapping[str, float],
    tilt_by_symbol: Mapping[str, float] | None,
    venue_for: Callable[[str], str],
    family_for: Callable[[str], str | None],
    trend_weight: float = DEFAULT_SHADOW_TREND_WEIGHT,
    relative_strength_weight: float = DEFAULT_SHADOW_RELATIVE_STRENGTH_WEIGHT,
) -> dict[str, list[dict[str, Any]]]:
    """Build balanced percentile rankings without changing production output.

    ``efficiency_ratio`` and direction-aligned relative strength are ranked
    cross-sectionally inside each venue.  Amplitude remains an eligibility
    concern and is deliberately not rewarded by the shadow score.
    """

    total_weight = float(trend_weight) + float(relative_strength_weight)
    if trend_weight < 0 or relative_strength_weight < 0 or total_weight <= 0:
        raise ValueError("shadow_weights_must_be_non_negative_and_non_zero")
    tilts = tilt_by_symbol or {}

    raw_by_venue: dict[str, list[dict[str, Any]]] = {venue: [] for venue in SUPPORTED_VENUES}
    for ranked_row in current_ranked:
        symbol = str(ranked_row.get("symbol") or "").strip()
        if not symbol:
            continue
        venue = str(venue_for(symbol) or "").strip().upper()
        if venue not in raw_by_venue:
            continue
        components = components_by_symbol.get(symbol)
        if not isinstance(components, Mapping):
            continue
        try:
            efficiency_ratio = float(components["efficiency_ratio"])
            ret = float(components["ret"])
            amplitude = float(components["amplitude"])
            benchmark_return = float(benchmark_return_by_symbol.get(symbol, 0.0))
            tilt = float(tilts.get(symbol, 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        if not all(
            math.isfinite(value)
            for value in (efficiency_ratio, ret, amplitude, benchmark_return, tilt)
        ):
            continue
        direction = 1.0 if ret >= 0 else -1.0
        raw_by_venue[venue].append(
            {
                "symbol": symbol,
                "venue": venue,
                "family": family_for(symbol) or "unclassified",
                "direction": direction,
                "efficiency_ratio": efficiency_ratio,
                "return": ret,
                "benchmark_return": benchmark_return,
                "aligned_relative_strength": (ret - benchmark_return) * direction,
                "amplitude": amplitude,
                "tilt": tilt,
            }
        )

    rankings: dict[str, list[dict[str, Any]]] = {}
    for venue in SUPPORTED_VENUES:
        rows = raw_by_venue[venue]
        trend_percentiles = percentile_ranks(
            {row["symbol"]: row["efficiency_ratio"] for row in rows}
        )
        relative_strength_percentiles = percentile_ranks(
            {row["symbol"]: row["aligned_relative_strength"] for row in rows}
        )
        ranked: list[dict[str, Any]] = []
        for row in rows:
            symbol = row["symbol"]
            trend_percentile = trend_percentiles[symbol]
            relative_strength_percentile = relative_strength_percentiles[symbol]
            base_attractiveness = (
                trend_weight * trend_percentile
                + relative_strength_weight * relative_strength_percentile
            ) / total_weight
            attractiveness = base_attractiveness * (1.0 + row["tilt"])
            directional_score = row["direction"] * attractiveness
            ranked.append(
                {
                    "symbol": symbol,
                    "venue": venue,
                    "family": row["family"],
                    "directional_score": directional_score,
                    "attractiveness": abs(directional_score),
                    "bias": "long" if row["direction"] > 0 else "short",
                    "components": {
                        "trend_percentile": trend_percentile,
                        "relative_strength_percentile": relative_strength_percentile,
                        "efficiency_ratio": row["efficiency_ratio"],
                        "aligned_relative_strength": row["aligned_relative_strength"],
                        "return": row["return"],
                        "benchmark_return": row["benchmark_return"],
                        "amplitude": row["amplitude"],
                        "amplitude_role": "eligibility_only",
                        "tilt": row["tilt"],
                    },
                }
            )
        ranked.sort(key=lambda item: (-item["attractiveness"], item["symbol"]))
        rankings[venue] = ranked
    return rankings


def build_score_audit(
    *,
    as_of: str,
    current_ranked: Sequence[Mapping[str, Any]],
    components_by_symbol: Mapping[str, Mapping[str, Any]],
    benchmark_return_by_symbol: Mapping[str, float],
    tilt_by_symbol: Mapping[str, float] | None,
    venue_for: Callable[[str], str],
    family_for: Callable[[str], str | None],
    params: RadarParams,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    """Build the observable current-vs-shadow score audit envelope."""

    limit = max(1, int(top_k))
    tilts = tilt_by_symbol or {}
    shadow_rankings = build_shadow_rankings(
        current_ranked=current_ranked,
        components_by_symbol=components_by_symbol,
        benchmark_return_by_symbol=benchmark_return_by_symbol,
        tilt_by_symbol=tilts,
        venue_for=venue_for,
        family_for=family_for,
    )
    current_by_venue: dict[str, list[dict[str, Any]]] = {
        venue: [] for venue in SUPPORTED_VENUES
    }
    legacy_components_by_venue: dict[str, list[dict[str, float]]] = {
        venue: [] for venue in SUPPORTED_VENUES
    }
    signature_rows: list[dict[str, Any]] = []

    for item in current_ranked:
        symbol = str(item.get("symbol") or "").strip()
        venue = str(venue_for(symbol) or "").strip().upper()
        if not symbol or venue not in current_by_venue:
            continue
        components = components_by_symbol.get(symbol)
        if not isinstance(components, Mapping):
            continue
        try:
            efficiency_ratio = float(components["efficiency_ratio"])
            ret = float(components["ret"])
            amplitude = float(components["amplitude"])
            benchmark_return = float(benchmark_return_by_symbol.get(symbol, 0.0))
            tilt = float(tilts.get(symbol, 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        if not all(
            math.isfinite(value)
            for value in (efficiency_ratio, ret, amplitude, benchmark_return, tilt)
        ):
            continue
        direction = 1.0 if ret >= 0 else -1.0
        trend_contribution = params.w_trend * efficiency_ratio * direction
        aligned_relative_strength = (ret - benchmark_return) * direction
        legacy_relative_strength_contribution = params.w_rs * aligned_relative_strength
        amplitude_uplift = params.w_amp * min(amplitude, params.amplitude_cap)
        current_by_venue[venue].append(
            {
                "symbol": symbol,
                "family": family_for(symbol) or "unclassified",
                "bias": str(item.get("bias") or ("long" if direction > 0 else "short")),
                "attractiveness": float(item.get("attractiveness") or 0.0),
            }
        )
        legacy_components_by_venue[venue].append(
            {
                "direction": direction,
                "trend": trend_contribution,
                "relative_strength": legacy_relative_strength_contribution,
                "aligned_relative_strength": aligned_relative_strength,
                "amplitude_uplift": amplitude_uplift,
            }
        )
        signature_rows.append(
            {
                "symbol": symbol,
                "venue": venue,
                "efficiency_ratio": efficiency_ratio,
                "return": ret,
                "benchmark_return": benchmark_return,
                "amplitude": amplitude,
                "tilt": tilt,
            }
        )

    venues: dict[str, Any] = {}
    all_legacy_components: list[dict[str, float]] = []
    for venue in SUPPORTED_VENUES:
        current_rows = current_by_venue[venue]
        current_rows.sort(key=lambda item: (-item["attractiveness"], item["symbol"]))
        current_top = current_rows[:limit]
        shadow_top = shadow_rankings[venue][:limit]
        current_symbols = {row["symbol"] for row in current_top}
        shadow_symbols = {row["symbol"] for row in shadow_top}
        legacy_components = legacy_components_by_venue[venue]
        all_legacy_components.extend(legacy_components)
        venues[venue] = {
            "eligible_count": len(current_rows),
            "top_k": min(limit, len(current_rows)),
            "top_k_overlap_count": len(current_symbols & shadow_symbols),
            "top_k_change_count": len(current_symbols - shadow_symbols),
            "current": _selection_summary(current_top, limit=limit),
            "shadow": _selection_summary(shadow_top, limit=limit),
            "component_balance": _component_balance(legacy_components),
            "current_top": [row["symbol"] for row in current_top],
            "shadow_top": [row["symbol"] for row in shadow_top],
            "shadow_top_details": [
                {
                    "symbol": row["symbol"],
                    "family": row["family"],
                    "bias": row["bias"],
                    "attractiveness": row["attractiveness"],
                    "trend_percentile": row["components"]["trend_percentile"],
                    "relative_strength_percentile": row["components"]
                    ["relative_strength_percentile"],
                }
                for row in shadow_top
            ],
        }

    encoded_signature = json.dumps(
        sorted(signature_rows, key=lambda row: (row["venue"], row["symbol"])),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "audit_version": AUDIT_VERSION,
        "as_of": str(as_of),
        "status": "shadow_only",
        "selection_effect": "none",
        "production_score_version": PRODUCTION_SCORE_VERSION,
        "shadow_score_version": SHADOW_SCORE_VERSION,
        "input_signature": hashlib.sha256(encoded_signature).hexdigest(),
        "configuration": {
            "top_k": limit,
            "production_weights": {
                "trend": params.w_trend,
                "relative_strength": params.w_rs,
                "amplitude": params.w_amp,
            },
            "shadow_weights": {
                "trend_percentile": DEFAULT_SHADOW_TREND_WEIGHT,
                "relative_strength_percentile": DEFAULT_SHADOW_RELATIVE_STRENGTH_WEIGHT,
                "amplitude": 0.0,
            },
            "shadow_amplitude_role": "eligibility_only",
        },
        "coverage": {
            "eligible_count": sum(len(rows) for rows in current_by_venue.values()),
            "venues": {
                venue: len(current_by_venue[venue]) for venue in SUPPORTED_VENUES
            },
        },
        "global_component_balance": _component_balance(all_legacy_components),
        "venues": venues,
        "limitations": [
            "shadow ranking never changes the active candidate scope",
            "forward performance must be evaluated separately before activation",
        ],
    }


def _component_balance(rows: Sequence[Mapping[str, float]]) -> dict[str, Any]:
    if not rows:
        return {
            "status": "missing",
            "sample_count": 0,
            "median_abs_trend": None,
            "median_abs_relative_strength": None,
            "median_trend_share": None,
            "median_amplitude_uplift": None,
            "short_relative_strength_offset_count": 0,
            "short_count": 0,
        }
    trend_values = [abs(float(row["trend"])) for row in rows]
    relative_strength_values = [abs(float(row["relative_strength"])) for row in rows]
    shares = [
        trend / (trend + relative_strength)
        for trend, relative_strength in zip(trend_values, relative_strength_values, strict=True)
        if trend + relative_strength > 0
    ]
    short_rows = [row for row in rows if float(row["direction"]) < 0]
    short_offsets = [
        row
        for row in short_rows
        if float(row["aligned_relative_strength"]) > 0
        and float(row["relative_strength"]) > 0
    ]
    return {
        "status": "observed",
        "sample_count": len(rows),
        "median_abs_trend": statistics.median(trend_values),
        "median_abs_relative_strength": statistics.median(relative_strength_values),
        "median_trend_share": statistics.median(shares) if shares else None,
        "median_amplitude_uplift": statistics.median(
            abs(float(row["amplitude_uplift"])) for row in rows
        ),
        "short_relative_strength_offset_count": len(short_offsets),
        "short_count": len(short_rows),
        "short_relative_strength_offset_semantics": (
            "legacy relative strength offsets a short when it underperforms its benchmark"
        ),
    }


def _selection_summary(rows: Sequence[Mapping[str, Any]], *, limit: int) -> dict[str, Any]:
    selected = list(rows[:limit])
    family_counts = Counter(str(row.get("family") or "unclassified") for row in selected)
    selected_count = len(selected)
    largest_family_count = max(family_counts.values(), default=0)
    return {
        "selected_count": selected_count,
        "long_count": sum(1 for row in selected if row.get("bias") == "long"),
        "short_count": sum(1 for row in selected if row.get("bias") == "short"),
        "family_count": len(family_counts),
        "largest_family_count": largest_family_count,
        "largest_family_share": (
            largest_family_count / selected_count if selected_count else None
        ),
        "families": dict(sorted(family_counts.items())),
    }


__all__ = [
    "AUDIT_VERSION",
    "SHADOW_SCORE_VERSION",
    "build_score_audit",
    "build_shadow_rankings",
    "percentile_ranks",
]
