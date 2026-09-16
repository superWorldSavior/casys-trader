"""Small counterfactual model bench over audited decision rows."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import os
import time
from typing import Any

from trader.agent import llm
from trader.agent.context import build_market_cockpit
from trader.agent.memory import Memory
from trader.agent.protocol.prompts import (
    _SYMBOL_CALLS_FINAL_CONTRACT,
    _decision_guidance,
    _indicator_watch_vocabulary,
    _symbol_calls_exit_details,
)
from trader.application.execute import risk_capacity as risk_capacity_mod
from trader.domain.market import family_regime, fx
from trader.domain.risk import RiskLimits
from trader.reporting.audit import decision_quality as decision_audit
from trader.reporting.bench.protocols import BenchHistory, ModelBenchCompleter

VALID_ACTIONS = {"BUY", "SELL", "HOLD"}
VALID_CONTRACTS = {"reviews", "production"}
DOCTRINE_SECTION_NAMES = ("mandate", "memory", "guidance", "vocabulary")
# Runtime ledger keys which never derive from the original decision. Everything
# else (armed_plan_order, tool_calls, exit plans, watches, sizing…) is the
# original decision's own trace and must not reach the candidate model.
_BENCH_RUNTIME_ALLOWLIST = frozenset({"data_source", "dry_run"})
DEFAULT_VERDICTS = {"good", "bad", "missed", "neutral"}
AUDIT_STALE_AFTER_DAYS = 7
DEFAULT_BENCH_MODELS = [
    "acpx:gpt-5.3-codex-spark",
    "acpx:gpt-5.5",
    "ollama-cloud:nemotron-3-super:cloud",
    "ollama-cloud:glm-5.1:cloud",
]


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str


@dataclass(frozen=True)
class ModelCompletion:
    provider: str
    model: str
    text: str
    latency_s: float


@dataclass(frozen=True)
class ModelBenchFailure:
    provider: str
    model: str
    code: str
    message: str
    latency_s: float


CompleteFn = ModelBenchCompleter


def _unique_symbols(symbols: list[str] | tuple[str, ...]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = str(raw or "").strip()
        if symbol and symbol not in seen:
            out.append(symbol)
            seen.add(symbol)
    return out


def parse_model_specs(raw_specs: list[str] | str | None) -> list[ModelSpec]:
    if raw_specs is None:
        raw_specs = DEFAULT_BENCH_MODELS
    if isinstance(raw_specs, str):
        raw_specs = [item.strip() for item in raw_specs.split(",") if item.strip()]
    specs: list[ModelSpec] = []
    for raw in raw_specs:
        item = str(raw).strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(f"model spec must be provider:model, got {item!r}")
        provider, model = item.split(":", 1)
        provider = provider.strip()
        model = model.strip()
        if provider == "spark":
            provider = "acpx"
        if provider == "ollama":
            provider = "ollama-cloud"
        if provider == "grok":
            provider = "grok-build"
        if provider not in {"acpx", "ollama-cloud", "grok-build", "muse"}:
            raise ValueError(f"unsupported bench provider: {provider}")
        if not model:
            raise ValueError(f"missing model in spec {item!r}")
        if provider == "muse":
            llm.parse_muse_model_spec(model)
        specs.append(ModelSpec(provider=provider, model=model))
    if not specs:
        raise ValueError("at least one model spec is required")
    return specs


def parse_verdicts(raw: str | None) -> set[str]:
    if not raw:
        return set(DEFAULT_VERDICTS)
    return {item.strip() for item in raw.split(",") if item.strip()}


def parse_actions(raw: str | None) -> set[str] | None:
    """Filtre d'actions originales. Vide = aucun filtre (toutes les actions)."""

    if not raw or not str(raw).strip():
        return None
    actions = {item.strip().upper() for item in str(raw).split(",") if item.strip()}
    unknown = actions - VALID_ACTIONS
    if unknown:
        raise ValueError(f"actions inconnues : {sorted(unknown)}, attendues parmi {sorted(VALID_ACTIONS)}")
    return actions


def parse_contract(raw: str | None) -> str:
    contract = str(raw or "reviews").strip().lower()
    if contract not in VALID_CONTRACTS:
        raise ValueError(f"contract must be reviews or production, got {raw!r}")
    return contract


def parse_batch_size(raw: object, *, n_cases: int) -> int:
    """Cases per model call. Default 1. ``0`` = all cases in one prompt (legacy)."""
    if raw is None:
        return 1
    try:
        size = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"batch_size must be an int, got {raw!r}") from exc
    if size == 0:
        return max(1, int(n_cases) or 1)
    if size < 0:
        raise ValueError(f"batch_size must be >= 0, got {size}")
    return size


def _case_batches(cases: list[dict], batch_size: int) -> list[list[dict]]:
    size = parse_batch_size(batch_size, n_cases=len(cases))
    if not cases:
        return []
    return [cases[index : index + size] for index in range(0, len(cases), size)]


def _audit_for(row: dict, horizon: str) -> dict | None:
    audits = row.get("audits")
    if not isinstance(audits, dict):
        return None
    audit = audits.get(horizon)
    return audit if isinstance(audit, dict) else None


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _sanitize_runtime(runtime: object) -> dict:
    if not isinstance(runtime, dict):
        return {}
    return {key: value for key, value in runtime.items() if key in _BENCH_RUNTIME_ALLOWLIST}


