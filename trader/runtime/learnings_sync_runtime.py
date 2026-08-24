"""Best-effort background synchronization for FLAIR and MemRL."""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from trader.agent.learnings import embeddings as embeddings_mod
from trader.application.record.learning_outcomes import (
    MIN_OUTCOME_AGE,
    outcome_for_row,
    realised_entry_outcomes,
    uses_realised_outcome,
)
from trader.infrastructure.files.model_performance_jsonl import JsonlModelPerformanceReader
from trader.infrastructure.files import ledger_rotation
from trader.infrastructure.state_db.learnings_store import LearningsStore
from trader.runtime.protocols import LoggerLike


DEFAULT_EMBEDDING_BATCH_SIZE = 64
DEFAULT_OUTCOME_BATCH_SIZE = 128
DEFAULT_OUTCOME_INTERVAL_S = 3600.0
DEFAULT_MEMRL_ALPHA = 0.1

log = logging.getLogger(__name__)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_ts(raw: object) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc(value)


def _source_paths(state_dir: Path) -> tuple[tuple[Path, str, str], ...]:
    archive = state_dir / "archive"
    return (
        (archive / "learnings-from-ledger.jsonl", "ledger_backfill", "ledger-backfill"),
        (
            archive / "rationale-experiences.jsonl",
            "rationale_backfill",
            "llm-rationale-backfill",
        ),
        (archive / "learnings-evicted.jsonl", "evicted", "runtime"),
        (state_dir / "learnings.jsonl", "runtime", "runtime"),
    )


def source_fingerprint(state_dir: Path) -> tuple[tuple[str, int, int], ...]:
    rows: list[tuple[str, int, int]] = []
    for path, _key, _source in _source_paths(state_dir):
        try:
            stat = path.stat()
        except OSError:
            rows.append((str(path), 0, 0))
        else:
            rows.append((str(path), int(stat.st_size), int(stat.st_mtime_ns)))
    return tuple(rows)


