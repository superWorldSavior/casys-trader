from __future__ import annotations

from trader.domain.trade_plan import TradePlan


class MemoryTradePlanStore:
    def __init__(self, plans: list[TradePlan] | None = None) -> None:
        self._plans = list(plans or [])

    def open_plans(self) -> list[TradePlan]:
        return list(self._plans)

    def upsert(self, plan: TradePlan) -> None:
        self._plans = [item for item in self._plans if item.id != plan.id]
        self._plans.append(plan)

    def close(self, plan_id: str) -> None:
        self._plans = [item for item in self._plans if item.id != plan_id]

    def close_symbol(self, symbol: str) -> None:
        self._plans = [item for item in self._plans if item.symbol != symbol]

    def sync_symbol_quantity(self, symbol: str, remaining_quantity: float) -> None:
        if remaining_quantity <= 0:
            self.close_symbol(symbol)
            return
        symbol_plans = [plan for plan in self._plans if plan.symbol == symbol]
        total_remaining = sum(plan.remaining_quantity for plan in symbol_plans)
        if total_remaining <= 0:
            self.close_symbol(symbol)
            return
        ratio = remaining_quantity / total_remaining
        updated: list[TradePlan] = []
        for plan in self._plans:
            if plan.symbol != symbol:
                updated.append(plan)
                continue
            new_remaining = round(plan.remaining_quantity * ratio, 8)
            if new_remaining <= 0:
                continue
            take_profits = [
                tp
                if tp.name in plan.filled_take_profits
                else tp.model_copy(update={"quantity": round(tp.quantity * ratio, 8)})
                for tp in plan.take_profits
            ]
            updated.append(
                plan.model_copy(
                    update={
                        "remaining_quantity": new_remaining,
                        "take_profits": take_profits,
                    }
                )
            )
        self._plans = updated