def load_planner_doctrine(root: object) -> dict[str, str]:
    """Live planner doctrine (mandate, memory, guidance, vocabulary) for bench prompts.

    Same sources as the daemon; guidance and vocabulary match the no-tools bench
    shape. Fail-closed: a partial doctrine would silently bench a different brain.
    """

    from pathlib import Path

    repo = Path(str(root))
    memory = Memory(repo / "mandate" / "mandate.md", repo / "mandate" / "memory.md")
    doctrine = {
        "mandate": memory.read_mandate(),
        "memory": memory.read_memory(),
        "guidance": _decision_guidance(allow_context_request=False, allow_tool_calls=False),
        "vocabulary": _indicator_watch_vocabulary(),
    }
    missing = [name for name in DOCTRINE_SECTION_NAMES if not str(doctrine[name] or "").strip()]
    if missing:
        raise ValueError(f"doctrine incomplète sous {repo}: sections vides : {', '.join(missing)}")
    return doctrine


def _require_doctrine(doctrine: dict[str, str] | None) -> dict[str, str] | None:
    if doctrine is None:
        return None
    missing = [name for name in DOCTRINE_SECTION_NAMES if not str(doctrine.get(name) or "").strip()]
    if missing:
        raise ValueError(f"doctrine incomplète : sections vides : {', '.join(missing)}")
    return doctrine


def _bench_case(row: dict, *, include_original: bool) -> dict:
    case = {
        "decision_id": row.get("decision_id"),
        "cycle_ts": row.get("cycle_ts"),
        "symbol": row.get("symbol"),
        "price": row.get("price"),
        "portfolio_snapshot": row.get("portfolio_snapshot") or {},
        "market_snapshot": row.get("market_snapshot") or {},
        "runtime": _sanitize_runtime(row.get("runtime")),
    }
    if include_original:
        case["original_decision"] = {
            "action": row.get("action"),
            "intent": row.get("intent"),
            "qty": row.get("qty"),
            "confidence": row.get("confidence"),
            "rationale": row.get("rationale"),
            "reason": row.get("reason"),
            "executed": row.get("executed"),
            "llm_provider": row.get("llm_provider"),
            "llm_model": row.get("llm_model"),
        }
    return case


def select_cases(
    audit_payload: dict,
    *,
    horizon: str,
    limit: int,
    offset: int = 0,
    verdicts: set[str],
    symbol: str | None = None,
    include_original: bool = True,
    actions: set[str] | None = None,
) -> list[dict]:
    cases: list[dict] = []
    skipped = 0
    offset = max(0, int(offset))
    for row in audit_payload.get("rows", []):
        if not isinstance(row, dict):
            continue
        if symbol and row.get("symbol") != symbol:
            continue
        if actions is not None and str(row.get("action") or "").upper() not in actions:
            continue
        audit = _audit_for(row, horizon)
        if audit is None:
            continue
        verdict = str(audit.get("verdict") or "unknown")
        if verdict not in verdicts:
            continue
        if _finite_float(audit.get("future_return_pct")) is None:
            continue
        if skipped < offset:
            skipped += 1
            continue
        cases.append(
            {
                "decision_id": row.get("decision_id"),
                "symbol": row.get("symbol"),
                "cycle_ts": row.get("cycle_ts"),
                "original_action": row.get("action"),
                "audit": {
                    "horizon": horizon,
                    "verdict": verdict,
                    "entry_price": audit.get("entry_price"),
                    "future_price": audit.get("future_price"),
                    "future_return_pct": audit.get("future_return_pct"),
                },
                "case": _bench_case(row, include_original=include_original),
            }
        )
        if len(cases) >= limit:
            break
    return cases


def build_prompt(
    cases: list[dict],
    *,
    horizon: str,
    threshold_pct: float,
    contract: str = "reviews",
    doctrine: dict[str, str] | None = None,
) -> str:
    contract = parse_contract(contract)
    prompt_cases = [case["case"] for case in cases]
    cases_json = json.dumps(prompt_cases, ensure_ascii=False, sort_keys=True)
    if contract == "production":
        resolved = _require_doctrine(doctrine)
        doctrine_block = ""
        exit_details = ""
        if resolved is not None:
            doctrine_block = (
                f"# Mandat\n{resolved['mandate']}\n\n"
                f"# Mémoire / stratégie\n{resolved['memory']}\n\n"
                f"{resolved['guidance']}"
                f"{resolved['vocabulary']}"
            )
            exit_details = _symbol_calls_exit_details()
        return (
            "Tu es un planificateur de trading en bench hors-ligne.\n"
            "Tu reçois des décisions historiques telles qu'elles étaient connues à l'instant T. "
            "Les données futures sont volontairement masquées.\n"
            "Pour chaque cas, rends la décision live que tu aurais prise à l'instant T "
            "avec le contrat planner Pine-like (`calls`), pas un avis BUY/SELL/HOLD.\n"
            f"L'évaluation locale utilisera ensuite l'horizon {horizon} et un seuil de {threshold_pct:.4f}% ; "
            "ne cherche pas à deviner des prix.\n\n"
            "Réponds UNIQUEMENT par un objet JSON valide au format:\n"
            '{"decisions":[{"decision_id":"<id>","symbol":"<SYM>","confidence":0.0,'
            '"rationale":"court","opportunity_side":"long|short|null",'
            '"decision_reason_code":"<code>","calls":[...] }]}\n'
            "Inclus `decision_id` de chaque cas. `calls: []` = HOLD explicite.\n\n"
            f"{doctrine_block}"
            f"# Contrat de sortie\n{_SYMBOL_CALLS_FINAL_CONTRACT}{exit_details}\n\n"
            f"# Cas\n{cases_json}\n"
        )
    return (
        "Tu es un planificateur de trading en bench hors-ligne.\n"
        "Tu reçois des décisions historiques telles qu'elles étaient connues à l'instant T. "
        "Les données futures sont volontairement masquées.\n"
        "Pour chaque cas, choisis ce que tu aurais fait à l'instant T: BUY, SELL ou HOLD. "
        "Tu peux conserver ou contredire la décision originale si elle est fournie.\n"
        f"L'évaluation locale utilisera ensuite l'horizon {horizon} et un seuil de {threshold_pct:.4f}% ; "
        "ne cherche pas à deviner des prix, donne seulement ton action.\n\n"
        "Réponds UNIQUEMENT par un objet JSON valide au format:\n"
        '{"reviews":[{"decision_id":"<id>","action":"BUY|SELL|HOLD","confidence":0.0,'
        '"opportunity_side":"long|short|null","rationale":"court"}]}\n'
        "Pour HOLD, `opportunity_side` nomme la thèse précise refusée (`long` ou "
        "`short`), ou vaut null s'il n'existe réellement aucune thèse directionnelle.\n\n"
        f"# Cas\n{cases_json}\n"
    )


