"""Canonical strategy activity funnel from existing ledgers, events and fills.

Watch metrics form a cohort: watches accepted by LLM decisions in the requested
decision window are followed through ``until``. Direct BUY/SELL activity and
paper fills are sibling counts, not an implied causal conversion from watches.
The paper broker persists submission and fill atomically, so a correlated fill
is the durable evidence for both stages.

Infra HOLD rows are counted separately and never mixed with LLM reviews.
"""

from __future__ import annotations

import gzip
import json
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trader.application.record.decision_entries import has_exploitable_llm_rationale
from trader.infrastructure.files.ledger_rotation import read_rows_with_archive

_TRADE_ACTIONS = frozenset({"BUY", "SELL"})
_WATCH_CREATED = "indicator_watch_created"
_WATCH_ARMED = "armed_plan_created"
_INDICATOR_WATCH_TRIGGERED = "indicator_watch_triggered"
_EXIT_WATCH_TRIGGERED = "exit_watch_triggered"
_WATCH_EXPIRED = frozenset({"indicator_watch_expired", "armed_plan_expired"})
_WATCH_CANCELLED = frozenset({"watch_cancelled_by_agent", "armed_plan_cancelled"})
_WATCH_SUPERSEDED = "indicator_watch_superseded"


class StrategyFunnelSourceError(RuntimeError):
    """A canonical funnel input exists but cannot be read safely."""


def _parse_ts(raw: object) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _in_window(raw: object, *, since: datetime | None, until: datetime | None) -> bool:
    parsed = _parse_ts(raw)
    if parsed is None:
        return False
    if since is not None and parsed < since:
        return False
    if until is not None and parsed > until:
        return False
    return True


def _is_llm_review(row: Mapping[str, Any]) -> bool:
    if row.get("model_called") is not True:
        return False
    return (
        str(row.get("decision_source") or "").strip().lower() == "llm"
        and has_exploitable_llm_rationale(
            rationale=row.get("rationale"),
            llm_error=row.get("llm_error"),
        )
    )


def _is_infra_hold(row: Mapping[str, Any]) -> bool:
    source = str(row.get("decision_source") or "").strip().lower()
    if source == "infra":
        return True
    if row.get("model_called") is False and str(row.get("action") or "").upper() == "HOLD":
        reason = str(row.get("reason") or "")
        return reason in {"quiet_gate", "stale_market_data", "no_decision_in_batch"}
    return False


