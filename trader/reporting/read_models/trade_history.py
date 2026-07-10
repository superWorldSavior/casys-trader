"""Canonical trade-history reconstruction and regime filtering read model."""

from __future__ import annotations

import json
import math
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
    ) -> None:
        if self.qty == 0.0:
            self.entry_ts = ts
            self._conf_sum = 0.0
            self._conf_qty = 0.0
            self.commission = 0.0
            self.entry_fx_rate = fx_rate
            self.entry_commission_currency = commission_currency
        new_qty = self.qty + added_qty
        self.avg_price = (
            self.avg_price * abs(self.qty) + price * abs(added_qty)
        ) / abs(new_qty)
        self.qty = new_qty
        self.commission += max(commission, 0.0)
        if confidence is not None:
            self._conf_sum += confidence * abs(added_qty)
            self._conf_qty += abs(added_qty)

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
    """Reconstruct closed trades from the append-only model-performance fills."""

    rows = _read_perf_rows(state_dir)
    rows.sort(key=lambda row: (str(row.get("symbol")), str(row.get("ts"))))
    legs: dict[str, _OpenLeg] = {}
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
        signed = qty if action == "BUY" else -qty
        leg = legs.setdefault(symbol, _OpenLeg())

        if abs(leg.qty) <= _FLAT_EPS or (leg.qty > 0) == (signed > 0):
            leg.open_or_add(
                added_qty=signed,
                price=price,
                ts=ts,
                confidence=confidence,
                commission=row_commission,
                fx_rate=row_fx_rate,
                commission_currency=row_commission_currency,
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
            }
        )

        remaining = qty - closing_qty
        leg.qty += signed
        if abs(leg.qty) <= _FLAT_EPS:
            legs[symbol] = _OpenLeg()
        elif remaining > _FLAT_EPS:
            fresh = _OpenLeg()
            fresh.open_or_add(
                added_qty=entry_sign * -remaining,
                price=price,
                ts=ts,
                confidence=confidence,
                commission=max(row_commission - exit_commission, 0.0),
                fx_rate=row_fx_rate,
                commission_currency=row_commission_currency,
            )
            legs[symbol] = fresh
        else:
            leg.commission = max(leg.commission - entry_commission, 0.0)
            leg.scale_entry_weight(abs(leg.qty) / old_abs)

    return trips


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


__all__ = ["compute_round_trips", "filter_regime_trips"]