def reconstruct_case_contexts(
    cases: list[dict],
    *,
    history: BenchHistory,
    symbols: list[str],
    interval: str,
    lookback_bars: int,
    cockpit_window: int,
    risk_limits: RiskLimits | None = None,
    fx_history: dict[str, list[tuple[str, float]]] | None = None,
) -> list[dict]:
    """Injecte un contexte as-of reconstruit, sans barres brutes ni futur."""
    universe_symbols = _unique_symbols(symbols)
    enriched: list[dict] = []

    for case in cases:
        item = copy.deepcopy(case)
        cycle_ts = str(item.get("cycle_ts") or "")
        case_symbol = str(item.get("symbol") or "").strip()
        requested_symbols = _unique_symbols([*universe_symbols, case_symbol])

        bars_by_symbol: dict[str, list[object]] = {}
        prices: dict[str, float] = {}
        latest_bar_ts: dict[str, str] = {}
        missing_symbols: list[str] = []
        for symbol in requested_symbols:
            bars = list(history.bars_asof(symbol, cycle_ts, lookback_bars)) if cycle_ts else []
            price = history.price_asof(symbol, cycle_ts) if cycle_ts else None
            if bars:
                bars_by_symbol[symbol] = bars
                latest_bar_ts[symbol] = str(bars[-1].ts)
            else:
                missing_symbols.append(symbol)
            if price is not None:
                prices[symbol] = round(float(price), 6)

        cockpit_symbols = [symbol for symbol in requested_symbols if symbol in bars_by_symbol or symbol in prices]
        active_families = family_regime.families_for_universe(cockpit_symbols)
        reconstructed_context = {
            "source": "history_asof_reconstruction",
            "exact_replay": False,
            "interval": interval,
            "lookback_bars": lookback_bars,
            "cockpit_window": cockpit_window,
            "symbols": cockpit_symbols,
            "missing_symbols": missing_symbols,
            "latest_bar_ts": latest_bar_ts,
            "prices": prices,
            "cockpit": build_market_cockpit(
                bars_by_symbol,
                symbols=cockpit_symbols,
                prices=prices,
                window=cockpit_window,
            ),
            "regime_families": family_regime.compute_family_bias(
                {
                    symbol: family_regime.momentum_from_bars(bars)
                    for symbol, bars in bars_by_symbol.items()
                },
                active_families,
            ),
        }
        if risk_limits is not None and fx_history is not None:
            reconstructed_context["risk_capacity"] = reconstruct_risk_capacity(
                item.get("case", {}),
                cycle_ts,
                prices,
                limits=risk_limits,
                fx_history=fx_history,
            )
        item.setdefault("case", {})["reconstructed_context"] = reconstructed_context
        enriched.append(item)

    return enriched


def _parse_case_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def load_reconstruction_history(
    cases: list[dict],
    *,
    symbols: list[str],
    interval: str,
    padding_days: int,
) -> tuple[BenchHistory, dict]:
    """Charge un historique as-of symbole par symbole pour isoler les trous data."""
    from backtest.data import DataError, HistoryStore

    requested_symbols = _unique_symbols(symbols)
    case_times = [_parse_case_datetime(case.get("cycle_ts")) for case in cases]
    valid_times = [ts for ts in case_times if ts is not None]
    metadata = {
        "enabled": True,
        "source": "history_asof_reconstruction",
        "exact_replay": False,
        "interval": interval,
        "symbols": requested_symbols,
        "loaded_symbols": [],
        "unavailable_symbols": [],
        "window": None,
    }
    if not requested_symbols or not valid_times:
        return HistoryStore.from_bars({}), metadata

    start = (min(valid_times) - timedelta(days=padding_days)).date().isoformat()
    end = (max(valid_times) + timedelta(days=1)).date().isoformat()
    metadata["window"] = {"start": start, "end": end, "padding_days": padding_days}

    bars_by_symbol: dict[str, list[object]] = {}
    unavailable_symbols: list[str] = []
    for symbol in requested_symbols:
        try:
            symbol_history = HistoryStore.load([symbol], start, end, interval=interval)
        except DataError:
            unavailable_symbols.append(symbol)
            continue
        bars = list(symbol_history._bars_by_symbol.get(symbol, ()))  # noqa: SLF001 - merge stores.
        if bars:
            bars_by_symbol[symbol] = bars

    metadata["loaded_symbols"] = sorted(bars_by_symbol)
    metadata["unavailable_symbols"] = unavailable_symbols
    return HistoryStore.from_bars(bars_by_symbol), metadata


