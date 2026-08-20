"""Live KPI read model projected from persisted runtime state files."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from backtest.metrics import Metrics, compute_metrics
from trader.reporting.read_models.trade_history import (
    aggregate_position_cycles,
    attribute_position_cycles_to_experiments,
    compute_round_trips,
)


def _stale_economic_fields(row: dict) -> set[str]:
    quality = row.get("economic_fields_quality")
    if not isinstance(quality, dict) or quality.get("status") != "stale":
        return set()
    fields = quality.get("fields")
    if not isinstance(fields, list):
        return set()
    return {str(field) for field in fields}


_GBP_MIGRATION_KEY = "gbp_minor_quotes_v1"
_GBP_SCOPE_PREFIX = f"{_GBP_MIGRATION_KEY}:scope:v1:"
_GBP_SCOPE_RE = re.compile(
    rf"^{re.escape(_GBP_SCOPE_PREFIX)}"
    r"(?P<through_seq>[1-9][0-9]*):"
    r"(?P<candidate_count>[1-9][0-9]*):"
    r"(?P<quality_through_seq>[1-9][0-9]*):"
    r"(?P<raw_sha256>[0-9a-f]{64}):"
    r"(?P<normalized_sha256>[0-9a-f]{64})$"
)


def _aware_iso_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def read_gbp_history_attestation(state_dir: Path) -> dict:
    """Read the two-row GBP migration authority without guessing.

    A missing database or a database with neither sentinel is a legitimate
    absence.  Any database/read error, partial pair, ambiguous scope or invalid
    descriptor is explicitly unavailable so historical economics fail closed.
    """

    db_path = state_dir / "casys.db"
    if not db_path.exists():
        return {
            "status": "absent",
            "reason": None,
            "cutoff": None,
            "scope": None,
        }
    try:
        from trader.infrastructure.state_db.connection import open_state_db

        rows = open_state_db(db_path).query_all(
            "SELECT store, imported_at FROM state_imports "
            "WHERE store=? OR store GLOB ? ORDER BY store",
            (_GBP_MIGRATION_KEY, f"{_GBP_SCOPE_PREFIX}*"),
        )
    except Exception as exc:
        return {
            "status": "unavailable",
            "reason": "state_imports_unreadable",
            "detail": type(exc).__name__,
            "cutoff": None,
            "scope": None,
        }

    primary_rows = [row for row in rows if row["store"] == _GBP_MIGRATION_KEY]
    scope_rows = [
        row
        for row in rows
        if str(row["store"] or "").startswith(_GBP_SCOPE_PREFIX)
    ]
    if not primary_rows and not scope_rows:
        return {
            "status": "absent",
            "reason": None,
            "cutoff": None,
            "scope": None,
        }
    if len(primary_rows) != 1 or len(scope_rows) != 1:
        return {
            "status": "unavailable",
            "reason": "sentinel_pair_partial_or_ambiguous",
            "cutoff": None,
            "scope": None,
        }

    primary_at = str(primary_rows[0]["imported_at"] or "").strip()
    scope_at = str(scope_rows[0]["imported_at"] or "").strip()
    if (
        primary_at != scope_at
        or _aware_iso_datetime(primary_at) is None
        or _aware_iso_datetime(scope_at) is None
    ):
        return {
            "status": "unavailable",
            "reason": "sentinel_timestamps_invalid_or_divergent",
            "cutoff": None,
            "scope": None,
        }

    scope_store = str(scope_rows[0]["store"] or "")
    match = _GBP_SCOPE_RE.fullmatch(scope_store)
    if match is None:
        return {
            "status": "unavailable",
            "reason": "sentinel_scope_invalid",
            "cutoff": None,
            "scope": None,
        }
    scope = {
        "through_seq": int(match.group("through_seq")),
        "candidate_count": int(match.group("candidate_count")),
        "quality_through_seq": int(match.group("quality_through_seq")),
        "raw_sha256": match.group("raw_sha256"),
        "normalized_sha256": match.group("normalized_sha256"),
    }
    if scope["quality_through_seq"] != scope["through_seq"]:
        return {
            "status": "unavailable",
            "reason": "sentinel_scope_invalid",
            "cutoff": None,
            "scope": None,
        }
    return {
        "status": "valid",
        "reason": None,
        "cutoff": primary_at,
        "scope": scope,
    }


def _iso_timestamp(value: object) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def read_equity_curve(state_dir: Path) -> tuple[list[tuple[str, float]], dict]:
    """Return trustworthy equity points plus an explicit quality authority."""

    attestation = read_gbp_history_attestation(state_dir)
    cutoff = (
        str(attestation["cutoff"])
        if attestation["status"] == "valid"
        else None
    )
    cutoff_ts = _iso_timestamp(cutoff) if cutoff is not None else None
    rows_total = 0
    rows_excluded = 0
    rows_invalid = 0
    rows_non_economic = 0
    curve: list[tuple[str, float]] = []
    history_path = state_dir / "history.jsonl"
    read_error: str | None = None
    if not history_path.exists():
        read_error = "history_missing"
    else:
        try:
            lines = history_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            lines = []
            read_error = f"history_unreadable:{type(exc).__name__}"
        for line in lines:
            line = line.strip()
            if not line:
                continue
            rows_total += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                rows_invalid += 1
                continue
            if not isinstance(row, Mapping):
                rows_invalid += 1
                continue
            ts = str(row.get("ts") or "").strip()
            row_ts = _iso_timestamp(ts)
            if row_ts is None:
                rows_invalid += 1
                continue
            if cutoff_ts is not None and row_ts <= cutoff_ts:
                rows_excluded += 1
                continue
            if row.get("equity") is None:
                rows_non_economic += 1
                continue
            equity = _finite_float(row.get("equity"))
            if equity is None or equity <= 0.0:
                rows_invalid += 1
                continue
            curve.append((ts, equity))

    if attestation["status"] == "unavailable":
        status = "unavailable"
        reason = f"migration_attestation:{attestation['reason']}"
    elif read_error is not None:
        status = "unavailable"
        reason = read_error
    elif rows_invalid:
        status = "unavailable"
        reason = "history_malformed"
    elif len(curve) < 2:
        status = "unavailable"
        reason = (
            "insufficient_post_migration_history"
            if cutoff is not None
            else "insufficient_history"
        )
    elif cutoff is not None:
        status = "post_migration_only"
        reason = "gbp_minor_quotes_v1_pre_migration_history_excluded"
    else:
        status = "complete"
        reason = None

    economic_metrics_available = status in {"complete", "post_migration_only"}
    if not economic_metrics_available:
        curve = []
    return curve, {
        "status": status,
        "reason": reason,
        "cutoff": cutoff,
        "rows_total": rows_total,
        "rows_used": len(curve),
        "rows_excluded": rows_excluded,
        "rows_invalid": rows_invalid,
        "rows_non_economic": rows_non_economic,
        "economic_metrics_available": economic_metrics_available,
        "migration_attestation": attestation,
    }


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _validated_positions(raw: object) -> dict[str, dict]:
    if not isinstance(raw, Mapping):
        raise ValueError("broker positions invalides")
    positions: dict[str, dict] = {}
    for key, candidate in raw.items():
        if not isinstance(candidate, Mapping):
            raise ValueError("broker position invalide")
        symbol = str(candidate.get("symbol") or key or "").strip()
        quantity = _finite_float(candidate.get("quantity"))
        avg_price = _finite_float(candidate.get("avg_price", 0.0))
        if not symbol or quantity is None or avg_price is None or avg_price < 0.0:
            raise ValueError(f"broker position invalide: {symbol or key!r}")
        positions[symbol] = {
            "symbol": symbol,
            "quantity": quantity,
            "avg_price": avg_price,
        }
    return positions


def _broker_failure(*, source: str, reason: str, exc: Exception) -> dict:
    return {
        "status": "unavailable",
        "source": source,
        "reason": reason,
        "detail": type(exc).__name__,
    }


def compute_experiment_performance(
    state_dir: Path,
    cycles: Iterable[Mapping[str, object]],
) -> list[dict]:
    """Aggregate canonical flat-to-flat economics by causal experiment."""

    attributed = attribute_position_cycles_to_experiments(state_dir, cycles)
    groups: dict[str, dict] = {}
    for cycle in attributed:
        cohort = str(cycle["experiment_cohort"])
        group = groups.setdefault(
            cohort,
            {
                "cohort": cohort,
                "experiment_id": cycle.get("experiment_id"),
                "experiment_ids": set(),
                "attribution_status": cycle["experiment_attribution_status"],
                "attribution_quality": cycle.get(
                    "experiment_attribution_quality",
                    {"status": "available", "reason": None, "source": None},
                ),
                "_attribution_reasons": Counter(),
                "completed_cycles": 0,
                "wins": 0,
                "gross_pnl": 0.0,
                "commissions": 0.0,
                "net_pnl": 0.0,
                "_commission_complete": True,
                "_complete_commission_cycles": 0,
                "_incomplete_commission_cycles": 0,
                "_commission_models": set(),
                "_commission_reasons": set(),
                "provider": cycle.get("provider") or "unknown",
                "model": cycle.get("model") or "unknown",
                "profile": cycle.get("profile"),
                "components": cycle.get("experiment_components"),
            },
        )
        group["completed_cycles"] += 1
        gross = _finite_float(cycle.get("gross_pnl"))
        if gross is None:
            raise ValueError("cycle experiment gross economics invalides")
        updated_gross = group["gross_pnl"] + gross
        if not math.isfinite(updated_gross):
            raise ValueError("experiment cohort economics overflow: gross_pnl")
        group["gross_pnl"] = updated_gross

        commission_quality = cycle.get("commission_quality")
        commission_available = (
            isinstance(commission_quality, Mapping)
            and commission_quality.get("status") == "available"
        )
        commissions = _finite_float(cycle.get("commission"))
        net = _finite_float(cycle.get("pnl"))
        if commission_available and (commissions is None or net is None):
            raise ValueError("cycle experiment net economics invalides")
        if not commission_available:
            group["_commission_complete"] = False
            group["_incomplete_commission_cycles"] += 1
            if isinstance(commission_quality, Mapping):
                models = commission_quality.get("models")
                if isinstance(models, list):
                    group["_commission_models"].update(
                        str(value) for value in models
                    )
                reasons = commission_quality.get("reasons")
                if isinstance(reasons, list):
                    group["_commission_reasons"].update(
                        str(value) for value in reasons
                    )
                elif commission_quality.get("reason"):
                    group["_commission_reasons"].add(
                        str(commission_quality["reason"])
                    )
        else:
            group["_complete_commission_cycles"] += 1
        for field, value in (
            ("commissions", commissions),
            ("net_pnl", net),
        ):
            if value is None:
                continue
            updated = group[field] + value
            if not math.isfinite(updated):
                raise ValueError(
                    f"experiment cohort economics overflow: {field}"
                )
            group[field] = updated
        if net is not None and net > 0.0:
            group["wins"] += 1
        group["experiment_ids"].update(cycle.get("experiment_ids") or [])
        reason = cycle.get("experiment_attribution_reason")
        if reason:
            group["_attribution_reasons"][str(reason)] += 1
        cycle_quality = cycle.get("experiment_attribution_quality")
        if (
            isinstance(cycle_quality, Mapping)
            and cycle_quality.get("status") == "unavailable"
        ):
            group["attribution_quality"] = dict(cycle_quality)

    rows: list[dict] = []
    for group in groups.values():
        completed = group["completed_cycles"]
        commission_complete = group["_commission_complete"]
        commission_reasons = sorted(group["_commission_reasons"])
        commission_quality = {
            "status": (
                "available" if commission_complete else "unavailable"
            ),
            "reason": (
                None
                if commission_complete
                else (
                    commission_reasons[0]
                    if len(commission_reasons) == 1
                    else "multiple_commission_quality_failures"
                )
            ),
            "models": sorted(group["_commission_models"]),
            "reasons": commission_reasons,
            "complete_cycles": group["_complete_commission_cycles"],
            "incomplete_cycles": group["_incomplete_commission_cycles"],
        }
        rows.append(
            {
                "cohort": group["cohort"],
                "experiment_id": group["experiment_id"],
                "experiment_ids": sorted(group["experiment_ids"]),
                "attribution_status": group["attribution_status"],
                "attribution_quality": group["attribution_quality"],
                "attribution_reasons": dict(sorted(group["_attribution_reasons"].items())),
                "completed_cycles": completed,
                "wins": group["wins"] if commission_complete else None,
                "gross_pnl": group["gross_pnl"],
                "commissions": (
                    group["commissions"] if commission_complete else None
                ),
                "net_pnl": group["net_pnl"] if commission_complete else None,
                "win_rate": (
                    group["wins"] / completed
                    if commission_complete
                    else None
                ),
                "commission_quality": commission_quality,
                "provider": group["provider"],
                "model": group["model"],
                "profile": group["profile"],
                "components": group["components"],
            }
        )
    rows.sort(key=lambda row: str(row["cohort"]))
    return rows


def _read_model_performance_rows(
    state_dir: Path,
) -> tuple[list[dict], dict[str, object]]:
    path = state_dir / "model_performance.jsonl"
    if not path.exists():
        return [], {
            "status": "absent",
            "reason": None,
            "rows_total": 0,
            "rows_used": 0,
            "rows_invalid": 0,
        }
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return [], {
            "status": "unavailable",
            "reason": "model_performance_projection_unreadable",
            "detail": type(exc).__name__,
            "rows_total": 0,
            "rows_used": 0,
            "rows_invalid": 0,
        }

    source_rows: list[dict] = []
    rows_total = 0
    rows_invalid = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        rows_total += 1
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            rows_invalid += 1
            continue
        if not isinstance(row, dict):
            rows_invalid += 1
            continue
        source_rows.append(row)
    return source_rows, {
        "status": "degraded" if rows_invalid else "available",
        "reason": (
            "model_performance_projection_malformed"
            if rows_invalid
            else None
        ),
        "rows_total": rows_total,
        "rows_used": len(source_rows),
        "rows_invalid": rows_invalid,
    }


def _aggregate_model_performance(source_rows: Iterable[Mapping]) -> list[dict]:
    groups: dict[tuple[str, str], dict] = {}
    for row in source_rows:
        provider = str(row.get("llm_provider") or "unknown")
        model = str(row.get("llm_model") or "unknown")
        key = (provider, model)
        group = groups.setdefault(
            key,
            {
                "provider": provider,
                "model": model,
                "fills": 0,
                "symbols": set(),
                "fallbacks": 0,
                "first_equity": None,
                "last_equity": None,
                "_confidence_sum": 0.0,
                "_confidence_n": 0,
            },
        )
        group["fills"] += 1
        if row.get("symbol"):
            group["symbols"].add(str(row["symbol"]))
        if row.get("llm_fallback_reason"):
            group["fallbacks"] += 1
        confidence = _finite_float(row.get("confidence"))
        if confidence is not None:
            updated_confidence = group["_confidence_sum"] + confidence
            if math.isfinite(updated_confidence):
                group["_confidence_sum"] = updated_confidence
                group["_confidence_n"] += 1
        if (
            row.get("equity") is not None
            and "equity" not in _stale_economic_fields(dict(row))
        ):
            equity = _finite_float(row.get("equity"))
            if equity is not None:
                if group["first_equity"] is None:
                    group["first_equity"] = equity
                group["last_equity"] = equity

    rows: list[dict] = []
    for group in groups.values():
        first_equity = group["first_equity"]
        last_equity = group["last_equity"]
        confidence_n = group["_confidence_n"]
        rows.append(
            {
                "provider": group["provider"],
                "model": group["model"],
                "fills": group["fills"],
                "symbols": sorted(group["symbols"]),
                "fallbacks": group["fallbacks"],
                "first_equity": first_equity,
                "last_equity": last_equity,
                "portfolio_equity_delta": (
                    None
                    if first_equity is None or last_equity is None
                    else _finite_float(last_equity - first_equity)
                ),
                "avg_confidence": (None if confidence_n == 0 else group["_confidence_sum"] / confidence_n),
            }
        )
    rows.sort(key=lambda row: (row["provider"], row["model"]))
    return rows


def compute_model_performance(state_dir: Path) -> list[dict]:
    source_rows, _ = _read_model_performance_rows(state_dir)
    return _aggregate_model_performance(source_rows)


def _trade_economics_quality(
    completed_cycles: list[dict] | None,
    *,
    broker_quality: Mapping[str, object],
    unavailable_reason: str | None = None,
) -> dict[str, object]:
    if completed_cycles is None:
        reason = unavailable_reason or str(
            broker_quality.get("reason") or "broker_unavailable"
        )
        return {
            "status": "unavailable",
            "reason": reason,
            "models": [],
            "reasons": [reason],
            "completed_cycles": None,
            "complete_cycles": None,
            "incomplete_cycles": None,
            "gross_pnl_available": False,
            "commission_and_net_available": False,
        }

    incomplete = 0
    models: set[str] = set()
    reasons: set[str] = set()
    for cycle in completed_cycles:
        quality = cycle.get("commission_quality")
        if (
            isinstance(quality, Mapping)
            and quality.get("status") == "available"
        ):
            continue
        incomplete += 1
        if isinstance(quality, Mapping):
            raw_models = quality.get("models")
            if isinstance(raw_models, list):
                models.update(str(value) for value in raw_models)
            raw_reasons = quality.get("reasons")
            if isinstance(raw_reasons, list):
                reasons.update(str(value) for value in raw_reasons)
            elif quality.get("reason"):
                reasons.add(str(quality["reason"]))
        else:
            reasons.add("commission_quality_missing")
    total = len(completed_cycles)
    complete = total - incomplete
    return {
        "status": "complete" if incomplete == 0 else "incomplete",
        "reason": (
            None
            if incomplete == 0
            else "commission_or_net_economics_incomplete"
        ),
        "models": sorted(models),
        "reasons": sorted(reasons),
        "completed_cycles": total,
        "complete_cycles": complete,
        "incomplete_cycles": incomplete,
        "gross_pnl_available": True,
        "commission_and_net_available": incomplete == 0,
    }


def compute_live_kpis(state_dir: Path) -> dict:
    """Compute live KPIs from history, broker state and model-performance logs."""
    from trader.support.config.portfolio import load_starting_cash

    starting_equity: float = load_starting_cash(state_dir.parent / "config")

    equity_curve, history_quality = read_equity_curve(state_dir)
    model_performance_rows, model_performance_quality = (
        _read_model_performance_rows(state_dir)
    )
    model_performance = _aggregate_model_performance(
        model_performance_rows
    )

    fills: list[dict] = []
    positions_raw: dict[str, dict] = {}
    current_cash: float | None = starting_equity
    broker_quality: dict = {
        "status": "absent",
        "source": None,
        "reason": None,
        "detail": None,
    }
    trade_economics_unavailable_reason: str | None = None

    db_path = state_dir / "casys.db"
    if db_path.exists():
        try:
            from trader.infrastructure.state_db.broker_store import SqliteBroker
            from trader.infrastructure.state_db.connection import open_state_db

            broker = SqliteBroker(open_state_db(db_path))
            fills = broker.fills()
            positions_raw = _validated_positions({
                symbol: {
                    "symbol": position.symbol,
                    "quantity": position.quantity,
                    "avg_price": position.avg_price,
                }
                for symbol, position in broker.positions().items()
            })
            current_cash = _finite_float(broker.cash())
            if current_cash is None:
                raise ValueError("broker cash invalide")
            broker_quality = {
                "status": "available",
                "source": "sqlite",
                "reason": None,
                "detail": None,
            }
        except Exception as exc:
            fills = []
            positions_raw = {}
            current_cash = None
            broker_quality = _broker_failure(
                source="sqlite",
                reason="broker_read_error",
                exc=exc,
            )
    elif (broker_path := state_dir / "broker.json").exists():
        try:
            broker_data = json.loads(broker_path.read_text(encoding="utf-8"))
            if not isinstance(broker_data, Mapping):
                raise ValueError("broker JSON invalide")
            raw_fills = broker_data.get("fills", [])
            if not isinstance(raw_fills, list):
                raise ValueError("broker fills invalides")
            fills = raw_fills
            positions_raw = _validated_positions(
                broker_data.get("positions", {})
            )
            current_cash = _finite_float(
                broker_data.get("cash", starting_equity)
            )
            if current_cash is None:
                raise ValueError("broker cash invalide")
            broker_quality = {
                "status": "available",
                "source": "json",
                "reason": None,
                "detail": None,
            }
        except Exception as exc:
            fills = []
            positions_raw = {}
            current_cash = None
            broker_quality = _broker_failure(
                source="json",
                reason="broker_read_error",
                exc=exc,
            )

    last_equity: float | None = equity_curve[-1][1] if equity_curve else None
    positions_list = [
        {
            "symbol": value["symbol"],
            "quantity": value["quantity"],
            "avg_price": value.get("avg_price", 0.0),
        }
        for value in positions_raw.values()
        if value.get("quantity", 0) != 0
    ]

    completed_cycles: list[dict] | None
    experiment_performance: list[dict] | None
    if broker_quality["status"] == "unavailable":
        completed_cycles = None
        experiment_performance = None
    else:
        try:
            completed_cycles = aggregate_position_cycles(
                compute_round_trips(state_dir, fills=fills)
            )
            experiment_performance = compute_experiment_performance(
                state_dir,
                completed_cycles,
            )
        except (KeyError, OverflowError, TypeError, ValueError) as exc:
            completed_cycles = None
            experiment_performance = None
            trade_economics_unavailable_reason = "canonical_fills_invalid"
            broker_quality = {
                **broker_quality,
                "economic_projection_error": type(exc).__name__,
            }
    metrics: Metrics = compute_metrics(equity_curve, fills, starting_equity)
    history_metrics_available = history_quality["economic_metrics_available"]
    trade_economics_quality = _trade_economics_quality(
        completed_cycles,
        broker_quality=broker_quality,
        unavailable_reason=trade_economics_unavailable_reason,
    )

    return {
        "equity": last_equity,
        "cash": current_cash,
        "total_return": metrics.total_return if history_metrics_available else None,
        "max_drawdown": metrics.max_drawdown if history_metrics_available else None,
        "period_win_rate": (metrics.period_win_rate if history_metrics_available else None),
        "volatility": metrics.volatility if history_metrics_available else None,
        "sharpe": metrics.sharpe if history_metrics_available else None,
        "history_quality": history_quality,
        "broker_quality": broker_quality,
        "trade_economics_quality": trade_economics_quality,
        # Compatibility key with corrected semantics: a trade is one completed
        # flat-to-flat position cycle, not a broker fill.
        "num_trades": (
            len(completed_cycles) if completed_cycles is not None else None
        ),
        "num_closed_position_cycles": (
            len(completed_cycles) if completed_cycles is not None else None
        ),
        "num_fills": (
            len(fills)
            if broker_quality["status"] != "unavailable"
            else None
        ),
        "trade_count_grain": "flat_to_flat_position_cycle",
        "n_positions": (
            len(positions_list)
            if broker_quality["status"] != "unavailable"
            else None
        ),
        "positions": (
            positions_list
            if broker_quality["status"] != "unavailable"
            else None
        ),
        "model_performance": model_performance,
        "model_performance_quality": model_performance_quality,
        "experiment_performance": experiment_performance,
    }
