"""Outcome resolution shared by FLAIR notes and global-rule citations.

Opening decisions are judged on their realised, net trade outcome. Every other
decision remains a short-horizon decision-quality judgement: an exit's quality
is about its timing, not the P&L created by the entry that preceded it.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from backtest.decision_quality import BAND


OPENING_INTENTS = frozenset({"OPEN_LONG", "OPEN_SHORT", "SCALE_IN", "FLIP"})


@dataclass
class _Lot:
    decision_id: str | None
    quantity: float
    price: float
    fx_rate: float
    entry_commission: float
    initial_quantity: float
    gross_usd: float = 0.0
    exit_commission: float = 0.0


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    result: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            result.append(row)
    return result


def realised_entry_outcomes(state_dir: str | Path) -> dict[str, float]:
    """Return net directional returns for fully closed entry lots by decision id.

    The performance ledger is append-only. FIFO matching makes partial exits
    explicit and keeps an entry pending until all of its quantity is closed.
    """

    rows = _rows(Path(state_dir) / "model_performance.jsonl")
    rows.sort(key=lambda row: (str(row.get("symbol") or ""), str(row.get("ts") or "")))
    lots_by_symbol: dict[str, deque[_Lot]] = {}
    resolved: dict[str, float] = {}

    for row in rows:
        symbol = str(row.get("symbol") or "")
        action = str(row.get("action") or "").upper()
        try:
            quantity = abs(float(row.get("quantity")))
            price = float(row.get("price"))
        except (TypeError, ValueError):
            continue
        if not symbol or action not in {"BUY", "SELL"} or quantity <= 0 or price <= 0:
            continue
        signed = quantity if action == "BUY" else -quantity
        fx_rate = _positive(row.get("fx_rate"), fallback=1.0)
        commission = _positive(row.get("commission"), fallback=0.0)
        decision_id = str(row.get("decision_id") or "") or None
        lots = lots_by_symbol.setdefault(symbol, deque())

        remaining = signed
        while lots and remaining and (lots[0].quantity > 0) != (remaining > 0):
            lot = lots[0]
            closed = min(abs(remaining), abs(lot.quantity))
            direction = 1.0 if lot.quantity > 0 else -1.0
            lot.gross_usd += (price - lot.price) * closed * direction * fx_rate
            lot.exit_commission += commission * (closed / abs(signed))
            lot.quantity -= direction * closed
            remaining += direction * closed
            if abs(lot.quantity) <= 1e-9:
                lots.popleft()
                if lot.decision_id:
                    deployed = lot.price * lot.initial_quantity * lot.fx_rate
                    net = lot.gross_usd - lot.entry_commission - lot.exit_commission
                    if deployed > 0:
                        resolved[lot.decision_id] = net / deployed
        if abs(remaining) > 1e-9:
            opening_quantity = abs(remaining)
            lots.append(
                _Lot(
                    decision_id=decision_id,
                    quantity=remaining,
                    price=price,
                    fx_rate=fx_rate,
                    entry_commission=commission * (opening_quantity / quantity),
                    initial_quantity=opening_quantity,
                )
            )
    return resolved


def _positive(value: object, *, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed >= 0 else fallback


def realised_verdict(net_return: float) -> tuple[str, float]:
    if net_return > BAND:
        return "WIN", 1.0
    if net_return < -BAND:
        return "LOSS", -1.0
    return "NEUTRAL", 0.0


def requires_realised_trade(row: dict) -> bool:
    return bool(row.get("executed")) and str(row.get("intent") or "") in OPENING_INTENTS


__all__ = [
    "OPENING_INTENTS",
    "realised_entry_outcomes",
    "realised_verdict",
    "requires_realised_trade",
]
