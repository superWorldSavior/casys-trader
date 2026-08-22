#!/usr/bin/env python3
"""Offline ablation: does Universe context help predict Trader FLAIR?

The input is a JSONL export produced by ``brain_trajectory_spike.py``.  Both
``trajectories.jsonl`` and the richer ``cross_loop_episodes.jsonl`` schema are
accepted.  Only information available at decision time is featurised; the
Trader FLAIR verdict is used solely as the target.

This is deliberately a small, deterministic Trader-critic ablation.  It is not
a world model, a policy, or evidence that the mandate causes the later verdict.
It compares:

* a smoothed majority baseline;
* a Bernoulli Naive Bayes model over market, portfolio, decision, hypothesis,
  rationale-shape and tool-pattern features;
* the exact same model with the selected Universe mandate added.

WIN/LOSS are the only training labels.  NEUTRAL, UNKNOWN and pending outcomes
are excluded and reported.  The split is chronological and grouped by cycle,
with a 24-hour embargo for the counterfactual horizon and an exit-time purge
for realized flat-to-flat targets.

Realized and counterfactual rewards are never pooled in one fit.  The default
is ``--outcome-basis counterfactual``; realized trades are a separate run and
usually fail the sample-size gate on the current history.

Example::

    uv run python scripts/brain_flair_ablation.py /tmp/export/trajectories.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from trader.domain.decision_benchmark import BENCHMARK_SEMANTICS_VERSION
except ImportError:  # pragma: no cover - keeps an exported script inspectable alone
    BENCHMARK_SEMANTICS_VERSION = 3


LABELS = ("LOSS", "WIN")
SCHEMA_VERSION = "brain_flair_ablation_v1"
_MISSING = "<missing>"
_RATIONALE_CONCEPTS = {
    "breakout": ("breakout", "cassure"),
    "momentum": ("momentum", "impulsion"),
    "pullback": ("pullback", "retracement", "repli"),
    "range": ("range", "latéral", "lateral"),
    "risk": ("risk", "risque"),
    "stop": ("stop", "invalidat"),
    "trend": ("trend", "tendance"),
    "volume": ("volume", "rvol"),
}


@dataclass(frozen=True)
class Example:
    """One point-in-time-valid observational decision episode."""

    ordinal: int
    cycle_id: str
    symbol: str
    decision_id: str | None
    label: str
    outcome_basis: str
    base_features: frozenset[str]
    universe_features: frozenset[str]
    has_structured_thesis: bool
    has_rationale_text: bool
    has_tool_pattern: bool
    has_universe_context: bool
    universe_context_source: str
    universe_context_observed: bool
    realized_exit_at: datetime | None
    realized_close_evidence: str

    @property
    def enriched_features(self) -> frozenset[str]:
        return self.base_features | self.universe_features


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _first(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _path(value: Any, dotted: str) -> Any:
    current = value
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _first_path(row: Mapping[str, Any], paths: Sequence[str]) -> Any:
    return _first(*(_path(row, path) for path in paths))


def _slug(value: Any, *, limit: int = 64) -> str:
    text = str(value).strip().lower()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^a-z0-9_.:+/=-]", "", text)
    if not text:
        return _MISSING
    return text[:limit]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        parsed = datetime.fromisoformat(normalized)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bucket(value: Any, cuts: Sequence[float]) -> str:
    number = _number(value)
    if number is None:
        return _MISSING
    for cut in cuts:
        if number < cut:
            return f"<{cut:g}"
    return f">={cuts[-1]:g}" if cuts else "present"


def _count_bucket(value: int) -> str:
    if value <= 0:
        return "0"
    if value == 1:
        return "1"
    if value <= 3:
        return "2-3"
    if value <= 7:
        return "4-7"
    return "8+"


def _token(name: str, value: Any) -> str:
    if isinstance(value, bool):
        normalized = "true" if value else "false"
    elif value is None or value == "":
        normalized = _MISSING
    else:
        normalized = _slug(value)
    return f"{name}={normalized}"


def _find_key(value: Any, names: set[str], *, depth: int = 0) -> Any:
    """Find the first named field in a bounded projection, not in outcomes."""

    if depth > 4:
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in names and item is not None:
                return item
        for item in value.values():
            found = _find_key(item, names, depth=depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value[:20]:
            found = _find_key(item, names, depth=depth + 1)
            if found is not None:
                return found
    return None


def _decision(row: Mapping[str, Any]) -> dict[str, Any]:
    candidates = (
        row.get("trader_decision"),
        row.get("decision"),
        _path(row, "trader.decision"),
        _path(row, "trader_workflow.decision"),
    )
    for candidate in candidates:
        if isinstance(candidate, dict):
            nested = candidate.get("decision")
            if isinstance(nested, dict):
                # Root fields are durable ledger projections; nested fields add
                # rationale/thesis/tool detail when the richer export keeps it.
                return candidate | nested
            return candidate
    return {}


def _market_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    state = _dict(row.get("state_before"))
    candidates = (
        row.get("market_state"),
        row.get("market_context"),
        _path(row, "decision.market_snapshot"),
        state.get("per_symbol_facts"),
        _path(row, "trader_context.market"),
    )
    merged: dict[str, Any] = {}
    for candidate in reversed(candidates):
        if isinstance(candidate, dict):
            merged.update(candidate)
    return merged


def _portfolio_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    candidates = (
        _path(row, "state_before.shared_projection.portfolio"),
        _path(row, "market_state.shared_projection.portfolio"),
        _path(row, "decision.portfolio_snapshot"),
        row.get("portfolio_state"),
        row.get("portfolio_snapshot"),
        _path(row, "trader_context.portfolio"),
    )
    merged: dict[str, Any] = {}
    for candidate in candidates:
        if isinstance(candidate, dict):
            merged.update(candidate)
    return merged


def _hypothesis_objects(row: Mapping[str, Any], decision: Mapping[str, Any]) -> list[tuple[str, Any]]:
    fields = (
        "thesis",
        "hypothesis",
        "hypotheses",
        "assumptions",
        "entry_dimensions",
        "trade_plan_evaluation",
    )
    objects: list[tuple[str, Any]] = []
    for field in fields:
        row_value = row.get(field)
        if field == "hypotheses" and isinstance(row_value, dict) and (
            "universe" in row_value or "trader" in row_value
        ):
            # In cross-loop exports this is the container for both loops, not
            # a Trader hypothesis.  Pulling it into the base arm would leak
            # the very Universe context the ablation is meant to withhold.
            row_value = None
        value = _first(
            decision.get(field),
            row_value,
            _path(row, f"trader_decision.{field}"),
            _path(row, f"hypotheses.trader.{field}"),
            _path(row, "hypotheses.trader.structured_thesis") if field == "thesis" else None,
        )
        if isinstance(value, (dict, list)) and bool(value):
            objects.append((field, value))
    return objects


def _structured_thesis(row: Mapping[str, Any], decision: Mapping[str, Any]) -> Any:
    thesis = _first(
        decision.get("thesis"),
        row.get("thesis"),
        _path(row, "trader_decision.thesis"),
        _path(row, "hypotheses.trader.structured_thesis"),
    )
    return thesis if isinstance(thesis, (dict, list)) and bool(thesis) else None


def _structured_tokens(prefix: str, value: Any, *, depth: int = 0) -> set[str]:
    if depth > 3:
        return set()
    tokens: set[str] = set()
    if isinstance(value, dict):
        for raw_key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            key = _slug(raw_key, limit=36)
            field = f"{prefix}.{key}"
            tokens.add(_token(f"{field}.present", True))
            if isinstance(item, (dict, list)):
                tokens.update(_structured_tokens(field, item, depth=depth + 1))
            elif isinstance(item, bool) or item is None:
                tokens.add(_token(field, item))
            elif _number(item) is not None:
                tokens.add(_token(field, _bucket(item, (-1.0, 0.0, 0.5, 1.0))))
            elif len(str(item)) <= 48:
                tokens.add(_token(field, item))
    elif isinstance(value, list):
        tokens.add(_token(f"{prefix}.count", _count_bucket(len(value))))
        for item in value[:12]:
            if isinstance(item, (dict, list)):
                tokens.update(_structured_tokens(prefix, item, depth=depth + 1))
            elif len(str(item)) <= 48:
                tokens.add(_token(f"{prefix}.value", item))
    return tokens


def _tool_calls(row: Mapping[str, Any], decision: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates = (
        _path(decision, "domain_tools.tool_calls"),
        decision.get("tool_calls"),
        _path(row, "trader_workflow.domain_steps"),
        _path(row, "trader_workflow.domain_tool_steps"),
        _path(row, "trader_workflow.tool_calls"),
        row.get("domain_tool_steps"),
    )
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def _base_features(row: Mapping[str, Any]) -> tuple[frozenset[str], dict[str, bool]]:
    decision = _decision(row)
    market = _market_projection(row)
    portfolio = _portfolio_projection(row)
    state_projection = _dict(_path(row, "state_before.shared_projection"))
    tokens: set[str] = {
        _token("decision.action", _first(decision.get("action"), row.get("action"))),
        _token("decision.intent", _first(decision.get("intent"), row.get("intent"))),
        _token("decision.reason_code", decision.get("decision_reason_code")),
        _token("decision.opportunity_side", decision.get("opportunity_side")),
        _token("decision.confidence", _bucket(decision.get("confidence"), (0.4, 0.55, 0.7, 0.85))),
    }

    configuration = _dict(row.get("configuration"))
    attempt_models = [item for item in _list(configuration.get("attempt_models")) if isinstance(item, dict)]
    configuration_values = {
        "experiment_id": _first(decision.get("experiment_id"), configuration.get("experiment_id")),
        "provider": _first(decision.get("llm_provider"), configuration.get("llm_provider")),
        "model": _first(decision.get("llm_model"), configuration.get("llm_model")),
    }
    for name, value in configuration_values.items():
        tokens.add(_token(f"configuration.{name}", value))
    for attempt_model in attempt_models:
        for name in ("summary_model_id", "agent_name", "reasoning_effort"):
            tokens.add(_token(f"configuration.{name}", attempt_model.get(name)))

    market_specs: tuple[tuple[str, set[str], tuple[float, ...]], ...] = (
        ("atr_pct", {"atr_pct", "atr_pct_14"}, (0.005, 0.01, 0.02, 0.04)),
        ("relative_volume", {"relative_volume", "relative_volume_20", "rvol"}, (0.5, 0.8, 1.2, 2.0)),
        ("range_position", {"range_position", "range_pos"}, (0.2, 0.4, 0.6, 0.8)),
        ("z_score", {"z_score", "zscore"}, (-2.0, -1.0, 0.0, 1.0, 2.0)),
        ("trend_slope", {"trend_slope", "slope"}, (-0.02, 0.0, 0.02)),
        ("return", {"return_1d", "change_pct", "price_change_pct"}, (-0.03, -0.01, 0.0, 0.01, 0.03)),
    )
    for feature_name, names, cuts in market_specs:
        tokens.add(_token(f"market.{feature_name}", _bucket(_find_key(market, names), cuts)))

    stale = _first(
        _find_key(market, {"stale", "is_stale", "stale_market_data"}),
        state_projection.get("stale_market_data"),
    )
    if isinstance(stale, (dict, list)):
        stale = bool(stale)
    tokens.add(_token("market.stale", stale))
    tokens.add(
        _token(
            "market.session_open",
            _first(
                _find_key(market, {"session_open", "is_open", "market_open"}),
                _find_key(state_projection.get("market_clocks"), {"is_open", "session_open"}),
            ),
        )
    )
    tokens.add(
        _token(
            "market.since_open",
            _bucket(_find_key(market, {"since_open_m", "minutes_since_open"}), (15, 60, 180, 300)),
        )
    )

    target_holding = _dict(portfolio.get("target_holding"))
    quantity = _first(target_holding.get("quantity"), portfolio.get("position_quantity"))
    quantity_number = _number(quantity)
    if quantity_number is None or quantity_number == 0:
        position_side = "flat"
    else:
        position_side = "long" if quantity_number > 0 else "short"
    tokens.add(_token("portfolio.position_side", position_side))
    equity = _number(portfolio.get("equity"))
    gross = _number(portfolio.get("gross_exposure_usd"))
    gross_ratio = gross / equity if equity not in (None, 0.0) and gross is not None else None
    tokens.add(_token("portfolio.gross_to_equity", _bucket(gross_ratio, (0.25, 0.5, 0.75, 1.0))))

    hypotheses = _hypothesis_objects(row, decision)
    structured_thesis = _structured_thesis(row, decision)
    for name, value in hypotheses:
        tokens.update(_structured_tokens(f"hypothesis.{name}", value))
    tokens.add(_token("hypothesis.structured_present", bool(hypotheses)))
    tokens.add(_token("hypothesis.thesis_present", structured_thesis is not None))

    rationale = _first(
        decision.get("rationale"),
        decision.get("rationale_text"),
        row.get("rationale"),
        _path(row, "trader_decision.rationale"),
        _path(row, "hypotheses.trader.rationale"),
    )
    rationale_text = rationale.strip() if isinstance(rationale, str) else ""
    tokens.add(_token("rationale.present", bool(rationale_text)))
    tokens.add(_token("rationale.length", _count_bucket(len(rationale_text) // 160)))
    lowered = rationale_text.lower()
    for concept, needles in _RATIONALE_CONCEPTS.items():
        tokens.add(_token(f"rationale.concept.{concept}", any(needle in lowered for needle in needles)))

    calls = _tool_calls(row, decision)
    names: list[str] = []
    for call in calls:
        request = _dict(call.get("request"))
        response = _dict(call.get("response"))
        name = _first(call.get("tool"), call.get("name"), request.get("tool"), response.get("tool"))
        outcome = _first(call.get("outcome"), response.get("outcome"), response.get("status"))
        if name:
            normalized_name = _slug(name, limit=48)
            names.append(normalized_name)
            tokens.add(_token("tools.name", normalized_name))
            tokens.add(_token(f"tools.outcome.{normalized_name}", outcome))
    tokens.add(_token("tools.count", _count_bucket(len(calls))))
    if names:
        tokens.add(_token("tools.sequence", ">".join(names[:8])))

    return frozenset(tokens), {
        "configuration": _has_meaningful_value(configuration_values) or bool(attempt_models),
        "structured_thesis": structured_thesis is not None,
        "structured_hypothesis_any": bool(hypotheses),
        "rationale_text": bool(rationale_text),
        "tool_pattern": bool(calls),
    }


def _universe_context(row: Mapping[str, Any]) -> tuple[dict[str, Any], str, bool]:
    direct = row.get("universe_context")
    nested = _path(row, "cross_loop.universe_context")
    observed_candidates = (
        ("cross_loop_observed_context", direct),
        ("nested_cross_loop_observed_context", nested),
        ("model_prompt_observation", _path(row, "model_observation.symbol_block.universe_mandate")),
    )
    for source, candidate in observed_candidates:
        if not isinstance(candidate, dict) or not _has_meaningful_value(candidate):
            continue
        if source.endswith("observed_context"):
            observed = candidate.get("observed_context")
            if not isinstance(observed, dict) or not _has_meaningful_value(observed):
                continue
        return candidate, source, True

    reconstructed_candidates = (
        ("reconstructed_universe_lineage", row.get("universe_lineage")),
        ("task_state_before", _path(row, "state_before.per_symbol_facts.universe_mandate")),
        ("legacy_market_state", _path(row, "market_state.universe_context")),
    )
    for source, candidate in reconstructed_candidates:
        if isinstance(candidate, dict) and _has_meaningful_value(candidate):
            return candidate, source, False
    return {}, "missing", False


def _has_meaningful_value(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has_meaningful_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_meaningful_value(item) for item in value)
    return value is not None and value != ""


def _universe_features(
    row: Mapping[str, Any],
) -> tuple[frozenset[str], bool, str, bool]:
    context, source, observed = _universe_context(row)
    if not context:
        return frozenset({_token("universe.present", False)}), False, source, observed

    hypothesis = _dict(_path(row, "hypotheses.universe"))
    symbol_mandate = _dict(
        _first(
            context.get("symbol_mandate"),
            _path(context, "observed_context.symbol_mandate"),
            _path(context, "mandate.symbol_mandate"),
            _path(context, "mandate_projection.symbol_mandate"),
            _path(context, "mandate_snapshot.symbol_mandate"),
            _path(context, "task_context.symbol_mandate"),
            hypothesis if _has_meaningful_value(hypothesis) else None,
        )
    )
    family_context = _dict(symbol_mandate.get("family_context"))
    mandate_ref = _dict(_first(context.get("mandate_ref"), context.get("task_mandate_ref")))
    relation = _first(context.get("relation"), context.get("status"), context.get("lineage_status"))
    allowed_sides = _first(symbol_mandate.get("allowed_sides"), context.get("allowed_sides"))
    allowed_values = sorted(_slug(value, limit=24) for value in _list(allowed_sides))
    directional_view = _first(
        symbol_mandate.get("directional_view"),
        context.get("directional_view"),
        hypothesis.get("directional_view"),
    )
    if isinstance(directional_view, dict):
        directional_view = _first(
            directional_view.get("side"),
            directional_view.get("bias"),
            directional_view.get("view"),
        )
    confidence = _first(symbol_mandate.get("confidence"), context.get("confidence"), hypothesis.get("confidence"))
    family = _first(
        family_context.get("family"),
        symbol_mandate.get("family"),
        context.get("family"),
        hypothesis.get("family"),
    )
    posture = _first(
        symbol_mandate.get("posture"),
        family_context.get("posture"),
        context.get("posture"),
        hypothesis.get("posture"),
    )
    tokens = {
        _token("universe.present", True),
        _token("universe.relation", relation),
        _token("universe.directional_view", directional_view),
        _token("universe.allowed_sides", "+".join(allowed_values) if allowed_values else _MISSING),
        _token(
            "universe.role",
            _first(symbol_mandate.get("role"), context.get("role"), hypothesis.get("role")),
        ),
        _token("universe.posture", posture),
        _token("universe.confidence", _bucket(confidence, (0.4, 0.55, 0.7, 0.85))),
        _token("universe.family", family),
        _token("universe.mandate_status", mandate_ref.get("status")),
    }
    return frozenset(tokens), True, source, observed


def _outcome(row: Mapping[str, Any]) -> dict[str, Any]:
    candidates = (
        _path(row, "cross_loop.target.trader_flair"),
        _path(row, "target.trader_flair"),
        row.get("learning_outcome"),
        _path(row, "rewards.trader_decision"),
        _path(row, "rewards.trader_decision_flair"),
        _path(row, "outcomes.trader_decision"),
        row.get("trader_outcome"),
        row.get("trader_reward"),
    )
    fallback: dict[str, Any] | None = None
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if candidate and fallback is None:
            fallback = candidate
        for nested_name in ("flair", "outcome", "learning_outcome"):
            nested = candidate.get(nested_name)
            if isinstance(nested, dict) and (
                nested.get("verdict") is not None or nested.get("status") is not None
            ):
                return candidate | nested
        if candidate.get("verdict") is not None or candidate.get("status") is not None:
            return candidate
    return fallback or {}


def _normalized_label(outcome: Mapping[str, Any]) -> tuple[str | None, str]:
    raw = outcome.get("verdict")
    if raw is None or not str(raw).strip():
        default_status = "pending" if outcome else "missing"
        status = str(_first(outcome.get("status"), outcome.get("maturity"), default_status)).lower()
        return None, "pending" if status in {"pending", "immature", "not_due"} else "missing"
    label = str(raw).strip().upper()
    if label in LABELS:
        semantics_version = outcome.get("outcome_semantics_version")
        if semantics_version is not None:
            try:
                trusted = int(semantics_version) == int(BENCHMARK_SEMANTICS_VERSION)
            except (TypeError, ValueError):
                trusted = False
            if not trusted:
                return None, "stale_semantics"
        return label, "included"
    if label in {"NEUTRAL", "UNKNOWN"}:
        return None, label.lower()
    if label in {"PENDING", "IMMATURE", "NOT_DUE"}:
        return None, "pending"
    return None, "other"


def _outcome_basis(row: Mapping[str, Any], outcome: Mapping[str, Any]) -> str:
    explicit = _first(
        outcome.get("outcome_basis"),
        outcome.get("evaluation_basis"),
        outcome.get("basis"),
        # ``verdict_basis`` is accepted for future Trader exports, but Universe
        # outcomes are never searched by _outcome().
        outcome.get("verdict_basis"),
    )
    if explicit:
        normalized = _slug(explicit, limit=40)
        if any(word in normalized for word in ("realized", "realise", "réalis")):
            return "realized"
        if any(word in normalized for word in ("counterfactual", "contrefact")):
            return "counterfactual"
        return normalized

    decision = _decision(row)
    action = str(_first(decision.get("action"), outcome.get("action"), "")).upper()
    intent = str(_first(decision.get("intent"), outcome.get("intent"), "")).upper()
    opening_intents = {"OPEN_LONG", "OPEN_SHORT", "SCALE_IN", "FLIP"}
    # Intent is the semantic operation.  The physical side alone is ambiguous:
    # BUY can open a long *or close/reduce a short*, and SELL can do the mirror
    # operation.  Only fall back to action when legacy data has no intent.
    semantic_action = intent or action
    executed = _first(decision.get("executed"), outcome.get("executed"))
    # This follows the current Trader contract: matured entry lots use their
    # flat-to-flat result; HOLD/blocked/close/reduce rows retain the fixed-
    # horizon counterfactual.  Unknown legacy actions stay explicit unknown.
    if semantic_action in {*opening_intents, "BUY", "SELL_SHORT"}:
        if executed is True or executed == 1:
            return "realized"
        if executed is False or executed == 0:
            return "counterfactual"
        return "unknown"
    if semantic_action in {"HOLD", "CLOSE", "REDUCE", "SCALE_OUT", "STRATEGY_CLOSE"}:
        return "counterfactual"
    return "unknown"


def _realized_close_evidence(
    row: Mapping[str, Any],
    *,
    outcome_basis: str,
) -> tuple[datetime | None, str]:
    if outcome_basis != "realized":
        return None, "not_realized"
    execution = _first_path(
        row,
        (
            "target.execution",
            "cross_loop.target.execution",
            "execution",
            "outcomes.execution",
            "rewards.execution",
        ),
    )
    if not isinstance(execution, dict):
        return None, "execution_target_missing"
    cycles = execution.get("position_cycles")
    if not isinstance(cycles, list) or not cycles:
        return None, "position_cycles_missing"
    exit_times: list[datetime] = []
    for cycle in cycles:
        if not isinstance(cycle, dict):
            return None, "position_cycle_exit_incomplete"
        raw_exit = _first(cycle.get("exit_ts"), cycle.get("closed_at"), cycle.get("close_ts"))
        parsed = _timestamp(raw_exit)
        if parsed is None:
            return None, "position_cycle_exit_incomplete"
        exit_times.append(parsed)
    return max(exit_times), "exact_position_cycle_exit"


def _identity(row: Mapping[str, Any], ordinal: int) -> tuple[str, str, str | None]:
    identity = _dict(row.get("identity"))
    cycle = _first(
        identity.get("cycle_id"),
        row.get("cycle_id"),
        row.get("cycle_ts"),
        _path(row, "trader_decision.cycle_ts"),
    )
    symbol = _first(identity.get("symbol"), row.get("symbol"), _path(row, "trader_decision.symbol"), "")
    decision_id = _first(identity.get("decision_id"), row.get("decision_id"), _path(row, "decision.decision_id"))
    # Missing cycle IDs must not collapse unrelated legacy rows into one group.
    cycle_id = str(cycle) if cycle else f"missing-cycle:{ordinal:09d}"
    return cycle_id, str(symbol), str(decision_id) if decision_id else None


def prepare_examples(rows: Sequence[Mapping[str, Any]]) -> tuple[list[Example], dict[str, Any]]:
    """Extract labelled observational examples plus trace-coverage metadata."""

    examples: list[Example] = []
    excluded = Counter()
    labels = Counter()
    bases_all = Counter()
    bases_labelled = Counter()
    labels_by_basis: dict[str, Counter[str]] = defaultdict(Counter)
    realized_close_evidence = Counter()
    schema_versions = Counter()
    outcome_semantics_versions = Counter()
    coverage_counts = Counter()

    for ordinal, row in enumerate(rows):
        schema_versions[str(row.get("schema_version") or "missing")] += 1
        outcome = _outcome(row)
        coverage_counts["trader_outcome_record"] += int(bool(outcome))
        semantics_version = outcome.get("outcome_semantics_version") if outcome else None
        outcome_semantics_versions[str(semantics_version) if semantics_version is not None else "missing"] += 1
        label, label_status = _normalized_label(outcome)
        basis = _outcome_basis(row, outcome)
        realized_exit_at, close_evidence = _realized_close_evidence(row, outcome_basis=basis)
        bases_all[basis] += 1
        realized_close_evidence[close_evidence] += 1
        base_features, feature_coverage = _base_features(row)
        universe_features, has_universe, universe_source, universe_observed = _universe_features(row)
        coverage_counts["configuration"] += int(feature_coverage["configuration"])
        coverage_counts["structured_thesis"] += int(feature_coverage["structured_thesis"])
        coverage_counts["structured_hypothesis_any"] += int(feature_coverage["structured_hypothesis_any"])
        coverage_counts["rationale_text"] += int(feature_coverage["rationale_text"])
        coverage_counts["tool_pattern"] += int(feature_coverage["tool_pattern"])
        coverage_counts["universe_context"] += int(has_universe)
        coverage_counts["universe_context_observed"] += int(has_universe and universe_observed)
        coverage_counts[f"universe_source::{universe_source}"] += 1
        if label is None:
            excluded[label_status] += 1
            continue

        cycle_id, symbol, decision_id = _identity(row, ordinal)
        labels[label] += 1
        bases_labelled[basis] += 1
        labels_by_basis[basis][label] += 1
        examples.append(
            Example(
                ordinal=ordinal,
                cycle_id=cycle_id,
                symbol=symbol,
                decision_id=decision_id,
                label=label,
                outcome_basis=basis,
                base_features=base_features,
                universe_features=universe_features,
                has_structured_thesis=feature_coverage["structured_thesis"],
                has_rationale_text=feature_coverage["rationale_text"],
                has_tool_pattern=feature_coverage["tool_pattern"],
                has_universe_context=has_universe,
                universe_context_source=universe_source,
                universe_context_observed=universe_observed,
                realized_exit_at=realized_exit_at,
                realized_close_evidence=close_evidence,
            )
        )

    total = len(rows)
    labelled = len(examples)
    coverage = {
        "records": total,
        "eligible_win_loss": labelled,
        "eligible_rate": round(labelled / total, 6) if total else None,
        "label_support": {label: labels[label] for label in LABELS},
        "excluded_labels": dict(sorted(excluded.items())),
        "outcome_basis_all": dict(sorted(bases_all.items())),
        "outcome_basis_labelled": dict(sorted(bases_labelled.items())),
        "label_support_by_outcome_basis": {
            basis: {
                "rows": sum(support.values()),
                **{label: support[label] for label in LABELS},
            }
            for basis, support in sorted(labels_by_basis.items())
        },
        "realized_close_evidence": dict(sorted(realized_close_evidence.items())),
        "decision_time_fields": {
            name: {
                "records": coverage_counts[name],
                "rate": round(coverage_counts[name] / total, 6) if total else None,
            }
            for name in (
                "configuration",
                "structured_thesis",
                "structured_hypothesis_any",
                "rationale_text",
                "tool_pattern",
                "universe_context",
            )
        },
        "universe_context_provenance": {
            "observed_records": coverage_counts["universe_context_observed"],
            "observed_rate": (
                round(coverage_counts["universe_context_observed"] / total, 6)
                if total
                else None
            ),
            "sources": {
                key.removeprefix("universe_source::"): value
                for key, value in sorted(coverage_counts.items())
                if key.startswith("universe_source::")
            },
            "policy": "observed_prompt_context_preferred; reconstructed_context_reported_separately",
        },
        "trader_outcome_records": {
            "records": coverage_counts["trader_outcome_record"],
            "rate": round(coverage_counts["trader_outcome_record"] / total, 6) if total else None,
        },
        "schema_versions": dict(sorted(schema_versions.items())),
        "outcome_semantics_versions": dict(sorted(outcome_semantics_versions.items())),
    }
    return examples, coverage


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    malformed = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if isinstance(value, dict):
                rows.append(value)
            else:
                malformed += 1
    return rows, malformed


def _cycle_sort_key(cycle_id: str) -> tuple[int, Any, str]:
    raw = cycle_id.strip()
    parsed = _timestamp(raw)
    return (0, parsed.timestamp(), raw) if parsed is not None else (1, raw, raw)


def chronological_group_split(
    examples: Sequence[Example],
    *,
    test_fraction: float = 0.2,
    embargo_hours: float = 24.0,
    purge_realized: bool = True,
) -> tuple[list[Example], list[Example], dict[str, Any]]:
    """Chronological, cycle-grouped split with target-availability purging.

    Counterfactual Trader FLAIR is normally evaluated at one day (with a four
    hour fallback), so the last 24 hours before the test cohort are embargoed.
    Realized entry labels are only admitted to train when every linked
    flat-to-flat cycle has an exit timestamp strictly before the test start.
    """

    if not 0.0 < test_fraction < 1.0:
        raise ValueError("test_fraction must be strictly between 0 and 1")
    if embargo_hours < 0.0:
        raise ValueError("embargo_hours must be non-negative")
    by_cycle: dict[str, list[Example]] = defaultdict(list)
    for example in examples:
        by_cycle[example.cycle_id].append(example)
    cycles = sorted(by_cycle, key=_cycle_sort_key)
    if len(cycles) < 2:
        return list(examples), [], {
            "strategy": "chronological_grouped_by_cycle_with_target_availability_purge",
            "cycle_count": len(cycles),
            "train_cycles": cycles,
            "test_cycles": [],
            "cycle_overlap": [],
            "embargo_hours": embargo_hours,
            "embargo_status": "not_applicable_without_test",
            "purge_realized": purge_realized,
            "purged_train_rows": {},
        }

    desired_test_rows = max(1, math.ceil(len(examples) * test_fraction))
    test_cycle_count = 1
    accumulated = len(by_cycle[cycles[-1]])
    while test_cycle_count < len(cycles) - 1 and accumulated < desired_test_rows:
        test_cycle_count += 1
        accumulated += len(by_cycle[cycles[-test_cycle_count]])
    candidate_train_cycles = cycles[:-test_cycle_count]
    test_cycles = cycles[-test_cycle_count:]
    candidate_train = [
        item for cycle in candidate_train_cycles for item in sorted(by_cycle[cycle], key=lambda row: row.ordinal)
    ]
    test = [item for cycle in test_cycles for item in sorted(by_cycle[cycle], key=lambda row: row.ordinal)]
    test_start = _timestamp(test_cycles[0])
    cutoff = test_start - timedelta(hours=embargo_hours) if test_start is not None else None
    purged = Counter()
    train: list[Example] = []
    for item in candidate_train:
        cycle_at = _timestamp(item.cycle_id)
        if cutoff is None or cycle_at is None:
            purged["embargo_timestamp_unavailable"] += 1
            continue
        # At equality, the one-day counterfactual target uses information from
        # the first test instant, so it is not available strictly before test.
        if cycle_at >= cutoff:
            purged["within_counterfactual_embargo"] += 1
            continue
        if purge_realized and item.outcome_basis == "realized":
            if item.realized_exit_at is None:
                purged[f"realized_{item.realized_close_evidence}"] += 1
                continue
            if test_start is None or item.realized_exit_at >= test_start:
                purged["realized_exit_at_or_after_test_start"] += 1
                continue
        train.append(item)

    train_cycles = sorted({item.cycle_id for item in train}, key=_cycle_sort_key)
    overlap = sorted(set(train_cycles) & set(test_cycles))
    return train, test, {
        "strategy": "chronological_grouped_by_cycle_with_target_availability_purge",
        "test_fraction_requested": test_fraction,
        "cycle_count": len(cycles),
        "candidate_train_cycle_count": len(candidate_train_cycles),
        "train_cycle_count": len(train_cycles),
        "test_cycle_count": len(test_cycles),
        "train_cycles": train_cycles,
        "test_cycles": test_cycles,
        "cycle_overlap": overlap,
        "test_start": test_start.isoformat() if test_start is not None else None,
        "embargo_hours": embargo_hours,
        "embargo_cutoff": cutoff.isoformat() if cutoff is not None else None,
        "embargo_status": "applied" if cutoff is not None else "unavailable",
        "purge_realized": purge_realized,
        "candidate_train_rows": len(candidate_train),
        "purged_train_rows": dict(sorted(purged.items())),
        "purged_train_rows_total": sum(purged.values()),
        "train_rows": len(train),
        "test_rows": len(test),
    }


class BernoulliNaiveBayes:
    """Tiny deterministic Bernoulli NB with Laplace smoothing."""

    def __init__(self, *, alpha: float = 1.0) -> None:
        if alpha <= 0.0:
            raise ValueError("alpha must be positive")
        self.alpha = float(alpha)
        self.class_counts: Counter[str] = Counter()
        self.feature_counts: dict[str, Counter[str]] = {label: Counter() for label in LABELS}
        self.vocabulary: tuple[str, ...] = ()

    def fit(self, features: Sequence[frozenset[str]], labels: Sequence[str]) -> "BernoulliNaiveBayes":
        if len(features) != len(labels) or not labels:
            raise ValueError("fit requires equally sized, non-empty features and labels")
        vocabulary: set[str] = set()
        for tokens, label in zip(features, labels):
            if label not in LABELS:
                raise ValueError(f"unsupported label: {label}")
            unique = set(tokens)
            vocabulary.update(unique)
            self.class_counts[label] += 1
            self.feature_counts[label].update(unique)
        self.vocabulary = tuple(sorted(vocabulary))
        return self

    def predict_proba(self, features: Iterable[str]) -> dict[str, float]:
        if not self.vocabulary or sum(self.class_counts.values()) == 0:
            raise RuntimeError("model is not fitted")
        present = set(features)
        total = sum(self.class_counts.values())
        scores: dict[str, float] = {}
        for label in LABELS:
            class_count = self.class_counts[label]
            prior = (class_count + self.alpha) / (total + self.alpha * len(LABELS))
            score = math.log(prior)
            denominator = class_count + 2.0 * self.alpha
            counts = self.feature_counts[label]
            for token in self.vocabulary:
                probability = (counts[token] + self.alpha) / denominator
                score += math.log(probability if token in present else 1.0 - probability)
            scores[label] = score
        maximum = max(scores.values())
        exp_scores = {label: math.exp(score - maximum) for label, score in scores.items()}
        normalizer = sum(exp_scores.values())
        return {label: exp_scores[label] / normalizer for label in LABELS}


def _majority_probabilities(train_labels: Sequence[str], *, alpha: float) -> dict[str, float]:
    counts = Counter(train_labels)
    denominator = len(train_labels) + alpha * len(LABELS)
    return {label: (counts[label] + alpha) / denominator for label in LABELS}


def classification_metrics(labels: Sequence[str], probabilities: Sequence[Mapping[str, float]]) -> dict[str, Any]:
    if len(labels) != len(probabilities):
        raise ValueError("labels and probabilities must have the same length")
    supports = Counter(labels)
    if not labels:
        return {
            "rows": 0,
            "support": {label: 0 for label in LABELS},
            "accuracy": None,
            "balanced_accuracy": None,
            "macro_f1": None,
            "log_loss": None,
            "brier": None,
        }

    predictions = [max(LABELS, key=lambda label: (float(row.get(label, 0.0)), label)) for row in probabilities]
    correct = sum(actual == predicted for actual, predicted in zip(labels, predictions))
    recalls: list[float] = []
    f1s: list[float] = []
    for label in LABELS:
        tp = sum(actual == label and predicted == label for actual, predicted in zip(labels, predictions))
        fn = sum(actual == label and predicted != label for actual, predicted in zip(labels, predictions))
        fp = sum(actual != label and predicted == label for actual, predicted in zip(labels, predictions))
        recall = tp / (tp + fn) if tp + fn else 0.0
        precision = tp / (tp + fp) if tp + fp else 0.0
        recalls.append(recall)
        f1s.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)

    epsilon = 1e-15
    losses = []
    briers = []
    for actual, row in zip(labels, probabilities):
        probability = min(max(float(row.get(actual, 0.0)), epsilon), 1.0 - epsilon)
        losses.append(-math.log(probability))
        win_probability = min(max(float(row.get("WIN", 0.0)), 0.0), 1.0)
        briers.append((win_probability - (1.0 if actual == "WIN" else 0.0)) ** 2)

    return {
        "rows": len(labels),
        "support": {label: supports[label] for label in LABELS},
        "predicted_support": {label: predictions.count(label) for label in LABELS},
        "accuracy": round(correct / len(labels), 6),
        "balanced_accuracy": round(sum(recalls) / len(LABELS), 6),
        "macro_f1": round(sum(f1s) / len(LABELS), 6),
        "log_loss": round(sum(losses) / len(losses), 6),
        "brier": round(sum(briers) / len(briers), 6),
    }


def _fit_predict(
    train: Sequence[Example],
    test: Sequence[Example],
    *,
    enriched: bool,
    alpha: float,
) -> tuple[list[dict[str, float]], dict[str, Any]]:
    train_features = [row.enriched_features if enriched else row.base_features for row in train]
    test_features = [row.enriched_features if enriched else row.base_features for row in test]
    model = BernoulliNaiveBayes(alpha=alpha).fit(train_features, [row.label for row in train])
    probabilities = [model.predict_proba(features) for features in test_features]
    return probabilities, {
        "kind": "bernoulli_naive_bayes",
        "alpha": alpha,
        "feature_set": "base_plus_universe" if enriched else "base",
        "vocabulary_size": len(model.vocabulary),
        "train_class_support": {label: model.class_counts[label] for label in LABELS},
    }


def _metric_lift(base: Mapping[str, Any], enriched: Mapping[str, Any]) -> dict[str, float | None]:
    def delta(metric: str, *, inverse: bool = False) -> float | None:
        left = base.get(metric)
        right = enriched.get(metric)
        if left is None or right is None:
            return None
        result = float(left) - float(right) if inverse else float(right) - float(left)
        return round(result, 6)

    return {
        "accuracy": delta("accuracy"),
        "balanced_accuracy": delta("balanced_accuracy"),
        "macro_f1": delta("macro_f1"),
        "log_loss_improvement": delta("log_loss", inverse=True),
        "brier_improvement": delta("brier", inverse=True),
    }


def _basis_slices(
    test: Sequence[Example],
    probability_sets: Mapping[str, Sequence[Mapping[str, float]]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    bases = sorted({row.outcome_basis for row in test})
    for basis in bases:
        indexes = [index for index, row in enumerate(test) if row.outcome_basis == basis]
        result[basis] = {
            name: classification_metrics(
                [test[index].label for index in indexes],
                [probabilities[index] for index in indexes],
            )
            for name, probabilities in probability_sets.items()
        }
    return result


def _descriptive_feature_slices(
    test: Sequence[Example],
    *,
    minimum_support: int,
    limit: int = 40,
) -> list[dict[str, Any]]:
    if not test:
        return []
    base_rate = sum(row.label == "WIN" for row in test) / len(test)
    accepted_prefixes = ("configuration.", "universe.", "hypothesis.", "rationale.", "tools.")
    rows_by_token: dict[str, list[Example]] = defaultdict(list)
    for row in test:
        for token in row.enriched_features:
            if token.startswith(accepted_prefixes):
                rows_by_token[token].append(row)
    slices: list[dict[str, Any]] = []
    for token, rows in rows_by_token.items():
        if len(rows) < minimum_support:
            continue
        wins = sum(row.label == "WIN" for row in rows)
        win_rate = wins / len(rows)
        slices.append(
            {
                "feature": token,
                "support": len(rows),
                "WIN": wins,
                "LOSS": len(rows) - wins,
                "win_rate": round(win_rate, 6),
                "win_rate_delta_vs_test": round(win_rate - base_rate, 6),
            }
        )
    slices.sort(key=lambda row: (-row["support"], -abs(row["win_rate_delta_vs_test"]), row["feature"]))
    return slices[:limit]


def _support_readiness(
    total_rows: Sequence[Example],
    train_rows: Sequence[Example],
    test_rows: Sequence[Example],
    *,
    min_class_support: int,
    min_train_class_support: int,
    min_test_class_support: int,
) -> dict[str, Any]:
    total_support = Counter(row.label for row in total_rows)
    train_support = Counter(row.label for row in train_rows)
    test_support = Counter(row.label for row in test_rows)
    reasons: list[str] = []
    for label in LABELS:
        if total_support[label] < min_class_support:
            reasons.append(f"total_{label.lower()}_support<{min_class_support}")
        if train_support[label] < min_train_class_support:
            reasons.append(f"train_{label.lower()}_support<{min_train_class_support}")
        if test_support[label] < min_test_class_support:
            reasons.append(f"test_{label.lower()}_support<{min_test_class_support}")
    return {
        "status": "GO" if not reasons else "NO_GO",
        "reasons": reasons,
        "total_support": {label: total_support[label] for label in LABELS},
        "train_support": {label: train_support[label] for label in LABELS},
        "test_support": {label: test_support[label] for label in LABELS},
    }


def _readiness_with_split_contract(
    total_rows: Sequence[Example],
    train_rows: Sequence[Example],
    test_rows: Sequence[Example],
    split: Mapping[str, Any],
    *,
    min_class_support: int,
    min_train_class_support: int,
    min_test_class_support: int,
    require_realized_purge: bool,
) -> dict[str, Any]:
    readiness = _support_readiness(
        total_rows,
        train_rows,
        test_rows,
        min_class_support=min_class_support,
        min_train_class_support=min_train_class_support,
        min_test_class_support=min_test_class_support,
    )
    reasons = list(readiness["reasons"])
    if split.get("cycle_overlap"):
        reasons.append("cycle_leakage_detected")
    if split.get("cycle_count", 0) < 2:
        reasons.append("fewer_than_two_cycles")
    if split.get("cycle_count", 0) >= 2 and split.get("embargo_status") != "applied":
        reasons.append("counterfactual_embargo_unavailable")
    if require_realized_purge and not split.get("purge_realized"):
        reasons.append("realized_close_purge_disabled")
    readiness["reasons"] = reasons
    readiness["status"] = "GO" if not reasons else "NO_GO"
    readiness["split_summary"] = {
        key: split.get(key)
        for key in (
            "cycle_count",
            "train_cycle_count",
            "test_cycle_count",
            "candidate_train_rows",
            "purged_train_rows",
            "train_rows",
            "test_rows",
        )
    }
    return readiness


def run_analysis(
    rows: Sequence[Mapping[str, Any]],
    *,
    test_fraction: float = 0.2,
    alpha: float = 1.0,
    min_class_support: int = 20,
    min_train_class_support: int = 10,
    min_test_class_support: int = 5,
    slice_min_support: int = 3,
    embargo_hours: float = 24.0,
    purge_realized: bool = True,
    outcome_basis: str = "counterfactual",
) -> dict[str, Any]:
    if outcome_basis not in {"counterfactual", "realized"}:
        raise ValueError("outcome_basis must be counterfactual or realized")
    examples, coverage = prepare_examples(rows)
    model_examples = [row for row in examples if row.outcome_basis == outcome_basis]
    train, test, split = chronological_group_split(
        model_examples,
        test_fraction=test_fraction,
        embargo_hours=embargo_hours,
        purge_realized=purge_realized,
    )
    overall_readiness = _readiness_with_split_contract(
        model_examples,
        train,
        test,
        split,
        min_class_support=min_class_support,
        min_train_class_support=min_train_class_support,
        min_test_class_support=min_test_class_support,
        require_realized_purge=outcome_basis == "realized",
    )
    reasons = list(overall_readiness["reasons"])

    readiness_by_basis: dict[str, Any] = {}
    for basis in ("realized", "counterfactual"):
        basis_total = [row for row in examples if row.outcome_basis == basis]
        if basis == outcome_basis:
            basis_train, basis_test, basis_split = train, test, split
        else:
            basis_train, basis_test, basis_split = chronological_group_split(
                basis_total,
                test_fraction=test_fraction,
                embargo_hours=embargo_hours,
                purge_realized=purge_realized,
            )
        readiness_by_basis[basis] = _readiness_with_split_contract(
            basis_total,
            basis_train,
            basis_test,
            basis_split,
            min_class_support=min_class_support,
            min_train_class_support=min_train_class_support,
            min_test_class_support=min_test_class_support,
            require_realized_purge=basis == "realized",
        )

    coverage["analysis_cohort"] = {
        "outcome_basis": outcome_basis,
        "eligible_rows": len(model_examples),
        "excluded_other_basis_rows": len(examples) - len(model_examples),
    }

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "GO" if not reasons else "NO_GO",
        "interpretation": (
            f"Universe lift may be estimated for {outcome_basis} FLAIR on the held-out chronological cohort."
            if not reasons
            else "Metrics are diagnostic only; support/readiness gates failed."
        ),
        "evaluation_contract": {
            "features": "decision_time_only",
            "target": "Trader FLAIR verdict WIN vs LOSS",
            "outcome_basis": outcome_basis,
            "basis_pooling": False,
            "outcome_score_used_as_target": False,
            "excluded_targets": ["NEUTRAL", "UNKNOWN", "pending", "missing"],
            "universe_outcomes_used_as_features": False,
            "split": "chronological_grouped_by_cycle_with_embargo_and_realized_close_purge",
            "embargo_hours": embargo_hours,
            "realized_close_purge_enabled": purge_realized,
        },
        "coverage": coverage,
        "split": split,
        "readiness": {
            "status": "GO" if not reasons else "NO_GO",
            "reasons": reasons,
            "thresholds": {
                "min_class_support": min_class_support,
                "min_train_class_support": min_train_class_support,
                "min_test_class_support": min_test_class_support,
            },
            "total_support": overall_readiness["total_support"],
            "train_support": overall_readiness["train_support"],
            "test_support": overall_readiness["test_support"],
            "by_outcome_basis": readiness_by_basis,
        },
        "models": {},
        "lift_enriched_vs_base": {},
        "test_slices": {"outcome_basis": {}, "decision_time_feature_slices": []},
        "caveats": [
            "predictive lift is not causal proof that the Universe mandate improves trading",
            "this is a Trader FLAIR critic ablation and not a model of market dynamics",
            "reconstructed Universe context is reported separately from context observed in the prompt",
            "feature slices are descriptive held-out associations and are not multiple-testing corrected",
            "realized and counterfactual FLAIR are gated separately and are never pooled in one model",
            "configuration changes can be time-confounded even after the chronological split",
        ],
    }
    if not train or not test:
        return report

    train_labels = [row.label for row in train]
    test_labels = [row.label for row in test]
    majority_probability = _majority_probabilities(train_labels, alpha=alpha)
    majority_probabilities = [majority_probability for _row in test]
    base_probabilities, base_meta = _fit_predict(train, test, enriched=False, alpha=alpha)
    enriched_probabilities, enriched_meta = _fit_predict(train, test, enriched=True, alpha=alpha)
    majority_metrics = classification_metrics(test_labels, majority_probabilities)
    base_metrics = classification_metrics(test_labels, base_probabilities)
    enriched_metrics = classification_metrics(test_labels, enriched_probabilities)
    report["models"] = {
        "majority": {
            "kind": "smoothed_train_majority",
            "alpha": alpha,
            "class_probabilities": {label: round(majority_probability[label], 6) for label in LABELS},
            "metrics": majority_metrics,
        },
        "base": base_meta | {"metrics": base_metrics},
        "base_plus_universe": enriched_meta | {"metrics": enriched_metrics},
    }
    report["lift_enriched_vs_base"] = _metric_lift(base_metrics, enriched_metrics)
    probability_sets = {
        "majority": majority_probabilities,
        "base": base_probabilities,
        "base_plus_universe": enriched_probabilities,
    }
    report["test_slices"] = {
        "outcome_basis": _basis_slices(test, probability_sets),
        "decision_time_feature_slices": _descriptive_feature_slices(
            test,
            minimum_support=max(1, slice_min_support),
        ),
    }
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="trajectories.jsonl or cross_loop_episodes.jsonl")
    parser.add_argument("--test-fraction", type=float, default=0.2)
    parser.add_argument("--alpha", type=float, default=1.0, help="Laplace smoothing")
    parser.add_argument("--min-class-support", type=int, default=20)
    parser.add_argument("--min-train-class-support", type=int, default=10)
    parser.add_argument("--min-test-class-support", type=int, default=5)
    parser.add_argument("--slice-min-support", type=int, default=3)
    parser.add_argument(
        "--outcome-basis",
        choices=("counterfactual", "realized"),
        default="counterfactual",
        help="fit one reward basis only; bases are never pooled",
    )
    parser.add_argument("--embargo-hours", type=float, default=24.0)
    parser.add_argument(
        "--no-purge-realized",
        action="store_true",
        help="diagnostic only: keep realized labels without checking close time",
    )
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.input.is_file():
        raise SystemExit(f"input JSONL not found: {args.input}")
    rows, malformed = read_jsonl(args.input)
    report = run_analysis(
        rows,
        test_fraction=args.test_fraction,
        alpha=args.alpha,
        min_class_support=args.min_class_support,
        min_train_class_support=args.min_train_class_support,
        min_test_class_support=args.min_test_class_support,
        slice_min_support=args.slice_min_support,
        embargo_hours=args.embargo_hours,
        purge_realized=not args.no_purge_realized,
        outcome_basis=args.outcome_basis,
    )
    report["input"] = {
        "path": str(args.input),
        "rows": len(rows),
        "malformed_rows": malformed,
    }
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":") if args.compact else None,
            indent=None if args.compact else 2,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