# Risk caps served while the audited decisions ran (config/risk.yaml unchanged
# Jul 11 → Aug 31 2026, git 11e19f3..ef344af ; audit rows are Aug 1–28).
# Deliberately NOT read from the working tree: current caps (25k) would
# understate the August sizing room by half.
AUDIT_WINDOW_RISK_LIMITS = RiskLimits(
    max_position_value=50000.0,
    max_gross_exposure=100000.0,
    max_order_value=50000.0,
    min_equity=50000.0,
)
AUDIT_WINDOW_RISK_LIMITS_SOURCE = "config/risk.yaml Jul 11-Aug 31 2026 (git 11e19f3..ef344af)"

# yfinance ticker per currency, with USD-per-unit conversion. USD needs no ticker.
_FX_TICKERS: dict[str, tuple[str, bool]] = {
    "EUR": ("EURUSD=X", False),
    "GBP": ("GBPUSD=X", False),
    "TWD": ("USDTWD=X", True),
    "CHF": ("USDCHF=X", True),
    "NOK": ("USDNOK=X", True),
    "SEK": ("USDSEK=X", True),
    "DKK": ("USDDKK=X", True),
}


def load_fx_history(
    cases: list[dict],
    *,
    padding_days: int,
) -> tuple[dict[str, list[tuple[str, float]]], dict]:
    """Charge les taux USD-par-unité as-of, ticker par ticker (trous isolés)."""

    from backtest.data import DataError, HistoryStore

    ccys = sorted(
        {fx.currency_for(str(case.get("symbol") or "")) for case in cases if case.get("symbol")}
        - {"USD"}
    )
    case_times = [_parse_case_datetime(case.get("cycle_ts")) for case in cases]
    valid_times = [ts for ts in case_times if ts is not None]
    metadata = {"tickers": {}, "window": None}
    if not ccys or not valid_times:
        return {}, metadata
    start = (min(valid_times) - timedelta(days=padding_days)).date().isoformat()
    end = (max(valid_times) + timedelta(days=1)).date().isoformat()
    metadata["window"] = {"start": start, "end": end, "padding_days": padding_days}
    rates: dict[str, list[tuple[str, float]]] = {}
    for ccy in ccys:
        ticker, invert = _FX_TICKERS.get(ccy, (None, False))
        if ticker is None:
            metadata["tickers"][ccy] = {"ticker": None, "status": "unsupported_currency"}
            continue
        try:
            store = HistoryStore.load([ticker], start, end, interval="1d")
        except DataError as exc:
            metadata["tickers"][ccy] = {"ticker": ticker, "status": f"unavailable: {exc}"}
            continue
        points = []
        for bar in store._bars_by_symbol.get(ticker, ()):  # noqa: SLF001 - read-only merge.
            close = _finite_float(bar.close)
            if close:
                points.append((str(bar.ts), (1.0 / close) if invert else close))
        points.sort()
        rates[ccy] = points
        metadata["tickers"][ccy] = {"ticker": ticker, "status": "ok", "points": len(points)}
    return rates, metadata


def fx_rate_asof(
    fx_history: dict[str, list[tuple[str, float]]],
    currency: str,
    cycle_ts: str,
) -> float | None:
    """Dernier taux USD-par-unité connu à l'instant T (clôture daily)."""

    if currency == "USD":
        return 1.0
    best: float | None = None
    for ts, rate in fx_history.get(currency, []):
        if ts <= cycle_ts:
            best = rate
        else:
            break
    return best


def reconstruct_risk_capacity(
    case: dict,
    cycle_ts: str,
    prices: dict[str, float],
    *,
    limits: RiskLimits,
    fx_history: dict[str, list[tuple[str, float]]],
) -> dict:
    """Rebuild the sizing caps the live brain saw, from persisted snapshots.

    Positions, price, gross and equity are the T-time persisted values; only
    the FX rate is re-fetched (daily close as-of). Same pure function as live.
    """

    from types import SimpleNamespace

    symbol = str(case.get("symbol") or "").strip()
    snapshot = case.get("portfolio_snapshot") or {}
    holdings = snapshot.get("holdings") if isinstance(snapshot, dict) else None
    positions: dict[str, object] = {}
    if isinstance(holdings, list):
        for holding in holdings:
            if not isinstance(holding, dict):
                continue
            name = str(holding.get("symbol") or "").strip()
            quantity = _finite_float(holding.get("quantity"))
            if name and quantity is not None:
                positions[name] = SimpleNamespace(quantity=quantity)
    elif isinstance(holdings, dict):
        for name, holding in holdings.items():
            quantity = _finite_float(holding.get("quantity") if isinstance(holding, dict) else holding)
            if str(name).strip() and quantity is not None:
                positions[str(name).strip()] = SimpleNamespace(quantity=quantity)
    price = _finite_float(case.get("price"))
    if price is None:
        price = _finite_float(prices.get(symbol))
    broker = SimpleNamespace(positions=lambda: positions)
    gross = _finite_float(snapshot.get("gross_exposure_usd")) if isinstance(snapshot, dict) else None
    if gross is None:
        gross = risk_capacity_mod.gross_exposure(
            broker,
            {symbol: price} if price is not None else {},
            rate_of=lambda sym: fx_rate_asof(fx_history, fx.currency_for(sym), cycle_ts),
        )
    equity = _finite_float(snapshot.get("equity")) if isinstance(snapshot, dict) else None
    return risk_capacity_mod.risk_capacity_context(
        symbols=[symbol],
        prices={symbol: price} if price is not None else {},
        broker=broker,
        gross_exposure=gross,
        limits=limits,
        equity=equity if equity is not None else 0.0,
        rate_of=lambda sym: fx_rate_asof(fx_history, fx.currency_for(sym), cycle_ts),
        currency_of=fx.currency_for,
    )