def _lookback_for(rows: list[Mapping[str, object]], *, now: datetime) -> str:
    timestamps = [parsed for row in rows if (parsed := _parse_ts(row.get("ts") or row.get("cycle_ts")))]
    if not timestamps:
        return "5d"
    days = int((_utc(now) - min(timestamps)).total_seconds() // 86400) + 3
    if days <= 30:
        return f"{max(days, 5)}d"
    if days <= 90:
        return "3mo"
    if days <= 180:
        return "6mo"
    return "1y"


def _fetch_bars_by_symbol(
    rows: list[Mapping[str, object]],
    *,
    now: datetime,
    get_bars: Callable[..., list] | None,
) -> tuple[dict[str, list], list[dict[str, str]]]:
    if get_bars is None:
        return {}, [{"code": "data_source_unavailable"}]
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if symbol:
            grouped.setdefault(symbol, []).append(row)
    bars_by_symbol: dict[str, list] = {}
    errors: list[dict[str, str]] = []
    for symbol in sorted(grouped):
        try:
            bars_by_symbol[symbol] = list(
                get_bars(
                    symbol,
                    lookback=_lookback_for(grouped[symbol], now=now),
                    interval="1h",
                )
            )
        except Exception as exc:  # noqa: BLE001 - background outcome scoring is fail-open
            errors.append({"symbol": symbol, "code": type(exc).__name__})
    return bars_by_symbol, errors


def _refresh_outcomes(
    store: LearningsStore,
    *,
    state_dir: Path,
    now: datetime,
    get_bars: Callable[..., list] | None,
    limit: int,
    memrl_alpha: float,
) -> dict[str, object]:
    mature_before = (_utc(now) - MIN_OUTCOME_AGE).isoformat()
    pending_notes = store.pending_outcome_notes(
        mature_before=mature_before,
        limit=limit,
    )
    # La maturité porte sur la décision qui a consommé le rappel, pas sur
    # l'instant où la trace ``recalls`` a été persistée (une trace peut être
    # restaurée/importée après coup).
    pending_recall_ids = store.pending_recall_decision_ids(limit=limit)
    pending_global_rule_ids = store.pending_global_rule_decision_ids(limit=limit)

    decision_rows = ledger_rotation.read_rows_with_archive(
        state_dir / "decisions.jsonl",
        state_dir / "archive",
    )
    pending_note_decision_ids = {
        str(row.get("decision_id"))
        for row in pending_notes
        if row.get("decision_id")
    }
    delayed_decision_ids = set(pending_recall_ids) | set(pending_global_rule_ids)
    pending_decision_ids = pending_note_decision_ids | delayed_decision_ids
    decisions_by_id = {
        str(row.get("decision_id")): row
        for row in decision_rows
        if row.get("decision_id") in pending_decision_ids
    }
    outcome_rows_by_note_id = {
        row["id"]: {
            **row,
            **decisions_by_id.get(str(row.get("decision_id")), {}),
            "id": row["id"],
        }
        for row in pending_notes
    }
    delayed_rows = [
        decisions_by_id[decision_id]
        for decision_id in delayed_decision_ids
        if decision_id in decisions_by_id
    ]
    mature_rows = [
        row
        for row in [*outcome_rows_by_note_id.values(), *delayed_rows]
        if (ts := _parse_ts(row.get("ts") or row.get("cycle_ts"))) is not None
        and _utc(now) - ts >= MIN_OUTCOME_AGE
        and not uses_realised_outcome(row)
    ]
    bars_by_symbol, errors = _fetch_bars_by_symbol(mature_rows, now=now, get_bars=get_bars)
    realised_returns = realised_entry_outcomes(
        JsonlModelPerformanceReader(state_dir / "model_performance.jsonl")
    )

    note_updates: list[dict] = []
    for row in pending_notes:
        outcome_row = outcome_rows_by_note_id[row["id"]]
        outcome = outcome_for_row(
            outcome_row,
            bars=bars_by_symbol.get(str(outcome_row.get("symbol") or ""), []),
            now=now,
            realised_returns=realised_returns,
        )
        if outcome is not None:
            note_updates.append({"id": row["id"], **outcome})
    notes_updated = store.update_note_outcomes(note_updates)

    recalls_updated = 0
    q_values_updated = 0
    for decision_id in pending_recall_ids:
        row = decisions_by_id.get(decision_id)
        if row is None:
            continue
        outcome = outcome_for_row(
            row,
            bars=bars_by_symbol.get(str(row.get("symbol") or ""), []),
            now=now,
            realised_returns=realised_returns,
        )
        if outcome is None:
            continue
        applied = store.apply_recall_outcome(
            decision_id=decision_id,
            verdict=str(outcome["verdict"]),
            reward=outcome["reward"],  # type: ignore[arg-type]
            forward_return=outcome["forward_return"],  # type: ignore[arg-type]
            evaluated_at=_utc(now).isoformat(),
            alpha=memrl_alpha,
        )
        recalls_updated += int(applied["recalls_updated"])
        q_values_updated += int(applied["notes_updated"])

    global_rule_citations_updated = 0
    global_rule_q_values_updated = 0
    for decision_id in pending_global_rule_ids:
        row = decisions_by_id.get(decision_id)
        if row is None:
            continue
        outcome = outcome_for_row(
            row,
            bars=bars_by_symbol.get(str(row.get("symbol") or ""), []),
            now=now,
            realised_returns=realised_returns,
        )
        if outcome is None:
            continue
        applied = store.apply_global_rule_outcome(
            decision_id=decision_id,
            verdict=str(outcome["verdict"]),
            reward=outcome["reward"],  # type: ignore[arg-type]
            forward_return=outcome["forward_return"],  # type: ignore[arg-type]
            evaluated_at=_utc(now).isoformat(),
            alpha=memrl_alpha,
        )
        global_rule_citations_updated += int(applied["citations_updated"])
        global_rule_q_values_updated += int(applied["rules_updated"])

    scoring = store.compute_outcome_scores() if notes_updated else {"scored": 0, "base_rates": {}}
    return {
        "pending_notes": len(pending_notes),
        "pending_recalls": len(pending_recall_ids),
        "pending_global_rule_citations": len(pending_global_rule_ids),
        "notes_updated": notes_updated,
        "recalls_updated": recalls_updated,
        "q_values_updated": q_values_updated,
        "global_rule_citations_updated": global_rule_citations_updated,
        "global_rule_q_values_updated": global_rule_q_values_updated,
        "scored": int(scoring.get("scored") or 0),
        "errors": errors,
    }


def _refresh_universe_selections(
    state_dir: Path,
    *,
    get_bars: Callable[..., list] | None,
    limit: int,
) -> dict[str, object]:
    """Score mandate selections with the same market judge. Never raise."""

    if get_bars is None:
        return {"pending": 0, "evaluated": 0, "stored": 0, "skipped": "data_source_unavailable"}
    try:
        from trader.application.universe.selection_attribution import (
            refresh_selection_outcomes,
        )
        from trader.infrastructure.state_db.candidate_scope_store import CandidateScopeStore
        from trader.infrastructure.state_db.universe_selection_store import (
            try_open_universe_selection_store,
        )

        class _Bars:
            def get_bars(self, symbol: str, lookback: str, interval: str):
                return list(get_bars(symbol, lookback, interval))

        return refresh_selection_outcomes(
            state_dir,
            _Bars(),
            limit=limit,
            store_opener=try_open_universe_selection_store,
            scope_store_opener=lambda root: CandidateScopeStore(Path(root) / "candidate_scopes"),
        )
    except Exception as exc:  # noqa: BLE001 - universe FLAIR is advisory
        log.warning("[learnings_sync] universe selections failed: %s", exc)
        return {"pending": 0, "evaluated": 0, "stored": 0, "error": f"{type(exc).__name__}:{exc}"}


def _refresh_situation_notes(
    state_dir: Path,
    *,
    get_bars: Callable[..., list] | None,
    limit: int,
    now: datetime | None = None,
) -> dict[str, object]:
    """Score pending situation notes with the same market judge. Never raise."""

    if os.getenv("TRADER_SITUATION_OUTCOMES_ENABLED", "1") == "0":
        return {"pending": 0, "evaluated": 0, "stored": 0, "skipped": "disabled"}
    if get_bars is None:
        return {"pending": 0, "evaluated": 0, "stored": 0, "skipped": "data_source_unavailable"}
    db_path = state_dir / "situation_memory.db"
    if not db_path.is_file():
        return {"pending": 0, "evaluated": 0, "stored": 0, "skipped": "store_missing"}
    try:
        from trader.application.analyst.situation_attribution import (
            refresh_situation_outcomes,
        )
        from trader.infrastructure.state_db.situation_memory_store import (
            SituationMemoryStore,
        )

        class _Bars:
            def get_bars(self, symbol: str, lookback: str, interval: str):
                return list(get_bars(symbol, lookback, interval))

        store = SituationMemoryStore(db_path)
        try:
            return refresh_situation_outcomes(store, _Bars(), limit=limit, now=now)
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001 - situation FLAIR is advisory
        log.warning("[learnings_sync] situation notes failed: %s", exc)
        return {"pending": 0, "evaluated": 0, "stored": 0, "error": f"{type(exc).__name__}:{exc}"}


def run_learning_sync(
    *,
    state_dir: str | Path,
    now: datetime,
    get_bars: Callable[..., list] | None,
    include_outcomes: bool,
    apply_bootstrap: bool,
    embedder: Callable[..., list[bytes]] | None = None,
    api_key: str | None = None,
    embedding_batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE,
    outcome_batch_size: int = DEFAULT_OUTCOME_BATCH_SIZE,
    memrl_alpha: float = DEFAULT_MEMRL_ALPHA,
) -> dict[str, object]:
    """Synchronize canonical JSONL into the derived store, never raising outward."""

    root = Path(state_dir)
    store = LearningsStore(root / "learnings.db")
    try:
        ingest = {
            key: store.ingest_jsonl(path, source=source)
            for path, key, source in _source_paths(root)
        }
        verdicts_updated = 0
        bootstrap_error = None
        if apply_bootstrap:
            try:
                verdicts_updated = store.apply_verdicts(
                    root / "archive" / "learnings-outcome-bootstrap.json",
                    only_missing=True,
                )
                if verdicts_updated:
                    store.compute_outcome_scores()
            except (json.JSONDecodeError, ValueError) as exc:
                # An old bootstrap must never re-introduce pre-v2 HOLD labels.
                # Live replay below remains authoritative and best-effort.
                bootstrap_error = f"{type(exc).__name__}:{exc}"

        embeddings_backfilled = 0
        embedding_error = None
        selected_embedder = embedder or embeddings_mod.embed_texts
        selected_api_key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")
        if selected_api_key:
            try:
                embeddings_backfilled = store.backfill_embeddings(
                    lambda texts: selected_embedder(texts, api_key=selected_api_key),
                    limit=embedding_batch_size,
                )
            except Exception as exc:  # noqa: BLE001 - retry on next background tick
                embedding_error = f"{type(exc).__name__}:{exc}"
        embeddings_pending = store.count_missing_embeddings()

        outcomes = None
        if include_outcomes:
            outcomes = _refresh_outcomes(
                store,
                state_dir=root,
                now=now,
                get_bars=get_bars,
                limit=outcome_batch_size,
                memrl_alpha=memrl_alpha,
            )
            outcomes["universe_selections"] = _refresh_universe_selections(
                root,
                get_bars=get_bars,
                limit=outcome_batch_size,
            )
            outcomes["situation_notes"] = _refresh_situation_notes(
                root,
                get_bars=get_bars,
                limit=outcome_batch_size,
                now=now,
            )
        universe = outcomes.get("universe_selections") if isinstance(outcomes, dict) else None
        situation = outcomes.get("situation_notes") if isinstance(outcomes, dict) else None
        more_outcomes = bool(
            isinstance(outcomes, dict)
            and int(outcomes.get("pending_notes") or 0) >= outcome_batch_size
            and int(outcomes.get("notes_updated") or 0) > 0
        ) or bool(
            isinstance(outcomes, dict)
            and (
                int(outcomes.get("recalls_updated") or 0) >= outcome_batch_size
                or int(outcomes.get("global_rule_citations_updated") or 0) >= outcome_batch_size
            )
        ) or bool(
            isinstance(universe, dict) and int(universe.get("pending") or 0) >= outcome_batch_size
        ) or bool(
            isinstance(situation, dict)
            and int(situation.get("evaluated") or 0) > 0
            and int(situation.get("pending") or 0) >= outcome_batch_size
        )
        return {
            "as_of": _utc(now).isoformat(),
            "ingest": ingest,
            "verdicts_updated": verdicts_updated,
            "bootstrap_error": bootstrap_error,
            "embeddings_backfilled": embeddings_backfilled,
            "embeddings_pending": embeddings_pending,
            "embedding_error": embedding_error,
            "outcomes": outcomes,
            "more_work": bool(
                (embeddings_backfilled > 0 and embeddings_pending > 0)
                or more_outcomes
            ),
            "more_outcomes": more_outcomes,
            "total_notes": store.count(),
        }
    finally:
        store.close()


def _write_status(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class LearningSyncRunner:
    """Single-flight coalescing worker; trading never waits for FLAIR maintenance."""

    def __init__(
        self,
        *,
        state_dir: str | Path,
        get_bars: Callable[..., list] | None,
        logger: LoggerLike | None = None,
        now_fn: Callable[[], datetime] | None = None,
        sync_fn: Callable[..., dict[str, object]] = run_learning_sync,
        outcome_interval_s: float = DEFAULT_OUTCOME_INTERVAL_S,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.get_bars = get_bars
        self.log = logger or logging.getLogger("casys-trader")
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.sync_fn = sync_fn
        self.outcome_interval_s = max(60.0, float(outcome_interval_s))
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._pending = False
        self._stopping = False
        self._bootstrapped = False
        self._last_outcome_at: datetime | None = None
        self._fingerprint: tuple[tuple[str, int, int], ...] | None = None

    def trigger(self, *, reason: str, force: bool = False) -> dict[str, object]:
        if os.getenv("TRADER_LEARNINGS_AUTO_SYNC_ENABLED", "1") == "0":
            return {"triggered": False, "reason": "disabled"}
        with self._lock:
            if self._stopping:
                return {"triggered": False, "reason": "stopping"}
            current_fingerprint = source_fingerprint(self.state_dir)
            due_outcomes = self._outcomes_due(self.now_fn())
            needs_work = force or current_fingerprint != self._fingerprint or due_outcomes
            if not needs_work:
                return {"triggered": False, "reason": "unchanged"}
            if self._thread is not None:
                self._pending = True
                return {"triggered": False, "reason": "queued_latest"}
            thread = threading.Thread(
                target=self._run_loop,
                kwargs={"reason": reason},
                daemon=True,
                name="learnings-sync",
            )
            self._thread = thread
            thread.start()
        return {"triggered": True, "reason": reason, "_thread": thread}

    def _outcomes_due(self, now: datetime) -> bool:
        if self._last_outcome_at is None:
            return True
        return (_utc(now) - self._last_outcome_at).total_seconds() >= self.outcome_interval_s

    def _run_loop(self, *, reason: str) -> None:
        while True:
            now = _utc(self.now_fn())
            include_outcomes = self._outcomes_due(now)
            try:
                result = self.sync_fn(
                    state_dir=self.state_dir,
                    now=now,
                    get_bars=self.get_bars,
                    include_outcomes=include_outcomes,
                    apply_bootstrap=not self._bootstrapped,
                )
                status = {"status": "ok", "reason": reason, **result}
                self._bootstrapped = True
                if include_outcomes and not result.get("more_outcomes"):
                    self._last_outcome_at = now
                self._fingerprint = source_fingerprint(self.state_dir)
                _write_status(self.state_dir / "learnings_sync_status.json", status)
                self.log.info(
                    "[learnings_sync] notes=%s embedded=%s outcomes=%s",
                    result.get("total_notes"),
                    result.get("embeddings_backfilled"),
                    (result.get("outcomes") or {}).get("notes_updated") if isinstance(result.get("outcomes"), dict) else 0,
                )
                if result.get("more_work"):
                    with self._lock:
                        self._pending = True
            except Exception as exc:  # noqa: BLE001 - background maintenance is fail-open
                _write_status(
                    self.state_dir / "learnings_sync_status.json",
                    {
                        "status": "error",
                        "reason": reason,
                        "as_of": now.isoformat(),
                        "error": f"{type(exc).__name__}:{exc}",
                    },
                )
                self.log.warning("[learnings_sync] failed: %s", exc)
            with self._lock:
                if self._stopping or not self._pending:
                    if self._thread is threading.current_thread():
                        self._thread = None
                    return
                self._pending = False
                reason = "coalesced"

    def status(self) -> dict[str, object]:
        path = self.state_dir / "learnings_sync_status.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        with self._lock:
            return {
                **(payload if isinstance(payload, dict) else {}),
                "running": self._thread is not None,
                "pending": self._pending,
            }

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            self._pending = False
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)


__all__ = [
    "LearningSyncRunner",
    "run_learning_sync",
    "source_fingerprint",
]
