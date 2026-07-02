"""Probe d'intégration : le canal REQUEST_CONTEXT marche-t-il de bout en bout ?

Question : sur un contexte synthétique frais où le cockpit compact ne montre pas
certains indicateurs utiles, l'agent batch émet-il `action="REQUEST_CONTEXT"` ?
S'il le fait, emploie-t-il les noms canoniques de `features.DEFAULT_INDICATORS`
ou les abréviations vues dans le cockpit (`er`, `ac`, `z`, etc.) ? Et que résout
réellement `resolve_indicator_requests` par rapport à ce qui a été demandé ?

Protocole :
1. Construire un cockpit déterministe (barres synthétiques fraîches, pas de
   `stale_market_data`) avec impulsions récentes à mèches larges. Le setup rend
   utiles `ohlc_volatility`, `range_position` et `candle_wick_skew`, absents des
   colonnes du cockpit compact.
2. Appeler le vrai `codex_client.decide_batch(..., allow_context_request=True)`
   sur plusieurs symboles pour mesurer un taux et non un point.
3. Pour chaque `ContextResearchRequest`, capturer les champs exacts demandés,
   appeler le vrai `resolve_indicator_requests`, puis comparer noms demandés,
   noms acceptés strictement, noms silencieusement jetés et fallback effectif.
4. Rejouer le 2e batch avec `allow_context_request=False`, `research` injecté et
   `prior_rationale`, comme le daemon.

RÉSULTAT (à remplir au run) :
"""

from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path
from typing import Any

from trader.agent import client as codex_client
from trader.agent.context import (
    build_market_cockpit,
    resolve_indicator_requests,
)
from trader.market.features import DEFAULT_INDICATORS
from trader.semantic.catalog import INDICATOR_COLUMNS
from trader.tools.market import Bar

SYMBOLS = ["SPY", "QQQ", "NVDA", "CL=F", "BZ=F", "GC=F"]
WINDOW = 48
RUNTIME_INTERVAL = "1h"
RUNTIME_LOOKBACK = "5d"
MAX_CONTEXT_REQUESTS_PER_SYMBOL = 2
MAX_INDICATORS_PER_REQUEST = 4

MISSING_BUT_USEFUL = ["ohlc_volatility", "range_position", "candle_wick_skew"]
BAD_RATIONALE_PREFIXES = (
    "batch_bad_output",
    "codex_bad_output",
    "llm_failed",
    "missing_in_batch",
)

ALIAS_TO_CANONICAL = {abbr: canonical for canonical, abbr in INDICATOR_COLUMNS.items()}
KNOWN_INDICATORS = set(DEFAULT_INDICATORS)

PROBE_LEARNING = (
    "Les faux départs récents venaient de spikes à mèches larges. Avant de "
    "trader un z extrême ou une divergence leader/laggard, demande un petit "
    "REQUEST_CONTEXT si le cockpit ne montre pas le risque OHLC/range "
    "(ohlc_volatility, range_position, candle_wick_skew). Pour le régime, le "
    "cockpit affiche er/ac en compact ; sur doute multi-timeframe, tire le "
    "complément plutôt que de deviner."
)