def _maybe_reconstruct_cases(
    cases: list[dict],
    *,
    context_history: BenchHistory | None,
    context_symbols: list[str] | None,
    context_interval: str,
    context_lookback_bars: int,
    cockpit_window: int,
    context_metadata: dict | None,
    risk_limits: RiskLimits | None = None,
    fx_history: dict[str, list[tuple[str, float]]] | None = None,
    fx_metadata: dict | None = None,
) -> tuple[list[dict], dict]:
    if context_history is None:
        return cases, {"enabled": False}

    symbols = _unique_symbols(context_symbols or [str(case.get("symbol") or "") for case in cases])
    metadata = dict(context_metadata or {})
    metadata.setdefault("enabled", True)
    metadata.setdefault("source", "history_asof_reconstruction")
    metadata.setdefault("exact_replay", False)
    metadata.setdefault("interval", context_interval)
    metadata.setdefault("symbols", symbols)
    metadata["lookback_bars"] = context_lookback_bars
    metadata["cockpit_window"] = cockpit_window
    capacity_on = risk_limits is not None and fx_history is not None
    metadata["risk_capacity"] = {
        "enabled": capacity_on,
        "limits_source": AUDIT_WINDOW_RISK_LIMITS_SOURCE if capacity_on else None,
        "fx": fx_metadata or {},
    }
    return (
        reconstruct_case_contexts(
            cases,
            history=context_history,
            symbols=symbols,
            interval=context_interval,
            lookback_bars=context_lookback_bars,
            cockpit_window=cockpit_window,
            risk_limits=risk_limits,
            fx_history=fx_history,
        ),
        metadata,
    )


def _extract_json_object(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object found")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be an object")
    return payload


def _action_from_production_calls(calls: Any) -> str:
    """Mappe le contrat live vers BUY/SELL/HOLD.

    `strategy_close` → SELL (exit), y compris couverture d'un short.
    Veilles (`set_next_wake`, `propose_indicator_watch` WAKE) → HOLD.
    """
    if calls is None:
        return "HOLD"
    if not isinstance(calls, list):
        return ""
    entry_action: str | None = None
    execute_action: str | None = None
    saw_close = False
    for raw in calls:
        if not isinstance(raw, dict):
            continue
        tool = str(raw.get("tool") or "").strip()
        args = raw.get("args") if isinstance(raw.get("args"), dict) else {}
        if tool == "strategy_entry" and entry_action is None:
            direction = str(args.get("direction") or "").strip().lower()
            if direction == "long":
                entry_action = "BUY"
            elif direction == "short":
                entry_action = "SELL"
        elif tool == "propose_indicator_watch" and execute_action is None:
            on_trigger = str(args.get("on_trigger") or "").strip().upper()
            if on_trigger == "EXECUTE_ORDER":
                order = args.get("order") if isinstance(args.get("order"), dict) else {}
                direction = str(order.get("direction") or "").strip().lower()
                if direction == "long":
                    execute_action = "BUY"
                elif direction == "short":
                    execute_action = "SELL"
        elif tool == "strategy_close":
            saw_close = True
    if entry_action:
        return entry_action
    if execute_action:
        return execute_action
    if saw_close:
        return "SELL"
    return "HOLD"


def _review_from_production_decision(item: dict) -> dict:
    calls = item.get("calls")
    return {
        "decision_id": item.get("decision_id"),
        "action": _action_from_production_calls(calls),
        "confidence": item.get("confidence"),
        "opportunity_side": item.get("opportunity_side"),
        "rationale": item.get("rationale"),
        "calls": list(calls) if isinstance(calls, list) else None,
        "decision_reason_code": item.get("decision_reason_code"),
    }


def _reviews_by_id(text: str) -> dict[str, dict]:
    payload = _extract_json_object(text)
    reviews = payload.get("reviews")
    decisions = payload.get("decisions")
    if isinstance(reviews, list):
        items = reviews
        as_review = None
    elif isinstance(decisions, list):
        items = decisions
        as_review = _review_from_production_decision
    else:
        raise ValueError("JSON object must contain a reviews or decisions array")
    result: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        review = as_review(item) if as_review is not None else item
        decision_id = review.get("decision_id")
        if decision_id:
            result[str(decision_id)] = review
    return result


def audit_freshness(audit_payload: dict, *, now: datetime | None = None) -> dict:
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    newest: datetime | None = None
    newest_raw: Any = None
    for row in audit_payload.get("rows") or []:
        if not isinstance(row, dict):
            continue
        parsed = _parse_case_datetime(row.get("cycle_ts"))
        if parsed is None:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if newest is None or parsed > newest:
            newest = parsed
            newest_raw = row.get("cycle_ts")
    if newest is None:
        return {"audit_stale": False, "audit_as_of": None}
    stale = newest < (clock - timedelta(days=AUDIT_STALE_AFTER_DAYS))
    return {"audit_stale": stale, "audit_as_of": newest_raw}


def _candidate_verdict(
    case: dict,
    action: str,
    opportunity_side: str | None,
    future_return_pct: float | None,
    threshold_pct: float,
) -> str:
    candidate_row = {
        "action": action,
        "symbol": case.get("symbol"),
        "opportunity_side": opportunity_side,
        "portfolio_snapshot": (case.get("case") or {}).get("portfolio_snapshot") or {},
    }
    verdict, _ = decision_audit.decision_verdict(
        candidate_row,
        future_return_pct,
        threshold_pct,
    )
    return verdict


