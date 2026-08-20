"""Canonical trade-history reconstruction and regime filtering read model."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from pathlib import Path

_FLAT_EPS = 1e-9


def _read_perf_rows(state_dir: Path) -> list[dict]:
    path = state_dir / "model_performance.jsonl"
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _holding_minutes(entry_ts: str, exit_ts: str) -> float | None:
    try:
        entry = datetime.fromisoformat(entry_ts)
        exit_ = datetime.fromisoformat(exit_ts)
    except ValueError:
        return None
    return (exit_ - entry).total_seconds() / 60.0


class _OpenLeg:
    """Signed open position with averaged entry metadata."""

    def __init__(self) -> None:
        self.qty = 0.0
        self.avg_price = 0.0
        self.commission = 0.0
        self.entry_ts: str | None = None
        self._conf_sum = 0.0
        self._conf_qty = 0.0
        self.entry_fx_rate = 1.0
        self.entry_commission_currency = "USD"
        self.entry_decision_ids: list[str] = []
        self.position_cycle_id: str | None = None

    @property
    def entry_confidence(self) -> float | None:
        return self._conf_sum / self._conf_qty if self._conf_qty else None

    def open_or_add(
        self,
        *,
        added_qty: float,
        price: float,
        ts: str,
        confidence: float | None,
        commission: float = 0.0,
        fx_rate: float = 1.0,
        commission_currency: str = "USD",
        decision_id: str | None = None,
        position_cycle_id: str | None = None,
    ) -> None:
        if self.qty == 0.0:
            self.entry_ts = ts
            self._conf_sum = 0.0
            self._conf_qty = 0.0
            self.commission = 0.0
            self.entry_fx_rate = fx_rate
            self.entry_commission_currency = commission_currency
            self.entry_decision_ids = []
            self.position_cycle_id = position_cycle_id
        new_qty = self.qty + added_qty
        self.avg_price = (
            self.avg_price * abs(self.qty) + price * abs(added_qty)
        ) / abs(new_qty)
        self.qty = new_qty
        self.commission += max(commission, 0.0)
        if confidence is not None:
            self._conf_sum += confidence * abs(added_qty)
            self._conf_qty += abs(added_qty)
        if decision_id and decision_id not in self.entry_decision_ids:
            self.entry_decision_ids.append(decision_id)

    def scale_entry_weight(self, factor: float) -> None:
        self._conf_sum *= factor
        self._conf_qty *= factor


def _as_non_negative_float(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed) or parsed < 0.0:
        return 0.0
    return parsed


def compute_round_trips(state_dir: Path) -> list[dict]:
    """Reconstruct realised exit legs from append-only model-performance fills.

    A partial exit is intentionally emitted immediately: callers that analyse
    exit mechanisms need one observation per realised leg.  Headline outcome
    metrics must collapse these rows with :func:`aggregate_position_cycles`.
    """

    rows = _read_perf_rows(state_dir)
    rows.sort(key=lambda row: (str(row.get("symbol")), str(row.get("ts"))))
    legs: dict[str, _OpenLeg] = {}
    cycle_numbers: dict[str, int] = {}
    trips: list[dict] = []

    for row in rows:
        symbol = str(row.get("symbol"))
        action = str(row.get("action", "")).upper()
        if action not in ("BUY", "SELL") or row.get("symbol") is None:
            continue
        try:
            qty = abs(float(row["quantity"]))
            price = float(row["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            not math.isfinite(qty)
            or not math.isfinite(price)
            or qty <= 0.0
            or price <= 0.0
        ):
            continue

        ts = str(row.get("ts"))
        confidence = row.get("confidence")
        confidence = (
            float(confidence) if isinstance(confidence, (int, float)) else None
        )
        row_commission = _as_non_negative_float(row.get("commission"))
        row_fx_rate = _as_non_negative_float(row.get("fx_rate")) or 1.0
        row_commission_currency = str(
            row.get("commission_currency") or "USD"
        )
        row_decision_id = str(row.get("decision_id") or "").strip() or None
        signed = qty if action == "BUY" else -qty
        leg = legs.setdefault(symbol, _OpenLeg())

        if abs(leg.qty) <= _FLAT_EPS or (leg.qty > 0) == (signed > 0):
            position_cycle_id = None
            if abs(leg.qty) <= _FLAT_EPS:
                cycle_numbers[symbol] = cycle_numbers.get(symbol, 0) + 1
                position_cycle_id = f"{symbol}:{cycle_numbers[symbol]}"
            leg.open_or_add(
                added_qty=signed,
                price=price,
                ts=ts,
                confidence=confidence,
                commission=row_commission,
                fx_rate=row_fx_rate,
                commission_currency=row_commission_currency,
                decision_id=row_decision_id,
                position_cycle_id=position_cycle_id,
            )
            continue

        closing_qty = min(qty, abs(leg.qty))
        entry_sign = 1.0 if leg.qty > 0 else -1.0
        old_abs = abs(leg.qty)
        entry_commission = (
            leg.commission * (closing_qty / old_abs) if old_abs > 0 else 0.0
        )
        exit_commission = (
            row_commission * (closing_qty / qty) if qty > 0 else 0.0
        )
        gross_pnl_native = (price - leg.avg_price) * closing_qty * entry_sign
        gross_pnl_usd = gross_pnl_native * row_fx_rate
        entry_commission_usd = entry_commission * leg.entry_fx_rate
        exit_commission_usd = exit_commission * row_fx_rate
        total_commission_usd = entry_commission_usd + exit_commission_usd
        trips.append(
            {
                "symbol": symbol,
                "side": "LONG" if leg.qty > 0 else "SHORT",
                "quantity": closing_qty,
                "entry_price": leg.avg_price,
                "exit_price": price,
                "gross_pnl": gross_pnl_usd,
                "commission": total_commission_usd,
                "pnl": gross_pnl_usd - total_commission_usd,
                "entry_ts": leg.entry_ts,
                "exit_ts": ts,
                "holding_minutes": _holding_minutes(leg.entry_ts or ts, ts),
                "entry_confidence": leg.entry_confidence,
                "exit_reason": row.get("exit_reason")
                or str(row.get("intent") or ""),
                "source_plan_id": row.get("source_plan_id"),
                "entry_decision_id": (
                    leg.entry_decision_ids[0]
                    if len(leg.entry_decision_ids) == 1
                    else None
                ),
                "entry_decision_ids": list(leg.entry_decision_ids),
                "position_cycle_id": leg.position_cycle_id,
                "position_cycle_closed": closing_qty >= old_abs - _FLAT_EPS,
            }
        )

        remaining = qty - closing_qty
        leg.qty += signed
        if abs(leg.qty) <= _FLAT_EPS:
            legs[symbol] = _OpenLeg()
        elif remaining > _FLAT_EPS:
            fresh = _OpenLeg()
            cycle_numbers[symbol] = cycle_numbers.get(symbol, 0) + 1
            fresh.open_or_add(
                added_qty=entry_sign * -remaining,
                price=price,
                ts=ts,
                confidence=confidence,
                commission=max(row_commission - exit_commission, 0.0),
                fx_rate=row_fx_rate,
                commission_currency=row_commission_currency,
                decision_id=row_decision_id,
                position_cycle_id=f"{symbol}:{cycle_numbers[symbol]}",
            )
            legs[symbol] = fresh
        else:
            leg.commission = max(leg.commission - entry_commission, 0.0)
            leg.scale_entry_weight(abs(leg.qty) / old_abs)

    return trips


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _unique_text(rows: Iterable[Mapping[str, object]], key: str) -> list[str]:
    values: list[str] = []
    for row in rows:
        raw = row.get(key)
        candidates = raw if isinstance(raw, list) else [raw]
        for candidate in candidates:
            value = str(candidate or "").strip()
            if value and value not in values:
                values.append(value)
    return values


def aggregate_position_cycles(
    trips: Iterable[Mapping[str, object]],
) -> list[dict]:
    """Collapse realised exit legs into completed flat-to-flat position cycles.

    Rows carrying a ``position_cycle_id`` are emitted only once the position is
    flat.  Legacy rows without that identifier remain standalone observations,
    preserving backward compatibility with pre-cycle history and unit callers.
    """

    grouped: dict[tuple[str, str, str], list[Mapping[str, object]]] = {}
    for index, trip in enumerate(trips):
        cycle_id = str(trip.get("position_cycle_id") or "").strip()
        if cycle_id:
            key = ("cycle", str(trip.get("symbol") or ""), cycle_id)
        else:
            key = ("legacy_leg", "", str(index))
        grouped.setdefault(key, []).append(trip)

    cycles: list[dict] = []
    for (kind, _, cycle_key), source_rows in grouped.items():
        if kind == "legacy_leg":
            legacy = dict(source_rows[0])
            legacy.setdefault("position_cycle_closed", True)
            legacy.setdefault("exit_leg_count", 1)
            cycles.append(legacy)
            continue
        if kind == "cycle" and not any(
            row.get("position_cycle_closed") is True for row in source_rows
        ):
            continue

        rows = sorted(source_rows, key=lambda row: str(row.get("exit_ts") or ""))
        first = rows[0]
        last = rows[-1]
        quantities = [
            abs(quantity)
            for row in rows
            if (quantity := _finite_float(row.get("quantity"))) is not None
        ]
        total_quantity = sum(quantities)

        def summed(field: str) -> float:
            return sum(
                parsed
                for row in rows
                if (parsed := _finite_float(row.get(field))) is not None
            )

        def quantity_weighted(field: str) -> float | None:
            weighted = 0.0
            weight = 0.0
            for row in rows:
                value = _finite_float(row.get(field))
                quantity = _finite_float(row.get("quantity"))
                if value is None or quantity is None:
                    continue
                row_weight = abs(quantity)
                weighted += value * row_weight
                weight += row_weight
            return weighted / weight if weight > 0.0 else None

        entry_decision_ids: list[str] = []
        for row in rows:
            raw_ids = row.get("entry_decision_ids")
            candidates = raw_ids if isinstance(raw_ids, list) else []
            if not candidates and row.get("entry_decision_id"):
                candidates = [row["entry_decision_id"]]
            for candidate in candidates:
                decision_id = str(candidate or "").strip()
                if decision_id and decision_id not in entry_decision_ids:
                    entry_decision_ids.append(decision_id)

        entry_ts = str(first.get("entry_ts") or "") or None
        exit_ts = str(last.get("exit_ts") or "") or None
        exit_reasons = _unique_text(rows, "exit_reason")
        source_plan_ids = _unique_text(rows, "source_plan_id")
        cycles.append(
            {
                "symbol": first.get("symbol"),
                "side": first.get("side"),
                "quantity": total_quantity,
                "entry_price": quantity_weighted("entry_price"),
                "exit_price": quantity_weighted("exit_price"),
                "gross_pnl": summed("gross_pnl"),
                "commission": summed("commission"),
                "pnl": summed("pnl"),
                "entry_ts": entry_ts,
                "exit_ts": exit_ts,
                "holding_minutes": (
                    _holding_minutes(entry_ts, exit_ts)
                    if entry_ts is not None and exit_ts is not None
                    else None
                ),
                "entry_confidence": quantity_weighted("entry_confidence"),
                # The final leg names what flattened the position.  All leg
                # mechanisms remain explicit for attribution below.
                "exit_reason": last.get("exit_reason"),
                "exit_reasons": exit_reasons,
                "source_plan_id": (
                    source_plan_ids[0] if len(source_plan_ids) == 1 else None
                ),
                "source_plan_ids": source_plan_ids,
                "entry_decision_id": (
                    entry_decision_ids[0]
                    if len(entry_decision_ids) == 1
                    else None
                ),
                "entry_decision_ids": entry_decision_ids,
                "position_cycle_id": (
                    cycle_key if kind == "cycle" else first.get("position_cycle_id")
                ),
                "position_cycle_closed": True,
                "exit_leg_count": len(rows),
            }
        )
    return cycles


def _parse_iso_date(value: str, *, field: str) -> date:
    raw = value.strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ValueError(
            f"{field} doit être une date/datetime ISO: {value!r}"
        ) from exc


def _normalize_excluded_symbols(
    exclude_symbols: tuple[str, ...] | frozenset[str],
) -> tuple[str, ...]:
    raw_symbols = (
        sorted(exclude_symbols)
        if isinstance(exclude_symbols, frozenset)
        else exclude_symbols
    )
    normalized: list[str] = []
    seen: set[str] = set()
    for symbol in raw_symbols:
        parsed = str(symbol)
        if parsed in seen:
            continue
        normalized.append(parsed)
        seen.add(parsed)
    return tuple(normalized)


def filter_regime_trips(
    trips: list[dict],
    *,
    since: str | None,
    exclude_symbols: tuple[str, ...] | frozenset[str],
    min_entry_confidence: float | None = None,
) -> tuple[list[dict], dict]:
    """Apply shared date, symbol and entry-confidence attribution filters."""

    since_date = (
        _parse_iso_date(since, field="since") if since is not None else None
    )
    excluded_symbols = _normalize_excluded_symbols(exclude_symbols)
    excluded_symbol_set = set(excluded_symbols)
    kept: list[dict] = []
    n_excluded_trades = 0
    n_excluded_low_confidence = 0

    for trip in trips:
        excluded_regime = False
        if since_date is not None:
            exit_date = _parse_iso_date(
                str(trip.get("exit_ts")),
                field="exit_ts",
            )
            excluded_regime = exit_date < since_date
        if str(trip.get("symbol")) in excluded_symbol_set:
            excluded_regime = True
        if excluded_regime:
            n_excluded_trades += 1
            continue

        entry_confidence = trip.get("entry_confidence")
        if (
            min_entry_confidence is not None
            and entry_confidence is not None
            and entry_confidence < min_entry_confidence
        ):
            n_excluded_low_confidence += 1
            continue
        kept.append(trip)

    return kept, {
        "since": since,
        "excluded_symbols": list(excluded_symbols),
        "n_excluded_trades": n_excluded_trades,
        "min_entry_confidence": min_entry_confidence,
        "n_excluded_low_confidence": n_excluded_low_confidence,
    }


__all__ = [
    "aggregate_position_cycles",
    "compute_round_trips",
    "filter_regime_trips",
]