def impulse_bars(symbol: str, *, interval: str = RUNTIME_INTERVAL, lookback: str = RUNTIME_LOOKBACK) -> list[Bar]:
    """Barres synthétiques : impulsion exploitable mais risquée par mèches larges."""
    profiles = {
        "SPY": (430.0, 0.010, 0.038, 1.0),
        "QQQ": (365.0, 0.018, 0.054, 1.0),
        "NVDA": (910.0, -0.006, 0.046, 1.0),
        "CL=F": (78.0, 0.006, 0.068, 1.0),
        "BZ=F": (82.0, -0.004, 0.042, -1.0),
        "GC=F": (2360.0, 0.004, 0.032, -1.0),
    }
    base, drift_pct, shock_pct, direction = profiles.get(
        symbol,
        (80.0 + sum(ord(c) for c in symbol) % 120, 0.0, 0.035, 1.0),
    )
    seed = sum(ord(c) for c in f"{symbol}:{interval}:{lookback}")
    n = 120
    bars: list[Bar] = []
    previous_close = base
    for i in range(n):
        phase = i / (n - 1)
        chop = math.sin((seed + i) * 1.73) * base * 0.0045
        micro = math.cos((seed // 3 + i) * 0.61) * base * 0.002
        impulse = 0.0
        if i >= n - 8:
            impulse = direction * base * shock_pct * ((i - (n - 8)) / 7)
        close = base * (1 + drift_pct * phase) + chop + micro + impulse
        open_ = previous_close + math.sin((seed + i) * 0.37) * base * 0.0015
        wick = base * (0.007 + (0.020 if i >= n - 6 else 0.002 * (i % 3)))
        high = max(open_, close) + wick
        low = min(open_, close) - wick * (1.35 if i >= n - 4 else 1.0)
        volume = 1000.0 + (seed % 400) + i * 7.0 + (450.0 if i >= n - 6 else 0.0)
        bars.append(
            Bar(
                ts=f"synthetic:{interval}:{lookback}:t{i}",
                open=round(open_, 6),
                high=round(high, 6),
                low=round(low, 6),
                close=round(close, 6),
                volume=round(volume, 6),
            )
        )
        previous_close = close
    return bars


def shared_context() -> tuple[dict[str, Any], dict[str, list[Bar]]]:
    bars = {symbol: impulse_bars(symbol) for symbol in SYMBOLS}
    prices = {symbol: bars[symbol][-1].close for symbol in SYMBOLS}
    cockpit = build_market_cockpit(bars, symbols=SYMBOLS, prices=prices, window=WINDOW)
    return (
        {
            "now": "2026-06-08T13:00:00+08:00",
            "portfolio": {"cash": 100000.0, "equity": 100000.0, "positions": {}},
            "risk_limits": {"max_gross_exposure_pct": 1.0, "max_symbol_exposure_pct": 0.25},
            "semantic": {"requestable_indicator_ids": DEFAULT_INDICATORS},
            "cockpit": cockpit,
            "stale_market_data": [],
            "kpis": {"return_pct": 0.0, "max_drawdown_pct": 0.0, "sharpe": None, "n_trades": 0},
            "attribution": {"round_trips": [], "by_confidence": {}, "by_exit_reason": {}},
            "learnings": [{"note": PROBE_LEARNING, "source": "probe_context_request"}],
            "probe_setup": {
                "fresh_synthetic_market_data": True,
                "missing_but_useful": MISSING_BUT_USEFUL,
                "reason": "wide-wick impulse makes OHLC/range context useful before sizing or fading",
            },
        },
        bars,
    )


def load_text(path: str) -> str:
    file_path = Path(path)
    return file_path.read_text(encoding="utf-8") if file_path.exists() else ""


def request_to_dict(request: codex_client.IndicatorRequest) -> dict[str, Any]:
    return {
        "symbol": request.symbol,
        "indicators": list(request.indicators),
        "timeframe": request.timeframe,
        "lookback": request.lookback,
        "window": request.window,
        "as_of": request.as_of,
    }


def response_summary(response: codex_client.Decision | codex_client.ContextResearchRequest) -> dict[str, Any]:
    if isinstance(response, codex_client.ContextResearchRequest):
        return {
            "type": "ContextResearchRequest",
            "symbol": response.symbol,
            "rationale": response.rationale,
            "next_wake_in_minutes": response.next_wake_in_minutes,
            "requests": [request_to_dict(request) for request in response.requests],
            "llm_provider": response.llm_provider,
            "llm_model": response.llm_model,
            "llm_fallback_reason": response.llm_fallback_reason,
        }
    return {
        "type": "Decision",
        "symbol": response.symbol,
        "action": response.action,
        "quantity": response.quantity,
        "confidence": response.confidence,
        "rationale": response.rationale,
        "intent": response.intent,
        "next_wake_in_minutes": response.next_wake_in_minutes,
        "exit_plan": response.exit_plan,
        "indicator_watch": response.indicator_watch,
        "learning": response.learning,
        "llm_provider": response.llm_provider,
        "llm_model": response.llm_model,
        "llm_fallback_reason": response.llm_fallback_reason,
        "llm_error": response.llm_error,
    }


def classify_indicator_name(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "accepted_by_resolver": name in KNOWN_INDICATORS,
        "is_cockpit_abbreviation": name in ALIAS_TO_CANONICAL,
        "abbreviation_for": ALIAS_TO_CANONICAL.get(name),
    }


def analyze_resolved_request(
    request: codex_client.IndicatorRequest,
    resolved_entry: dict[str, Any] | None,
) -> dict[str, Any]:
    raw_names = [str(name) for name in request.indicators]
    accepted_names = [name for name in raw_names if name in KNOWN_INDICATORS][:MAX_INDICATORS_PER_REQUEST]
    fallback_used = not accepted_names
    effective_names = accepted_names or DEFAULT_INDICATORS[:MAX_INDICATORS_PER_REQUEST]
    resolved_names = list((resolved_entry or {}).get("indicators", {}).keys())
    direct_resolved = [name for name in raw_names if name in resolved_names]
    silently_dropped = [name for name in raw_names if name not in KNOWN_INDICATORS]
    truncated = len([name for name in raw_names if name in KNOWN_INDICATORS]) > MAX_INDICATORS_PER_REQUEST
    return {
        "request": request_to_dict(request),
        "name_classes": [classify_indicator_name(name) for name in raw_names],
        "accepted_names_before_fallback": accepted_names,
        "silently_dropped_names": silently_dropped,
        "fallback_used": fallback_used,
        "resolver_effective_names_expected": effective_names,
        "resolved_names": resolved_names,
        "directly_resolved_requested_names": direct_resolved,
        "canonical_names_if_aliases_expanded": [
            ALIAS_TO_CANONICAL[name] for name in raw_names if name in ALIAS_TO_CANONICAL
        ],
        "canonical_truncation": truncated,
        "resolved_entry": resolved_entry,
    }


def pop_matching_resolved_entries(
    requests: list[codex_client.IndicatorRequest],
    research: dict[str, Any],
) -> list[dict[str, Any] | None]:
    entries = list(research.get("requests", []))
    matched: list[dict[str, Any] | None] = []
    cursor = 0
    for request in requests[:MAX_CONTEXT_REQUESTS_PER_SYMBOL]:
        if request.symbol not in SYMBOLS:
            matched.append(None)
            continue
        matched.append(entries[cursor] if cursor < len(entries) else None)
        cursor += 1
    return matched


def resolve_and_analyze(
    request: codex_client.ContextResearchRequest,
    bars_by_symbol: dict[str, list[Bar]],
) -> dict[str, Any]:
    fetch_calls: list[dict[str, str]] = []

    def synthetic_get_bars(symbol: str, lookback: str, interval: str) -> list[Bar]:
        fetch_calls.append({"symbol": symbol, "lookback": lookback, "interval": interval})
        return impulse_bars(symbol, interval=interval, lookback=lookback)

    research = resolve_indicator_requests(
        request.requests,
        bars_by_symbol,
        symbols=SYMBOLS,
        max_requests=MAX_CONTEXT_REQUESTS_PER_SYMBOL,
        max_indicators=MAX_INDICATORS_PER_REQUEST,
        market_get_bars=synthetic_get_bars,
        cached_interval=RUNTIME_INTERVAL,
        cached_lookback=RUNTIME_LOOKBACK,
    )
    matched_entries = pop_matching_resolved_entries(request.requests, research)
    return {
        "research": research,
        "fetch_calls": fetch_calls,
        "request_analysis": [
            analyze_resolved_request(indicator_request, resolved_entry)
            for indicator_request, resolved_entry in zip(
                request.requests[:MAX_CONTEXT_REQUESTS_PER_SYMBOL],
                matched_entries,
                strict=True,
            )
        ],
    }


def is_structural_decision_ok(response: codex_client.Decision | codex_client.ContextResearchRequest | None) -> bool:
    if not isinstance(response, codex_client.Decision):
        return False
    return not any(response.rationale.startswith(prefix) for prefix in BAD_RATIONALE_PREFIXES)


def rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "count": numerator,
        "total": denominator,
        "rate": None if denominator == 0 else round(numerator / denominator, 6),
        "label": f"{numerator}/{denominator}",
    }


def counter_dict(counter: Counter[str]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def cockpit_is_missing_indicator(cockpit_cols: list[str], indicator: str) -> bool:
    alias = INDICATOR_COLUMNS.get(indicator)
    return indicator not in cockpit_cols and (alias is None or alias not in cockpit_cols)


def aggregate_metrics(
    first_responses: dict[str, codex_client.Decision | codex_client.ContextResearchRequest],
    resolution_by_symbol: dict[str, dict[str, Any]],
    second_responses: dict[str, codex_client.Decision | codex_client.ContextResearchRequest],
) -> dict[str, Any]:
    context_symbols = [
        symbol
        for symbol, response in first_responses.items()
        if isinstance(response, codex_client.ContextResearchRequest)
    ]
    requested_names: Counter[str] = Counter()
    canonical_names: Counter[str] = Counter()
    abbreviation_names: Counter[str] = Counter()
    unknown_names: Counter[str] = Counter()
    totals = Counter()

    for payload in resolution_by_symbol.values():
        for item in payload["request_analysis"]:
            raw_names = item["request"]["indicators"]
            requested_names.update(raw_names)
            canonical_names.update(name for name in raw_names if name in KNOWN_INDICATORS)
            abbreviation_names.update(name for name in raw_names if name in ALIAS_TO_CANONICAL)
            unknown_names.update(name for name in raw_names if name not in KNOWN_INDICATORS)
            totals["indicator_names_requested"] += len(raw_names)
            totals["indicator_names_accepted_before_fallback"] += len(item["accepted_names_before_fallback"])
            totals["indicator_names_silently_dropped"] += len(item["silently_dropped_names"])
            totals["indicator_names_directly_resolved"] += len(item["directly_resolved_requested_names"])
            totals["requests_analyzed"] += 1
            if item["fallback_used"]:
                totals["requests_using_fallback"] += 1

    round_trip_ok = sum(1 for symbol in context_symbols if is_structural_decision_ok(second_responses.get(symbol)))
    return {
        "request_context": rate(len(context_symbols), len(first_responses)),
        "resolver": {
            "requests_analyzed": totals["requests_analyzed"],
            "requests_using_fallback": rate(totals["requests_using_fallback"], totals["requests_analyzed"]),
            "indicator_names_requested": totals["indicator_names_requested"],
            "indicator_names_accepted_before_fallback": rate(
                totals["indicator_names_accepted_before_fallback"],
                totals["indicator_names_requested"],
            ),
            "indicator_names_directly_resolved": rate(
                totals["indicator_names_directly_resolved"],
                totals["indicator_names_requested"],
            ),
            "indicator_names_silently_dropped": rate(
                totals["indicator_names_silently_dropped"],
                totals["indicator_names_requested"],
            ),
        },
        "vocabulary": {
            "requested_names": counter_dict(requested_names),
            "canonical_names": counter_dict(canonical_names),
            "cockpit_abbreviation_names": counter_dict(abbreviation_names),
            "not_accepted_by_resolver_names": counter_dict(unknown_names),
        },
        "round_trip": {
            "attempted": bool(context_symbols),
            "symbols": context_symbols,
            "final_decision_ok": rate(round_trip_ok, len(context_symbols)),
        },
    }


def main() -> None:
    mandate = load_text("mandate/mandate.md")
    memory = "\n\n".join(part for part in [load_text("mandate/memory.md"), PROBE_LEARNING] if part)
    ctx, bars_by_symbol = shared_context()

    first = codex_client.decide_batch(
        symbols=SYMBOLS,
        mandate=mandate,
        memory=memory,
        shared_context=ctx,
        per_symbol={symbol: {"indicator_triggers": []} for symbol in SYMBOLS},
        allow_context_request=True,
        timeout_s=180,
    )

    needs = {
        symbol: response
        for symbol, response in first.items()
        if isinstance(response, codex_client.ContextResearchRequest)
    }
    resolution_by_symbol = {
        symbol: resolve_and_analyze(request, bars_by_symbol)
        for symbol, request in needs.items()
    }

    if needs:
        second = codex_client.decide_batch(
            symbols=list(needs),
            mandate=mandate,
            memory=memory,
            shared_context=ctx,
            per_symbol={
                symbol: {
                    "indicator_triggers": [],
                    "research": resolution_by_symbol[symbol]["research"],
                    "prior_rationale": request.rationale,
                }
                for symbol, request in needs.items()
            },
            allow_context_request=False,
            timeout_s=180,
        )
    else:
        second = {}

    final_by_symbol = {
        symbol: second.get(symbol, response)
        for symbol, response in first.items()
    }
    result = {
        "probe": "context_request",
        "protocol": {
            "symbols": SYMBOLS,
            "runtime_interval": RUNTIME_INTERVAL,
            "runtime_lookback": RUNTIME_LOOKBACK,
            "window": WINDOW,
            "max_context_requests_per_symbol": MAX_CONTEXT_REQUESTS_PER_SYMBOL,
            "max_indicators_per_request": MAX_INDICATORS_PER_REQUEST,
            "missing_but_useful": MISSING_BUT_USEFUL,
            "cockpit_cols": ctx["cockpit"]["cols"],
            "missing_indicator_check": {
                name: cockpit_is_missing_indicator(ctx["cockpit"]["cols"], name)
                for name in MISSING_BUT_USEFUL
            },
        },
        "first_batch": {
            symbol: response_summary(response)
            for symbol, response in first.items()
        },
        "context_resolution": resolution_by_symbol,
        "second_batch": {
            symbol: response_summary(response)
            for symbol, response in second.items()
        },
        "final_decisions": {
            symbol: response_summary(response)
            for symbol, response in final_by_symbol.items()
        },
        "metrics": aggregate_metrics(first, resolution_by_symbol, second),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