def score_model_reviews(cases: list[dict], response_text: str, *, threshold_pct: float) -> tuple[list[dict], dict]:
    parsed = _reviews_by_id(response_text)
    rows: list[dict] = []
    counts = {
        "total": len(cases),
        "parsed": 0,
        "missing": 0,
        "invalid": 0,
        "good": 0,
        "bad": 0,
        "neutral": 0,
        "missed": 0,
        "unknown": 0,
        "same_as_original": 0,
    }
    confidences: list[float] = []

    for case in cases:
        review = parsed.get(str(case.get("decision_id")))
        if review is None:
            counts["missing"] += 1
            rows.append({"decision_id": case.get("decision_id"), "error": "missing_review"})
            continue
        counts["parsed"] += 1
        action = str(review.get("action") or "").upper()
        raw_opportunity_side = review.get("opportunity_side")
        opportunity_side = (
            str(raw_opportunity_side).strip().lower()
            if raw_opportunity_side is not None
            else None
        )
        confidence = _finite_float(review.get("confidence"))
        if confidence is not None:
            confidences.append(confidence)
        if action not in VALID_ACTIONS:
            counts["invalid"] += 1
            rows.append(
                {
                    "decision_id": case.get("decision_id"),
                    "candidate_action": action,
                    "error": "invalid_action",
                }
            )
            continue
        if opportunity_side not in {None, "long", "short"}:
            counts["invalid"] += 1
            rows.append(
                {
                    "decision_id": case.get("decision_id"),
                    "candidate_action": action,
                    "candidate_opportunity_side": opportunity_side,
                    "error": "invalid_opportunity_side",
                }
            )
            continue
        audit = case["audit"]
        future_return_pct = _finite_float(audit.get("future_return_pct"))
        verdict = _candidate_verdict(
            case,
            action,
            opportunity_side,
            future_return_pct,
            threshold_pct,
        )
        if verdict in counts:
            counts[verdict] += 1
        original_action = str(case.get("original_action") or "").upper()
        same = action == original_action
        if same:
            counts["same_as_original"] += 1
        rows.append(
            {
                "decision_id": case.get("decision_id"),
                "symbol": case.get("symbol"),
                "cycle_ts": case.get("cycle_ts"),
                "original_action": original_action,
                "original_verdict": audit.get("verdict"),
                "candidate_action": action,
                "candidate_opportunity_side": opportunity_side,
                "candidate_verdict": verdict,
                "confidence": confidence,
                "same_as_original": same,
                "rationale": review.get("rationale"),
                "candidate_calls": review.get("calls"),
                "candidate_decision_reason_code": review.get("decision_reason_code"),
            }
        )

    return rows, _summary_from_counts(counts, confidences)


def planner_summary(reviews: list[dict]) -> dict:
    """Aggregate production calls: tool usage, watches, reason codes.

    Reviews contract has no calls: counters stay empty, which is itself the signal.
    """

    tools: dict[str, int] = {}
    watches = {"armed": 0, "wake": 0, "other": 0}
    reason_codes: dict[str, int] = {}
    with_calls = 0
    for review in reviews:
        calls = review.get("candidate_calls")
        if isinstance(calls, list) and calls:
            with_calls += 1
        for call in calls or []:
            if not isinstance(call, dict):
                continue
            tool = str(call.get("tool") or "").strip() or "unknown"
            tools[tool] = tools.get(tool, 0) + 1
            if tool == "propose_indicator_watch":
                args = call.get("args") if isinstance(call.get("args"), dict) else {}
                trigger = str(args.get("on_trigger") or "").strip().upper()
                if trigger == "EXECUTE_ORDER":
                    watches["armed"] += 1
                elif trigger in {"WAKE", "WAKE_WITH_ORDER_INTENT"}:
                    watches["wake"] += 1
                else:
                    watches["other"] += 1
        code = review.get("candidate_decision_reason_code")
        if code is not None:
            key = str(code).strip() or "unknown"
            reason_codes[key] = reason_codes.get(key, 0) + 1
    return {
        "reviews_with_calls": with_calls,
        "tools": dict(sorted(tools.items())),
        "watches": watches,
        "reason_codes": dict(sorted(reason_codes.items())),
    }


def _summary_from_counts(counts: dict[str, int], confidences: list[float]) -> dict:
    valid = counts["parsed"] - counts["invalid"]
    scored = counts["good"] + counts["bad"] + counts["neutral"] + counts["missed"]
    denominator = max(1, scored)
    return {
        **counts,
        "scored": scored,
        "coverage_pct": round(scored / max(1, valid) * 100.0, 2),
        "good_pct": round(counts["good"] / denominator * 100.0, 2),
        "bad_pct": round(counts["bad"] / denominator * 100.0, 2),
        "missed_pct": round(counts["missed"] / denominator * 100.0, 2),
        "same_as_original_pct": round(counts["same_as_original"] / max(1, counts["parsed"]) * 100.0, 2),
        "avg_confidence": (sum(confidences) / len(confidences)) if confidences else None,
    }


def _merge_scored_reviews(chunk_results: list[tuple[list[dict], dict]]) -> tuple[list[dict], dict]:
    reviews: list[dict] = []
    counts = {
        "total": 0,
        "parsed": 0,
        "missing": 0,
        "invalid": 0,
        "good": 0,
        "bad": 0,
        "neutral": 0,
        "missed": 0,
        "unknown": 0,
        "same_as_original": 0,
    }
    confidences: list[float] = []
    for chunk_reviews, chunk_summary in chunk_results:
        reviews.extend(chunk_reviews)
        for key in counts:
            counts[key] += int(chunk_summary.get(key) or 0)
        for review in chunk_reviews:
            confidence = _finite_float(review.get("confidence"))
            if confidence is not None:
                confidences.append(confidence)
    return reviews, _summary_from_counts(counts, confidences)