def _accepted_indicator_watch(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the watch durably accepted by one canonical decision row.

    The decision ledger stores the normalized watch inside its nested
    ``decision`` snapshot and the mutation receipt inside ``runtime``.  Accept
    the top-level form only for legacy/imported rows that do not expose an
    explicit negative receipt.
    """
    nested_decision = row.get("decision")
    nested_watch = (
        nested_decision.get("indicator_watch")
        if isinstance(nested_decision, Mapping)
        else None
    )
    watch = nested_watch if isinstance(nested_watch, Mapping) else row.get(
        "indicator_watch"
    )
    if not isinstance(watch, Mapping) or not watch.get("id"):
        return None
    runtime = row.get("runtime")
    receipt = (
        runtime.get("indicator_watch_created")
        if isinstance(runtime, Mapping)
        else row.get("indicator_watch_created")
    )
    if receipt is False:
        return None
    return watch


def _read_ledger(path: Path, *, archive_dir: Path) -> list[dict[str, Any]]:
    """Read live and archived rows, failing closed on partial/corrupt input."""
    archives = (
        sorted(archive_dir.glob(f"{path.stem}-????-??.jsonl.gz"))
        if archive_dir.is_dir()
        else []
    )
    has_archive = bool(archives)
    if not path.is_file() and not has_archive:
        raise StrategyFunnelSourceError(f"missing canonical ledger: {path}")
    try:
        for archive_path in archives:
            with gzip.open(archive_path, "rt", encoding="utf-8") as handle:
                _validate_jsonl_rows(handle, source=archive_path)
        if path.is_file():
            with path.open("r", encoding="utf-8") as handle:
                _validate_jsonl_rows(handle, source=path)
        return list(read_rows_with_archive(path, archive_dir))
    except StrategyFunnelSourceError:
        raise
    except (OSError, UnicodeDecodeError, EOFError, zlib.error) as exc:
        raise StrategyFunnelSourceError(
            f"unreadable canonical ledger: {path}: {type(exc).__name__}"
        ) from exc


def _validate_jsonl_rows(handle: Iterable[str], *, source: Path) -> None:
    """Reject malformed/non-object rows before the tolerant archive reader runs."""
    for line_number, line in enumerate(handle, start=1):
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StrategyFunnelSourceError(
                f"invalid canonical ledger row: {source}:{line_number}"
            ) from exc
        if not isinstance(row, dict):
            raise StrategyFunnelSourceError(
                f"non-object canonical ledger row: {source}:{line_number}"
            )


def _load_fills(state_dir: Path) -> list[Mapping[str, Any]]:
    db_path = state_dir / "casys.db"
    if db_path.is_file():
        try:
            from trader.infrastructure.state_db.broker_store import SqliteBroker
            from trader.infrastructure.state_db.connection import open_state_db

            return list(SqliteBroker(open_state_db(db_path)).fills())
        except Exception as exc:
            raise StrategyFunnelSourceError(
                f"unreadable canonical broker database: {db_path}: {type(exc).__name__}"
            ) from exc
    broker_json = state_dir / "broker.json"
    if not broker_json.is_file():
        raise StrategyFunnelSourceError(
            f"missing broker fill source: {broker_json}"
        )
    try:
        raw = json.loads(broker_json.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise StrategyFunnelSourceError(
            f"unreadable broker fill source: {broker_json}"
        )
    fills = raw.get("fills") if isinstance(raw, dict) else None
    if not isinstance(fills, list):
        raise StrategyFunnelSourceError(
            f"invalid broker fill source: {broker_json}: fills_not_list"
        )
    if not all(isinstance(row, dict) for row in fills):
        raise StrategyFunnelSourceError(
            f"invalid broker fill source: {broker_json}: fill_not_object"
        )
    return fills


def _load_process_events(
    state_dir: Path,
) -> list[Mapping[str, Any]] | None:
    """Load causal watch→decision joins when the canonical SQLite trace exists."""
    db_path = state_dir / "casys.db"
    if not db_path.is_file():
        return None
    try:
        from trader.infrastructure.state_db.connection import open_state_db

        rows = open_state_db(db_path).query_all(
            "SELECT process_instance_id, attempt_id, work_object_key, ts, "
            "caused_by_json, effect_refs_json "
            "FROM process_events ORDER BY seq"
        )
        events: list[Mapping[str, Any]] = []
        for row in rows:
            caused_by = json.loads(row["caused_by_json"])
            effect_refs = json.loads(row["effect_refs_json"])
            if not isinstance(caused_by, list) or not isinstance(effect_refs, list):
                raise ValueError("process event JSON fields must be lists")
            if not all(isinstance(item, dict) for item in [*caused_by, *effect_refs]):
                raise ValueError("process event causal rows must be objects")
            events.append(
                {
                    "process_instance_id": row["process_instance_id"],
                    "attempt_id": row["attempt_id"],
                    "work_object_key": row["work_object_key"],
                    "ts": row["ts"],
                    "caused_by": caused_by,
                    "effect_refs": effect_refs,
                }
            )
        return events
    except Exception as exc:
        raise StrategyFunnelSourceError(
            f"unreadable canonical process trace: {db_path}: {type(exc).__name__}"
        ) from exc


@dataclass(frozen=True)
class StrategyFunnel:
    llm_reviews: int
    infra_holds: int
    watches_created: int
    watches_armed: int
    watches_triggered: int
    watches_expired_untriggered: int
    watches_cancelled: int
    watches_superseded: int
    watches_pending_or_unresolved: int
    external_indicator_triggers: int
    exit_watch_triggers: int
    unjoinable_watch_creations: int
    buy_sell_proposed: int
    orders_submitted: int
    fills: int
    non_decision_fills: int
    causal_linkage_available: bool
    causal_coverage_start: str | None
    trigger_process_attempts: int | None
    trigger_decisions: int | None
    trigger_holds: int | None
    trigger_trade_decisions: int | None
    trigger_orders_submitted: int | None
    trigger_fills: int | None
    triggered_watches_with_fill: int | None
    trigger_without_process: int | None
    trigger_process_without_decision: int | None
    triggered_cancelled: int | None
    expiry_review_decisions: int | None
    expiry_review_fills: int | None
    direct_trade_decisions: int | None
    direct_fills: int | None
    since: str | None = None
    until: str | None = None

    def as_dict(self) -> dict[str, int | bool | str | None]:
        return {
            "llm_reviews": self.llm_reviews,
            "infra_holds": self.infra_holds,
            "watches_created": self.watches_created,
            "watches_armed": self.watches_armed,
            "watches_triggered": self.watches_triggered,
            "watches_expired_untriggered": self.watches_expired_untriggered,
            "watches_cancelled": self.watches_cancelled,
            "watches_superseded": self.watches_superseded,
            "watches_pending_or_unresolved": self.watches_pending_or_unresolved,
            "external_indicator_triggers": self.external_indicator_triggers,
            "exit_watch_triggers": self.exit_watch_triggers,
            "unjoinable_watch_creations": self.unjoinable_watch_creations,
            "buy_sell_proposed": self.buy_sell_proposed,
            "orders_submitted": self.orders_submitted,
            "fills": self.fills,
            "non_decision_fills": self.non_decision_fills,
            "causal_linkage_available": self.causal_linkage_available,
            "causal_coverage_start": self.causal_coverage_start,
            "trigger_process_attempts": self.trigger_process_attempts,
            "trigger_decisions": self.trigger_decisions,
            "trigger_holds": self.trigger_holds,
            "trigger_trade_decisions": self.trigger_trade_decisions,
            "trigger_orders_submitted": self.trigger_orders_submitted,
            "trigger_fills": self.trigger_fills,
            "triggered_watches_with_fill": self.triggered_watches_with_fill,
            "trigger_without_process": self.trigger_without_process,
            "trigger_process_without_decision": self.trigger_process_without_decision,
            "triggered_cancelled": self.triggered_cancelled,
            "expiry_review_decisions": self.expiry_review_decisions,
            "expiry_review_fills": self.expiry_review_fills,
            "direct_trade_decisions": self.direct_trade_decisions,
            "direct_fills": self.direct_fills,
        }


def _compute_causal_metrics(
    *,
    process_events: Iterable[Mapping[str, Any]] | None,
    cohort_watch_created_at: Mapping[str, datetime],
    triggered_watch_ids: set[str],
    cancelled_watch_ids: set[str],
    decision_actions: Mapping[str, str],
    window_trade_decision_at: Mapping[str, datetime],
    fills: list[Mapping[str, Any]],
    until: datetime | None,
) -> dict[str, bool | int | str | None]:
    """Join watch causes to decisions and fills on the governed attempt key."""
    unavailable: dict[str, bool | int | str | None] = {
        "causal_linkage_available": False,
        "causal_coverage_start": None,
        "trigger_process_attempts": None,
        "trigger_decisions": None,
        "trigger_holds": None,
        "trigger_trade_decisions": None,
        "trigger_orders_submitted": None,
        "trigger_fills": None,
        "triggered_watches_with_fill": None,
        "trigger_without_process": None,
        "trigger_process_without_decision": None,
        "triggered_cancelled": None,
        "expiry_review_decisions": None,
        "expiry_review_fills": None,
        "direct_trade_decisions": None,
        "direct_fills": None,
    }
    if process_events is None:
        return unavailable

    eligible_process_events: list[tuple[datetime, Mapping[str, Any]]] = []
    for row in process_events:
        event_ts = _parse_ts(row.get("ts"))
        if event_ts is None or (until is not None and event_ts > until):
            continue
        process_instance_id = str(row.get("process_instance_id") or "").strip()
        attempt_id = str(row.get("attempt_id") or "").strip()
        if not process_instance_id or not attempt_id:
            continue
        eligible_process_events.append((event_ts, row))
    if not eligible_process_events:
        return unavailable

    causal_coverage_start = min(
        event_ts for event_ts, _row in eligible_process_events
    )
    unavailable["causal_coverage_start"] = causal_coverage_start.isoformat()
    relevant_activity = [
        *cohort_watch_created_at.values(),
        *window_trade_decision_at.values(),
    ]
    if relevant_activity and causal_coverage_start > min(relevant_activity):
        return unavailable

    attempts: dict[tuple[str, str], dict[str, set[str]]] = {}
    for event_ts, row in eligible_process_events:
        process_instance_id = str(row.get("process_instance_id") or "").strip()
        attempt_id = str(row.get("attempt_id") or "").strip()
        key = (process_instance_id, attempt_id)
        attempt = attempts.setdefault(
            key,
            {
                "trigger_watches": set(),
                "expiry_watches": set(),
                "decision_ids": set(),
                "symbols": set(),
            },
        )
        symbol = str(row.get("work_object_key") or "").strip()
        if symbol:
            attempt["symbols"].add(symbol)
        causes = row.get("caused_by")
        if isinstance(causes, Iterable) and not isinstance(causes, (str, bytes)):
            for cause in causes:
                if not isinstance(cause, Mapping):
                    continue
                watch_id = str(cause.get("watch_id") or "").strip()
                created_at = cohort_watch_created_at.get(watch_id)
                if created_at is None or event_ts < created_at:
                    continue
                cause_type = str(cause.get("type") or "")
                if cause_type == "indicator_trigger":
                    attempt["trigger_watches"].add(watch_id)
                elif cause_type == "wake_reason" and str(
                    cause.get("reason") or ""
                ) in {"watch_expired", "armed_plan_expired"}:
                    attempt["expiry_watches"].add(watch_id)
        refs = row.get("effect_refs")
        if isinstance(refs, Iterable) and not isinstance(refs, (str, bytes)):
            for ref in refs:
                if not isinstance(ref, Mapping) or ref.get("type") != "decision":
                    continue
                if (
                    str(ref.get("process_instance_id") or "")
                    != process_instance_id
                    or str(ref.get("attempt_id") or "") != attempt_id
                ):
                    continue
                decision_id = str(ref.get("decision_id") or "").strip()
                if decision_id:
                    attempt["decision_ids"].add(decision_id)

    trigger_attempts = {
        key: value for key, value in attempts.items() if value["trigger_watches"]
    }
    expiry_attempts = {
        key: value for key, value in attempts.items() if value["expiry_watches"]
    }

    def resolved_decisions(attempt_rows: Mapping[tuple[str, str], dict[str, set[str]]]) -> set[str]:
        return {
            decision_id
            for attempt in attempt_rows.values()
            for decision_id in attempt["decision_ids"]
            if decision_id in decision_actions
        }

    def matching_fill_indexes(
        attempt_rows: Mapping[tuple[str, str], dict[str, set[str]]],
        eligible_decisions: set[str],
    ) -> tuple[set[int], set[str], set[str]]:
        indexes: set[int] = set()
        submitted: set[str] = set()
        watches_with_fill: set[str] = set()
        for (process_instance_id, attempt_id), attempt in attempt_rows.items():
            attempt_decisions = attempt["decision_ids"] & eligible_decisions
            if not attempt_decisions:
                continue
            attempt_fill_indexes: set[int] = set()
            for index, fill in enumerate(fills):
                decision_id = str(fill.get("decision_id") or "").strip()
                if decision_id not in attempt_decisions:
                    continue
                if (
                    str(fill.get("process_instance_id") or "")
                    != process_instance_id
                    or str(fill.get("attempt_id") or "") != attempt_id
                ):
                    continue
                fill_symbol = str(fill.get("symbol") or "").strip()
                if attempt["symbols"] and fill_symbol not in attempt["symbols"]:
                    continue
                attempt_fill_indexes.add(index)
                submitted.add(decision_id)
            if attempt_fill_indexes:
                indexes.update(attempt_fill_indexes)
                watches_with_fill.update(attempt["trigger_watches"])
        return indexes, submitted, watches_with_fill

    trigger_decision_ids = resolved_decisions(trigger_attempts)
    trigger_trade_ids = {
        decision_id
        for decision_id in trigger_decision_ids
        if decision_actions[decision_id] in _TRADE_ACTIONS
    }
    trigger_hold_ids = {
        decision_id
        for decision_id in trigger_decision_ids
        if decision_actions[decision_id] == "HOLD"
    }
    trigger_fill_indexes, trigger_submitted_ids, trigger_watches_with_fill = (
        matching_fill_indexes(trigger_attempts, trigger_trade_ids)
    )
    trigger_watches_with_process = {
        watch_id
        for attempt in trigger_attempts.values()
        for watch_id in attempt["trigger_watches"]
    }
    trigger_attempts_without_decision = sum(
        1
        for attempt in trigger_attempts.values()
        if not (attempt["decision_ids"] & set(decision_actions))
    )

    expiry_decision_ids = resolved_decisions(expiry_attempts)
    expiry_trade_ids = {
        decision_id
        for decision_id in expiry_decision_ids
        if decision_actions[decision_id] in _TRADE_ACTIONS
    }
    expiry_fill_indexes, _, _ = matching_fill_indexes(
        expiry_attempts,
        expiry_trade_ids,
    )

    direct_trade_ids = set(window_trade_decision_at) - trigger_trade_ids - expiry_trade_ids
    direct_fill_count = sum(
        1
        for fill in fills
        if str(fill.get("decision_id") or "").strip() in direct_trade_ids
    )
    return {
        "causal_linkage_available": True,
        "causal_coverage_start": causal_coverage_start.isoformat(),
        "trigger_process_attempts": len(trigger_attempts),
        "trigger_decisions": len(trigger_decision_ids),
        "trigger_holds": len(trigger_hold_ids),
        "trigger_trade_decisions": len(trigger_trade_ids),
        "trigger_orders_submitted": len(trigger_submitted_ids),
        "trigger_fills": len(trigger_fill_indexes),
        "triggered_watches_with_fill": len(trigger_watches_with_fill),
        "trigger_without_process": len(
            triggered_watch_ids - trigger_watches_with_process
        ),
        "trigger_process_without_decision": trigger_attempts_without_decision,
        "triggered_cancelled": len(triggered_watch_ids & cancelled_watch_ids),
        "expiry_review_decisions": len(expiry_decision_ids),
        "expiry_review_fills": len(expiry_fill_indexes),
        "direct_trade_decisions": len(direct_trade_ids),
        "direct_fills": direct_fill_count,
    }


def _watch_symbol(row: Mapping[str, Any], watch: Mapping[str, Any]) -> str:
    symbol = str(watch.get("symbol") or row.get("symbol") or "").strip()
    if symbol:
        return symbol
    watch_id = str(watch.get("id") or "")
    if ":" in watch_id:
        return watch_id.rsplit(":", 1)[0]
    return ""


def _infer_superseded_watch_ids(
    *,
    creations: Iterable[tuple[datetime, int, str, str]],
    lifecycle_events: Iterable[tuple[datetime, int, str, str]],
) -> set[str]:
    """Infer supersessions when an old event append is missing.

    Accepted decision rows are the creation authority. Terminal lifecycle
    events remove a watch before a later creation is considered, avoiding a
    false supersession when a watch already triggered, expired or was cancelled.
    """
    creation_rows = list(creations)
    symbol_by_watch_id = {
        watch_id: symbol
        for _created_at, _index, symbol, watch_id in creation_rows
        if symbol
    }
    timeline: list[tuple[datetime, int, int, str, str, str]] = []
    for created_at, index, symbol, watch_id in creation_rows:
        if symbol:
            timeline.append((created_at, 1, index, "created", watch_id, symbol))
    for event_ts, index, kind, watch_id in lifecycle_events:
        timeline.append((event_ts, 0, index, kind, watch_id, ""))

    active_by_symbol: dict[str, str] = {}
    inferred: set[str] = set()
    for _ts, _priority, _index, kind, watch_id, symbol in sorted(timeline):
        if kind == "created":
            previous_watch_id = active_by_symbol.get(symbol)
            if previous_watch_id and previous_watch_id != watch_id:
                inferred.add(previous_watch_id)
            active_by_symbol[symbol] = watch_id
            continue
        watch_symbol = symbol_by_watch_id.get(watch_id)
        if watch_symbol and active_by_symbol.get(watch_symbol) == watch_id:
            active_by_symbol.pop(watch_symbol, None)
    return inferred


def compute_strategy_funnel(
    *,
    decisions: Iterable[Mapping[str, Any]],
    events: Iterable[Mapping[str, Any]],
    fills: Iterable[Mapping[str, Any]],
    process_events: Iterable[Mapping[str, Any]] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> StrategyFunnel:
    """Project the operator funnel from already-loaded canonical records."""
    decision_rows = list(decisions)
    fill_rows = list(fills)
    llm_review_ids: set[str] = set()
    infra_hold_ids: set[str] = set()
    proposed_ids: set[str] = set()
    window_trade_decision_at: dict[str, datetime] = {}
    decision_actions: dict[str, str] = {}
    cohort_created_watch_ids: set[str] = set()
    cohort_armed_watch_ids: set[str] = set()
    cohort_watch_created_at: dict[str, datetime] = {}
    nonarmed_watch_creations: list[tuple[datetime, int, str, str]] = []
    for index, row in enumerate(decision_rows):
        raw_ts = row.get("cycle_ts") or row.get("ts")
        decision_ts = _parse_ts(raw_ts)
        if decision_ts is None or (until is not None and decision_ts > until):
            continue
        persisted_decision_id = str(row.get("decision_id") or "").strip()
        decision_id = persisted_decision_id or f"row:{index}"
        action = str(row.get("action") or "").upper()
        if persisted_decision_id:
            decision_actions[persisted_decision_id] = action
        if not _in_window(raw_ts, since=since, until=until):
            continue
        if _is_llm_review(row):
            llm_review_ids.add(decision_id)
            if action in _TRADE_ACTIONS:
                proposed_ids.add(decision_id)
            watch = _accepted_indicator_watch(row)
            if watch is not None:
                watch_id = str(watch["id"])
                if (
                    str(watch.get("on_trigger") or "") == "EXECUTE_ORDER"
                    and isinstance(watch.get("order"), Mapping)
                ):
                    cohort_armed_watch_ids.add(watch_id)
                else:
                    cohort_created_watch_ids.add(watch_id)
                    nonarmed_watch_creations.append(
                        (
                            decision_ts,
                            index,
                            _watch_symbol(row, watch),
                            watch_id,
                        )
                    )
                cohort_watch_created_at[watch_id] = decision_ts
        elif _is_infra_hold(row):
            infra_hold_ids.add(decision_id)
        if action in _TRADE_ACTIONS and persisted_decision_id:
            window_trade_decision_at[persisted_decision_id] = decision_ts

    cohort_watch_ids = cohort_created_watch_ids | cohort_armed_watch_ids
    event_created_watch_ids: set[str] = set()
    cohort_triggered_watch_ids: set[str] = set()
    external_triggered_watch_ids: set[str] = set()
    expired_watch_ids: set[str] = set()
    cancelled_watch_ids: set[str] = set()
    explicit_superseded_watch_ids: set[str] = set()
    watch_lifecycle_events: list[tuple[datetime, int, str, str]] = []
    exit_trigger_keys: set[str] = set()
    for index, event in enumerate(events):
        event_ts = _parse_ts(event.get("ts"))
        if event_ts is None or (until is not None and event_ts > until):
            continue
        kind = str(event.get("event") or "")
        watch_id = str(
            event.get("watch_id") or event.get("plan_id") or f"event:{index}"
        )
        in_activity_window = since is None or event_ts >= since
        if kind in {_WATCH_CREATED, _WATCH_ARMED} and in_activity_window:
            event_created_watch_ids.add(watch_id)
        elif kind == _INDICATOR_WATCH_TRIGGERED:
            if watch_id in cohort_created_watch_ids:
                watch_lifecycle_events.append((event_ts, index, kind, watch_id))
            if watch_id in cohort_watch_ids:
                cohort_triggered_watch_ids.add(watch_id)
            elif in_activity_window:
                external_triggered_watch_ids.add(watch_id)
        elif kind == _EXIT_WATCH_TRIGGERED and in_activity_window:
            exit_trigger_keys.add(watch_id)
        elif kind in _WATCH_EXPIRED and watch_id in cohort_watch_ids:
            expired_watch_ids.add(watch_id)
            if watch_id in cohort_created_watch_ids:
                watch_lifecycle_events.append((event_ts, index, kind, watch_id))
        elif kind in _WATCH_CANCELLED and watch_id in cohort_watch_ids:
            cancelled_watch_ids.add(watch_id)
            if watch_id in cohort_created_watch_ids:
                watch_lifecycle_events.append((event_ts, index, kind, watch_id))
        elif kind == _WATCH_SUPERSEDED and watch_id in cohort_created_watch_ids:
            explicit_superseded_watch_ids.add(watch_id)
            watch_lifecycle_events.append((event_ts, index, kind, watch_id))

    inferred_superseded_watch_ids = _infer_superseded_watch_ids(
        creations=nonarmed_watch_creations,
        lifecycle_events=watch_lifecycle_events,
    )
    superseded_watch_ids = (
        explicit_superseded_watch_ids | inferred_superseded_watch_ids
    )

    expired_untriggered = expired_watch_ids - cohort_triggered_watch_ids
    cancelled_untriggered = (
        cancelled_watch_ids - cohort_triggered_watch_ids - expired_untriggered
    )
    superseded_untriggered = (
        superseded_watch_ids
        - cohort_triggered_watch_ids
        - expired_untriggered
        - cancelled_untriggered
    )
    pending_or_unresolved = (
        cohort_watch_ids
        - cohort_triggered_watch_ids
        - expired_untriggered
        - cancelled_untriggered
        - superseded_untriggered
    )

    correlated_fills = 0
    non_decision_fills = 0
    submitted_decision_ids: set[str] = set()
    causal_fill_rows: list[Mapping[str, Any]] = []
    for fill in fill_rows:
        if isinstance(fill, Mapping):
            ts = fill.get("ts")
            decision_id = fill.get("decision_id")
        else:
            ts = getattr(fill, "ts", None)
            decision_id = getattr(fill, "decision_id", None)
        decision_id_text = str(decision_id or "").strip()
        if _in_window(ts, since=None, until=until) and isinstance(fill, Mapping):
            causal_fill_rows.append(fill)
        if not _in_window(ts, since=since, until=until):
            continue
        if decision_id_text and decision_id_text in decision_actions:
            correlated_fills += 1
            submitted_decision_ids.add(decision_id_text)
        else:
            non_decision_fills += 1

    causal = _compute_causal_metrics(
        process_events=process_events,
        cohort_watch_created_at=cohort_watch_created_at,
        triggered_watch_ids=cohort_triggered_watch_ids,
        cancelled_watch_ids=cancelled_watch_ids,
        decision_actions=decision_actions,
        window_trade_decision_at=window_trade_decision_at,
        fills=causal_fill_rows,
        until=until,
    )

    return StrategyFunnel(
        llm_reviews=len(llm_review_ids),
        infra_holds=len(infra_hold_ids),
        watches_created=len(cohort_created_watch_ids),
        watches_armed=len(cohort_armed_watch_ids),
        watches_triggered=len(cohort_triggered_watch_ids),
        watches_expired_untriggered=len(expired_untriggered),
        watches_cancelled=len(cancelled_untriggered),
        watches_superseded=len(superseded_untriggered),
        watches_pending_or_unresolved=len(pending_or_unresolved),
        external_indicator_triggers=len(external_triggered_watch_ids),
        exit_watch_triggers=len(exit_trigger_keys),
        unjoinable_watch_creations=len(event_created_watch_ids - cohort_watch_ids),
        buy_sell_proposed=len(proposed_ids),
        # The paper broker persists submit and fill atomically.  A correlated
        # fill is therefore the only durable proof of broker submission; do not
        # infer submission from decision.executed or queue admission.
        orders_submitted=len(submitted_decision_ids),
        fills=correlated_fills,
        non_decision_fills=non_decision_fills,
        **causal,
        since=None if since is None else since.isoformat(),
        until=None if until is None else until.isoformat(),
    )


def load_strategy_funnel(
    state_dir: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    hours: float | None = None,
    now: datetime | None = None,
) -> StrategyFunnel:
    """Read the funnel from ``decisions.jsonl``, ``events.jsonl`` and broker fills."""
    clock = now or datetime.now(timezone.utc)
    if until is None:
        until = clock
    if since is None and hours is not None:
        since = clock - timedelta(hours=float(hours))
    return compute_strategy_funnel(
        decisions=_read_ledger(
            state_dir / "decisions.jsonl",
            archive_dir=state_dir / "archive",
        ),
        events=_read_ledger(
            state_dir / "events.jsonl",
            archive_dir=state_dir / "archive",
        ),
        fills=_load_fills(state_dir),
        process_events=_load_process_events(state_dir),
        since=since,
        until=until,
    )


def render_strategy_funnel(funnel: StrategyFunnel) -> str:
    window = ""
    if funnel.since or funnel.until:
        window = f" ({funnel.since or '…'} → {funnel.until or '…'})"
    lines = [
        f"strategy activity funnel{window}",
        "  watch counts are cohort-aware; HOLD after trigger is not a market miss",
        f"  llm_reviews        : {funnel.llm_reviews}",
        f"  infra_holds        : {funnel.infra_holds}",
        f"  watches_created    : {funnel.watches_created}",
        f"  watches_armed      : {funnel.watches_armed}",
        f"  watches_triggered  : {funnel.watches_triggered}",
        f"  watches_expired    : {funnel.watches_expired_untriggered}",
        f"  watches_cancelled  : {funnel.watches_cancelled}",
        f"  watches_superseded : {funnel.watches_superseded}",
        f"  watches_pending    : {funnel.watches_pending_or_unresolved}",
        f"  external_triggers  : {funnel.external_indicator_triggers}",
        f"  exit_watch_triggers: {funnel.exit_watch_triggers}",
        f"  unjoinable_created : {funnel.unjoinable_watch_creations}",
        f"  buy_sell_proposed  : {funnel.buy_sell_proposed}",
        f"  orders_submitted   : {funnel.orders_submitted}",
        f"  fills              : {funnel.fills}",
        f"  non_decision_fills : {funnel.non_decision_fills}",
    ]
    if funnel.causal_linkage_available:
        lines.extend(
            [
                "  causal trigger branch (watch → governed attempt → decision → fill)",
                f"    attempts          : {funnel.trigger_process_attempts}",
                f"    decisions         : {funnel.trigger_decisions}",
                f"    holds             : {funnel.trigger_holds}",
                f"    trade_decisions   : {funnel.trigger_trade_decisions}",
                f"    orders_submitted  : {funnel.trigger_orders_submitted}",
                f"    fills             : {funnel.trigger_fills}",
                f"    watches_with_fill : {funnel.triggered_watches_with_fill}",
                f"    no_process_link   : {funnel.trigger_without_process}",
                f"    process_no_decision: {funnel.trigger_process_without_decision}",
                f"    triggered_cancelled: {funnel.triggered_cancelled}",
                f"  expiry decisions/fills: {funnel.expiry_review_decisions}/{funnel.expiry_review_fills}",
                f"  direct decisions/fills: {funnel.direct_trade_decisions}/{funnel.direct_fills}",
            ]
        )
    else:
        coverage = funnel.causal_coverage_start or "none"
        lines.append(
            "  causal trigger branch: unavailable "
            f"(missing/incomplete canonical process trace; coverage_start={coverage})"
        )
    return "\n".join(lines)


__all__ = [
    "StrategyFunnel",
    "StrategyFunnelSourceError",
    "compute_strategy_funnel",
    "load_strategy_funnel",
    "render_strategy_funnel",
]
