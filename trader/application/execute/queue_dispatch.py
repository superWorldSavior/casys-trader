"""Dispatch and collect execute-order tasks through the durable queue."""

from __future__ import annotations

import json
import logging
import time as _time
from dataclasses import dataclass
from typing import Callable, Literal

from trader.domain.contracts import Fill

log = logging.getLogger("trader.application.execute.queue_dispatch")

_POLL_SLEEP_S: float = 0.05

ExecuteQueueTerminal = Literal[
    "done",
    "dead",
    "not_found",
    "timeout",
    "enqueue_failed",
]


@dataclass(frozen=True)
class ExecuteQueueOutcome:
    task_id: int | None
    terminal: ExecuteQueueTerminal
    fill: Fill | None = None
    reason: str | None = None
    raw_result: str | None = None
    late_execution_risk: bool = False
    abandoned: bool = False


def dispatch_execute_order_via_queue(
    *,
    ledger,
    symbol: str,
    side: str,
    quantity: float,
    rationale: str,
    price: float,
    ts: str,
    fx_rate: float,
    dry_run: bool,
    plan_to_upsert: dict | None,
    symbol_to_close: str | None,
    symbol_to_sync_quantity: str | None = None,
    cycle_id: str,
    intent: str,
    budget_s: float,
    now_fn: Callable[[], float] = _time.time,
    sleep_fn: Callable[[float], None] = _time.sleep,
) -> ExecuteQueueOutcome:
    """Enqueue one ``execute_order`` task and collect its terminal outcome.

    This mirrors the existing daemon fail-closed semantics:
    dead/not_found/timeout/enqueue failure/no-fill never become executed=True.
    ``dry_run`` tasks may complete with no fill.
    """
    payload = json.dumps(
        {
            "order": {
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "rationale": rationale,
            },
            "price": price,
            "ts": ts,
            "fx_rate": fx_rate,
            "dry_run": dry_run,
            "plan_to_upsert": plan_to_upsert,
            "symbol_to_close": symbol_to_close,
            "symbol_to_sync_quantity": symbol_to_sync_quantity,
        }
    )
    now_ms = int(now_fn() * 1000)
    dedup_key = f"exec:{cycle_id}:{symbol}:{intent}"
    task_id = ledger.enqueue(
        kind="execute_order",
        priority=0,
        scheduled_at_ms=now_ms,
        now_ms=now_ms,
        resource="portfolio",
        partition_key="portfolio",
        dedup_key=dedup_key,
        payload=payload,
    )
    if task_id is None:
        log.error("[queue_execute] enqueue None sym=%s — skip", symbol)
        return ExecuteQueueOutcome(
            task_id=None,
            terminal="enqueue_failed",
            reason="queue_execute_enqueue_failed",
        )

    deadline = now_fn() + budget_s
    terminal: ExecuteQueueTerminal = "timeout"
    raw_result: str | None = None
    fill: Fill | None = None

    while now_fn() < deadline:
        task = ledger.get(task_id)
        if task is None:
            log.warning("[queue_execute] task introuvable id=%s sym=%s", task_id, symbol)
            terminal = "not_found"
            break

        status = task.get("status")
        if status == "done":
            raw_result, fill = _read_fill_result(task, symbol=symbol)
            terminal = "done"
            break
        if status == "dead":
            log.warning("[queue_execute] task dead sym=%s id=%s", symbol, task_id)
            terminal = "dead"
            break

        remaining = deadline - now_fn()
        if remaining > 0:
            sleep_fn(min(_POLL_SLEEP_S, remaining))
    else:
        log.warning("[queue_execute] budget épuisé sym=%s budget_s=%s", symbol, budget_s)

    abandoned = False
    if terminal == "timeout":
        abandon = getattr(ledger, "abandon", None)
        timeout_reason = _fail_closed_reason(terminal=terminal, fill=fill, dry_run=dry_run)
        if callable(abandon):
            try:
                abandoned = bool(
                    abandon(
                        task_id=task_id,
                        now_ms=int(now_fn() * 1000),
                        error=timeout_reason or "queue_execute_timeout",
                    )
                )
            except Exception as exc:  # noqa: BLE001 — fail-closed, mais signaler le risque résiduel
                log.error(
                    "[queue_execute] abandon échoué sym=%s id=%s: %s",
                    symbol,
                    task_id,
                    exc,
                )
        if abandoned:
            log.warning(
                "[queue_execute] FAIL-CLOSED sym=%s — budget expiré, tâche id=%s abandonnée "
                "avant soumission tardive",
                symbol,
                task_id,
            )
        else:
            try:
                refreshed = ledger.get(task_id)
            except Exception as exc:  # noqa: BLE001 — fail-closed si la relecture échoue
                refreshed = None
                log.error("[queue_execute] reload après abandon échoué sym=%s id=%s: %s", symbol, task_id, exc)
            if refreshed is not None and refreshed.get("status") == "done":
                raw_result, fill = _read_fill_result(refreshed, symbol=symbol)
                terminal = "done"
                log.warning(
                    "[queue_execute] timeout race résolue sym=%s id=%s — tâche devenue done "
                    "entre le dernier poll et abandon",
                    symbol,
                    task_id,
                )
            else:
                status = refreshed.get("status") if refreshed is not None else "missing"
                log.warning(
                    "[queue_execute] FAIL-CLOSED sym=%s — budget expiré sans état terminal "
                    "(tâche id=%s status=%s, ordre peut encore s'exécuter plus tard — "
                    "risque résiduel d'exécution tardive non géré dans ce cycle)",
                    symbol,
                    task_id,
                    status,
                )
    reason = _fail_closed_reason(terminal=terminal, fill=fill, dry_run=dry_run)
    late_execution_risk = terminal == "timeout" and not abandoned
    if reason == "queue_execute_no_fill":
        log.error(
            "[queue_execute] FAIL-CLOSED sym=%s id=%s — task done sans fill décodable "
            "(result=%r) — ordre non confirmé, executed=False",
            symbol,
            task_id,
            raw_result,
        )

    return ExecuteQueueOutcome(
        task_id=task_id,
        terminal=terminal,
        fill=fill,
        reason=reason,
        raw_result=raw_result,
        late_execution_risk=late_execution_risk,
        abandoned=abandoned,
    )


def _read_fill_result(task: dict, *, symbol: str) -> tuple[str | None, Fill | None]:
    raw_value = task.get("result")
    raw_result = raw_value if isinstance(raw_value, str) else None
    fill = None
    if raw_result:
        try:
            fill = Fill(**json.loads(raw_result))
        except Exception as exc:  # noqa: BLE001
            log.warning("[queue_execute] désérialisation fill sym=%s: %s", symbol, exc)
    return raw_result, fill


def _fail_closed_reason(*, terminal: ExecuteQueueTerminal, fill: Fill | None, dry_run: bool) -> str | None:
    if terminal == "dead":
        return "queue_execute_dead"
    if terminal == "not_found":
        return "queue_execute_not_found"
    if terminal == "timeout":
        return "queue_execute_timeout"
    if terminal == "enqueue_failed":
        return "queue_execute_enqueue_failed"
    if terminal == "done" and fill is None and not dry_run:
        return "queue_execute_no_fill"
    return None