def _ollama_backend(spec: ModelSpec) -> llm.OpenAICompatibleBackend | ModelBenchFailure:
    llm.load_dotenv()
    api_key = os.environ.get("TRADER_OLLAMA_API_KEY") or os.environ.get("OLLAMA_API_KEY")
    if not api_key:
        return ModelBenchFailure(
            provider=spec.provider,
            model=spec.model,
            code="missing_api_key",
            message="TRADER_OLLAMA_API_KEY or OLLAMA_API_KEY is required",
            latency_s=0.0,
        )
    return llm.OpenAICompatibleBackend(
        provider=spec.provider,
        api_key=api_key,
        base_url=(
            os.environ.get("TRADER_OLLAMA_BASE_URL")
            or os.environ.get("OLLAMA_CLOUD_BASE_URL")
            or os.environ.get("OLLAMA_BASE_URL")
            or llm.DEFAULT_OLLAMA_BASE_URL
        ),
        model=spec.model,
    )


def complete_model(spec: ModelSpec, prompt: str, timeout_s: int) -> ModelCompletion | ModelBenchFailure:
    if spec.provider == "acpx":
        backend: llm.LlmBackend | ModelBenchFailure = llm.AcpxBackend(
            provider="acpx",
            model=spec.model,
            session_label="casys-trader:decision-bench",
        )
    elif spec.provider == "grok-build":
        backend = llm.AcpxBackend(
            provider="grok-build",
            model=spec.model,
            agent="grok-build",
            session_label="casys-trader:decision-bench",
        )
    elif spec.provider == "muse":
        backend = llm.MuseBackend(
            provider="muse",
            model=spec.model,
        )
    else:
        backend = _ollama_backend(spec)
    if isinstance(backend, ModelBenchFailure):
        return backend

    t0 = time.time()
    result = backend.complete(prompt, timeout_s=timeout_s)
    latency_s = round(time.time() - t0, 3)
    if isinstance(result, llm.LlmFailure):
        return ModelBenchFailure(
            provider=result.provider,
            model=result.model,
            code=result.code,
            message=result.message,
            latency_s=latency_s,
        )
    return ModelCompletion(
        provider=result.provider,
        model=result.model,
        text=result.text,
        latency_s=latency_s,
    )


def run_bench(
    audit_payload: dict,
    *,
    models: list[ModelSpec],
    horizon: str,
    limit: int,
    offset: int = 0,
    verdicts: set[str],
    timeout_s: int,
    symbol: str | None = None,
    include_original: bool = True,
    actions: set[str] | None = None,
    contract: str = "reviews",
    batch_size: int | None = None,
    context_history: BenchHistory | None = None,
    context_symbols: list[str] | None = None,
    context_interval: str = "15m",
    context_lookback_bars: int = 160,
    cockpit_window: int = 48,
    context_metadata: dict | None = None,
    complete: CompleteFn = complete_model,
    now: datetime | None = None,
    doctrine: dict[str, str] | None = None,
    risk_limits: RiskLimits | None = None,
    fx_history: dict[str, list[tuple[str, float]]] | None = None,
    fx_metadata: dict | None = None,
) -> dict:
    contract = parse_contract(contract)
    resolved_doctrine = _require_doctrine(doctrine)
    threshold_pct = float(audit_payload.get("threshold_pct") or 0.5)
    cases = select_cases(
        audit_payload,
        horizon=horizon,
        limit=limit,
        offset=offset,
        verdicts=verdicts,
        symbol=symbol,
        include_original=include_original,
        actions=actions,
    )
    cases, context_reconstruction = _maybe_reconstruct_cases(
        cases,
        context_history=context_history,
        context_symbols=context_symbols,
        context_interval=context_interval,
        context_lookback_bars=context_lookback_bars,
        cockpit_window=cockpit_window,
        context_metadata=context_metadata,
        risk_limits=risk_limits,
        fx_history=fx_history,
        fx_metadata=fx_metadata,
    )
    batches = _case_batches(cases, parse_batch_size(batch_size, n_cases=len(cases)))
    first_prompt = (
        build_prompt(
            batches[0], horizon=horizon, threshold_pct=threshold_pct, contract=contract, doctrine=resolved_doctrine
        )
        if batches
        else build_prompt([], horizon=horizon, threshold_pct=threshold_pct, contract=contract, doctrine=resolved_doctrine)
    )
    results = []
    for spec in models:
        chunk_scores: list[tuple[list[dict], dict]] = []
        latencies: list[float] = []
        failure: dict[str, str] | None = None
        raw_text = ""
        for chunk in batches or [[]]:
            prompt = build_prompt(
                chunk, horizon=horizon, threshold_pct=threshold_pct, contract=contract, doctrine=resolved_doctrine
            )
            completion = complete(spec, prompt, timeout_s)
            latencies.append(float(completion.latency_s))
            if isinstance(completion, ModelBenchFailure):
                failure = {"code": completion.code, "message": completion.message}
                break
            try:
                chunk_scores.append(
                    score_model_reviews(chunk, completion.text, threshold_pct=threshold_pct)
                )
            except Exception as exc:  # noqa: BLE001
                failure = {"code": "parse_failed", "message": str(exc)}
                raw_text = completion.text[:2000]
                break
        reviews, summary = _merge_scored_reviews(chunk_scores) if chunk_scores else (
            [],
            _summary_from_counts(
                {
                    "total": len(cases),
                    "parsed": 0,
                    "missing": len(cases),
                    "invalid": 0,
                    "good": 0,
                    "bad": 0,
                    "neutral": 0,
                    "missed": 0,
                    "unknown": 0,
                    "same_as_original": 0,
                },
                [],
            ),
        )
        if failure is not None:
            summary["total"] = len(cases)
            summary["missing"] = max(summary["missing"], len(cases) - summary["parsed"])
        model_row: dict[str, Any] = {
            "provider": spec.provider,
            "model": spec.model,
            "latency_s": round(sum(latencies), 3),
            "n_calls": len(latencies),
            "summary": summary,
            "planner": planner_summary(reviews),
            "reviews": reviews,
        }
        if failure is not None:
            model_row["failure"] = failure
            if raw_text:
                model_row["raw_text"] = raw_text
        results.append(model_row)

    return {
        "horizon": horizon,
        "threshold_pct": threshold_pct,
        "limit": limit,
        "offset": offset,
        "verdicts": sorted(verdicts),
        "actions": sorted(actions) if actions else [],
        "include_original": include_original,
        "contract": contract,
        "batch_size": parse_batch_size(batch_size, n_cases=len(cases)),
        "n_calls": len(batches),
        "context_reconstruction": context_reconstruction,
        "prompt_fidelity": {
            "doctrine_sections": sorted(resolved_doctrine) if resolved_doctrine else [],
            "runtime_allowlist": sorted(_BENCH_RUNTIME_ALLOWLIST),
        },
        "cases": cases,
        "prompt": first_prompt,
        "models": results,
        **audit_freshness(audit_payload, now=now),
    }


