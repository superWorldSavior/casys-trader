"""Small counterfactual model bench over audited decision rows."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import math
import os
import time
from typing import Any, Callable

from trader import llm
from trader.agent_context import build_market_cockpit
from trader.market import family_regime
from trader.reporting import decision_audit

VALID_ACTIONS = {"BUY", "SELL", "HOLD"}
DEFAULT_VERDICTS = {"good", "bad", "missed", "neutral"}
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


CompleteFn = Callable[[ModelSpec, str, int], ModelCompletion | ModelBenchFailure]


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
        if provider not in {"acpx", "ollama-cloud"}:
            raise ValueError(f"unsupported bench provider: {provider}")
        if not model:
            raise ValueError(f"missing model in spec {item!r}")
        specs.append(ModelSpec(provider=provider, model=model))
    if not specs:
        raise ValueError("at least one model spec is required")
    return specs


def parse_verdicts(raw: str | None) -> set[str]:
    if not raw:
        return set(DEFAULT_VERDICTS)
    return {item.strip() for item in raw.split(",") if item.strip()}


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


def _bench_case(row: dict, *, include_original: bool) -> dict:
    case = {
        "decision_id": row.get("decision_id"),
        "cycle_ts": row.get("cycle_ts"),
        "symbol": row.get("symbol"),
        "price": row.get("price"),
        "portfolio_snapshot": row.get("portfolio_snapshot") or {},
        "market_snapshot": row.get("market_snapshot") or {},
        "runtime": row.get("runtime") or {},
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
) -> list[dict]:
    cases: list[dict] = []
    skipped = 0
    offset = max(0, int(offset))
    for row in audit_payload.get("rows", []):
        if not isinstance(row, dict):
            continue
        if symbol and row.get("symbol") != symbol:
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


def build_prompt(cases: list[dict], *, horizon: str, threshold_pct: float) -> str:
    prompt_cases = [case["case"] for case in cases]
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
        '"rationale":"court"}]}\n\n'
        f"# Cas\n{json.dumps(prompt_cases, ensure_ascii=False, sort_keys=True)}\n"
    )


def reconstruct_case_contexts(
    cases: list[dict],
    *,
    history: Any,
    symbols: list[str],
    interval: str,
    lookback_bars: int,
    cockpit_window: int,
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
) -> tuple[Any, dict]:
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


def _maybe_reconstruct_cases(
    cases: list[dict],
    *,
    context_history: Any | None,
    context_symbols: list[str] | None,
    context_interval: str,
    context_lookback_bars: int,
    cockpit_window: int,
    context_metadata: dict | None,
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
    return (
        reconstruct_case_contexts(
            cases,
            history=context_history,
            symbols=symbols,
            interval=context_interval,
            lookback_bars=context_lookback_bars,
            cockpit_window=cockpit_window,
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


def _reviews_by_id(text: str) -> dict[str, dict]:
    payload = _extract_json_object(text)
    reviews = payload.get("reviews")
    if not isinstance(reviews, list):
        raise ValueError("JSON object must contain a reviews array")
    result: dict[str, dict] = {}
    for item in reviews:
        if not isinstance(item, dict):
            continue
        decision_id = item.get("decision_id")
        if decision_id:
            result[str(decision_id)] = item
    return result


def _candidate_verdict(action: str, future_return_pct: float | None, threshold_pct: float) -> str:
    if future_return_pct is None:
        return "unknown"
    return decision_audit._verdict(action, future_return_pct, threshold_pct)  # noqa: SLF001


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
        audit = case["audit"]
        future_return_pct = _finite_float(audit.get("future_return_pct"))
        verdict = _candidate_verdict(action, future_return_pct, threshold_pct)
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
                "candidate_verdict": verdict,
                "confidence": confidence,
                "same_as_original": same,
                "rationale": review.get("rationale"),
            }
        )

    denominator = max(1, counts["parsed"] - counts["invalid"])
    summary = {
        **counts,
        "good_pct": round(counts["good"] / denominator * 100.0, 2),
        "bad_pct": round(counts["bad"] / denominator * 100.0, 2),
        "missed_pct": round(counts["missed"] / denominator * 100.0, 2),
        "same_as_original_pct": round(counts["same_as_original"] / max(1, counts["parsed"]) * 100.0, 2),
        "avg_confidence": (sum(confidences) / len(confidences)) if confidences else None,
    }
    return rows, summary


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
    context_history: Any | None = None,
    context_symbols: list[str] | None = None,
    context_interval: str = "15m",
    context_lookback_bars: int = 160,
    cockpit_window: int = 48,
    context_metadata: dict | None = None,
    complete: CompleteFn = complete_model,
) -> dict:
    threshold_pct = float(audit_payload.get("threshold_pct") or 0.5)
    cases = select_cases(
        audit_payload,
        horizon=horizon,
        limit=limit,
        offset=offset,
        verdicts=verdicts,
        symbol=symbol,
        include_original=include_original,
    )
    cases, context_reconstruction = _maybe_reconstruct_cases(
        cases,
        context_history=context_history,
        context_symbols=context_symbols,
        context_interval=context_interval,
        context_lookback_bars=context_lookback_bars,
        cockpit_window=cockpit_window,
        context_metadata=context_metadata,
    )
    prompt = build_prompt(cases, horizon=horizon, threshold_pct=threshold_pct)
    results = []
    for spec in models:
        completion = complete(spec, prompt, timeout_s)
        model_row: dict[str, Any] = {
            "provider": completion.provider,
            "model": completion.model,
            "latency_s": completion.latency_s,
        }
        if isinstance(completion, ModelBenchFailure):
            model_row["failure"] = {
                "code": completion.code,
                "message": completion.message,
            }
            model_row["summary"] = {
                "total": len(cases),
                "parsed": 0,
                "missing": len(cases),
                "invalid": 0,
                "good": 0,
                "bad": 0,
                "neutral": 0,
                "missed": 0,
            }
            model_row["reviews"] = []
        else:
            try:
                reviews, summary = score_model_reviews(cases, completion.text, threshold_pct=threshold_pct)
                model_row["summary"] = summary
                model_row["reviews"] = reviews
            except Exception as exc:  # noqa: BLE001
                model_row["failure"] = {"code": "parse_failed", "message": str(exc)}
                model_row["summary"] = {
                    "total": len(cases),
                    "parsed": 0,
                    "missing": len(cases),
                    "invalid": 0,
                    "good": 0,
                    "bad": 0,
                    "neutral": 0,
                    "missed": 0,
                }
                model_row["raw_text"] = completion.text[:2000]
                model_row["reviews"] = []
        results.append(model_row)

    return {
        "horizon": horizon,
        "threshold_pct": threshold_pct,
        "limit": limit,
        "offset": offset,
        "verdicts": sorted(verdicts),
        "include_original": include_original,
        "context_reconstruction": context_reconstruction,
        "cases": cases,
        "prompt": prompt,
        "models": results,
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
    context_history: Any | None = None,
    context_symbols: list[str] | None = None,
    context_interval: str = "15m",
    context_lookback_bars: int = 160,
    cockpit_window: int = 48,
    context_metadata: dict | None = None,
) -> dict:
    threshold_pct = float(audit_payload.get("threshold_pct") or 0.5)
    cases = select_cases(
        audit_payload,
        horizon=horizon,
        limit=limit,
        offset=offset,
        verdicts=verdicts,
        symbol=symbol,
        include_original=include_original,
    )
    cases, context_reconstruction = _maybe_reconstruct_cases(
        cases,
        context_history=context_history,
        context_symbols=context_symbols,
        context_interval=context_interval,
        context_lookback_bars=context_lookback_bars,
        cockpit_window=cockpit_window,
        context_metadata=context_metadata,
    )
    return {
        "dry_run": True,
        "horizon": horizon,
        "threshold_pct": threshold_pct,
        "limit": limit,
        "offset": offset,
        "verdicts": sorted(verdicts),
        "include_original": include_original,
        "context_reconstruction": context_reconstruction,
        "models": [{"provider": spec.provider, "model": spec.model} for spec in models],
        "cases": cases,
        "prompt": build_prompt(cases, horizon=horizon, threshold_pct=threshold_pct),
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