def dry_run_payload(
    audit_payload: dict,
    *,
    models: list[ModelSpec],
    horizon: str,
    limit: int,
    offset: int = 0,
    verdicts: set[str],
    symbol: str | None = None,
    include_original: bool = True,
    actions: set[str] | None = None,
    contract: str = "reviews",
    batch_size: int | None = None,
    context_history: Any | None = None,
    context_symbols: list[str] | None = None,
    context_interval: str = "15m",
    context_lookback_bars: int = 160,
    cockpit_window: int = 48,
    context_metadata: dict | None = None,
    now: datetime | None = None,
    doctrine: dict[str, str] | None = None,
    risk_limits: RiskLimits | None = None,
    fx_history: dict[str, list[tuple[str, float]]] | None = None,
    fx_metadata: dict | None = None,
) -> dict:
    contract = parse_contract(contract)
    resolved_doctrine = _require_doctrine(doctrine)
    threshold_pct = float(audit_payload.get("threshold_pct") or 0.5)
    cases = select_cases(
        audit_payload,
        horizon=horizon,
        limit=limit,
        offset=offset,
        verdicts=verdicts,
        symbol=symbol,
        include_original=include_original,
        actions=actions,
    )
    cases, context_reconstruction = _maybe_reconstruct_cases(
        cases,
        context_history=context_history,
        context_symbols=context_symbols,
        context_interval=context_interval,
        context_lookback_bars=context_lookback_bars,
        cockpit_window=cockpit_window,
        context_metadata=context_metadata,
        risk_limits=risk_limits,
        fx_history=fx_history,
        fx_metadata=fx_metadata,
    )
    resolved_batch = parse_batch_size(batch_size, n_cases=len(cases))
    batches = _case_batches(cases, resolved_batch)
    first = batches[0] if batches else []
    return {
        "dry_run": True,
        "horizon": horizon,
        "threshold_pct": threshold_pct,
        "limit": limit,
        "offset": offset,
        "verdicts": sorted(verdicts),
        "actions": sorted(actions) if actions else [],
        "include_original": include_original,
        "contract": contract,
        "batch_size": resolved_batch,
        "n_calls": len(batches),
        "context_reconstruction": context_reconstruction,
        "prompt_fidelity": {
            "doctrine_sections": sorted(resolved_doctrine) if resolved_doctrine else [],
            "runtime_allowlist": sorted(_BENCH_RUNTIME_ALLOWLIST),
        },
        "models": [{"provider": spec.provider, "model": spec.model} for spec in models],
        "cases": cases,
        "prompt": build_prompt(
            first, horizon=horizon, threshold_pct=threshold_pct, contract=contract, doctrine=resolved_doctrine
        ),
        **audit_freshness(audit_payload, now=now),
    }


def render_summary(payload: dict) -> str:
    lines = [
        (
            f"Decision bench horizon={payload['horizon']} "
            f"cases={len(payload['cases'])} threshold={payload['threshold_pct']}%"
        ),
        f"{'provider':<14} {'model':<34} {'ok':>4} {'good%':>7} {'bad%':>6} {'missed%':>8} {'same%':>7} {'latency':>8}",
        "-" * 98,
    ]
    for item in payload.get("models", []):
        summary = item.get("summary") or {}
        failure = item.get("failure")
        ok = f"{summary.get('parsed', 0)}/{summary.get('total', 0)}"
        if failure:
            lines.append(
                f"{item.get('provider',''):<14} {item.get('model',''):<34} {ok:>4} "
                f"{'ERR':>7} {failure.get('code','')[:6]:>6} {'':>8} {'':>7} {item.get('latency_s', 0):>8}"
            )
            continue
        lines.append(
            f"{item.get('provider',''):<14} {item.get('model',''):<34} {ok:>4} "
            f"{summary.get('good_pct', 0):>6.2f}% {summary.get('bad_pct', 0):>5.2f}% "
            f"{summary.get('missed_pct', 0):>7.2f}% {summary.get('same_as_original_pct', 0):>6.2f}% "
            f"{item.get('latency_s', 0):>8.3f}"
        )
    return "\n".join(lines)
