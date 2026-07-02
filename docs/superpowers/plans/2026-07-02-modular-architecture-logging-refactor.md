# Modular Architecture Logging Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `casys-trader` toward a modular monolith with light hexagonal boundaries, preserve runtime behavior, and improve operational logging on the existing logging framework.

**Architecture:** Extract cohesive application services from `trader/daemon.py` first, keeping compatibility wrappers so current tests and imports keep working. Split protocol, tool, and UI/read-model modules only after the runtime seams are stable. Use project-owned interfaces and standard `logging`; introduce third-party helpers only when a task records a concrete reduction in complexity.

**Tech Stack:** Python 3.11+, standard `dataclasses`, `typing.Protocol`, stdlib `logging` with `trader.logging_setup`, pytest, ruff, existing Rich/Textual UI stack.

---

## Baseline

Already verified in the worktree after copying ignored `config/universe.yaml` from the main checkout:

- `uv run ruff check trader tests` -> pass.
- `uv run pytest -q` -> `1852 passed, 1 skipped`.

Keep this ignored config file local to the worktree; do not add it to Git.

## Files and Responsibilities

- Create `trader/application/__init__.py`: package marker and migration namespace.
- Create `trader/application/planner_batch.py`: batch planner orchestration extracted from `daemon._batch_decide`.
- Create `trader/application/decision_recorder.py`: decision persistence/report/status/event/learning service.
- Create `trader/application/market_snapshot.py`: market bars, stale data, daily planning data, exit bars, FX rates, execution eligibility.
- Create `trader/application/runtime_logging.py`: optional small helper for consistent structured log fragments if repeated patterns appear.
- Later create `trader/agent_protocol/*`: types, prompts, parsing behind the `codex_client` facade.
- Later convert `trader/agent_tools.py` to `trader/agent_tools/` package with `core.py`, handler modules, and `registry.py`.
- Later create `trader/read_models/runtime_state.py` and `trader/ui/rich_panels.py`.
- Modify `trader/daemon.py`: keep entrypoint and high-level orchestration; delegate extracted slices through wrappers during migration.
- Modify docs: `docs/architecture.md` after each major slice if the live code map changes.

---

## Task 1: Extract Planner Batch

**Files:**
- Create: `trader/application/__init__.py`
- Create: `trader/application/planner_batch.py`
- Modify: `trader/daemon.py`
- Modify: `tests/test_daemon_batch.py`
- Modify: `tests/test_daemon_tool_round.py`

- [ ] **Step 1: Write failing import/compat tests**

Add near the top of `tests/test_daemon_batch.py`:

```python
from trader.application import planner_batch
```

Add this test after `_COMMON`:

```python
def test_planner_batch_module_expose_batch_decide() -> None:
    assert callable(planner_batch.batch_decide)
```

Add this test after `test_batch_decide_injecte_age_data_et_session_par_symbole`:

```python
def test_daemon_batch_decide_wrapper_delegue_au_module_application(monkeypatch) -> None:
    captured: dict = {}

    def fake_batch_decide(**kwargs):
        captured.update(kwargs)
        return {"SPY": Decision.hold("SPY", "module")}, 0

    monkeypatch.setattr(planner_batch, "batch_decide", fake_batch_decide)

    decisions, calls = daemon._batch_decide(
        decidable=["SPY"],
        max_model_calls=1,
        **_COMMON,
    )

    assert calls == 0
    assert decisions["SPY"].rationale == "module"
    assert captured["decidable"] == ["SPY"]
```

- [ ] **Step 2: Run RED**

Run:

```bash
uv run pytest tests/test_daemon_batch.py::test_planner_batch_module_expose_batch_decide tests/test_daemon_batch.py::test_daemon_batch_decide_wrapper_delegue_au_module_application -q
```

Expected: fails with `ModuleNotFoundError` or missing `batch_decide`.

- [ ] **Step 3: Create package and move planner code**

Create `trader/application/__init__.py`:

```python
"""Application services for the casys-trader runtime."""
```

Create `trader/application/planner_batch.py` by moving these functions from `trader/daemon.py` with behavior unchanged:

- `_context_request_summary`
- `_active_watch_summaries_by_symbol`
- `_run_tool_round`
- `_batch_decide`, renamed to `batch_decide`

Use module-local logger:

```python
import logging

log = logging.getLogger(__name__)
```

Keep dependencies explicit:

```python
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime
from typing import Callable

from trader import agent_tools, codex_client
from trader.agent_context import resolve_indicator_requests
from trader.indicator_watch import summarize_watch
from trader.tools import market, scheduler
```

Define default constants in the module to match daemon behavior:

```python
DEFAULT_DECISION_BATCH_SIZE = 5
DEFAULT_DECISION_BATCH_PARALLELISM = 3
```

In `trader/daemon.py`, import the module:

```python
from .application import planner_batch
```

Replace `_batch_decide` body with a compatibility wrapper:

```python
def _batch_decide(**kwargs):
    return planner_batch.batch_decide(**kwargs)
```

If existing tests still patch `daemon.resolve_indicator_requests`, pass a resolver callable through the wrapper:

```python
def _batch_decide(**kwargs):
    kwargs.setdefault("indicator_request_resolver", resolve_indicator_requests)
    return planner_batch.batch_decide(**kwargs)
```

Then let `planner_batch.batch_decide` accept `indicator_request_resolver: Callable = resolve_indicator_requests` and use it in both the tool resolver and legacy `REQUEST_CONTEXT` resolver.

- [ ] **Step 4: Run GREEN**

Run:

```bash
uv run pytest tests/test_daemon_batch.py tests/test_daemon_tool_round.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Add planner logging test**

Add to `tests/test_daemon_batch.py`:

```python
def test_batch_decide_loggue_le_decoupage_des_chunks(monkeypatch, caplog) -> None:
    caplog.set_level("DEBUG", logger="trader.application.planner_batch")

    def fake_batch(*, symbols, **kwargs):
        return {sym: Decision.hold(sym, "x") for sym in symbols}

    monkeypatch.setattr(planner_batch.codex_client, "decide_batch", fake_batch)

    planner_batch.batch_decide(
        decidable=["A", "B", "C"],
        max_model_calls=2,
        decision_batch_size=2,
        decision_batch_parallelism=1,
        **_COMMON,
    )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "planner_batch chunks=2 allowed=2 skipped=0" in messages
```

Implement one debug log after chunk budgeting:

```python
log.debug(
    "planner_batch chunks=%d allowed=%d skipped=%d budget=%d tool_mode=%s",
    len(chunks),
    len(allowed_chunks),
    len(skipped),
    budget,
    bool(agent_tools_enabled and allow_context_request),
)
```

- [ ] **Step 6: Run verification and commit**

Run:

```bash
uv run ruff check trader tests
uv run pytest tests/test_daemon_batch.py tests/test_daemon_tool_round.py -q
uv run pytest -q
```

Expected: ruff passes and full suite passes.

Commit:

```bash
git add trader/application trader/daemon.py tests/test_daemon_batch.py tests/test_daemon_tool_round.py
git commit -m "refactor(runtime): extract planner batch service"
```

---

## Task 2: Extract Decision Recorder

**Files:**
- Create: `trader/application/decision_recorder.py`
- Modify: `trader/daemon.py`
- Create: `tests/test_decision_recorder.py`
- Modify: `tests/test_daemon_status.py`

- [ ] **Step 1: Write failing recorder tests**

Create `tests/test_decision_recorder.py`:

```python
import json
from datetime import datetime, timezone

from trader.application.decision_recorder import DecisionRecorder
from trader import decision_ledger


class FakeLearnings:
    def __init__(self):
        self.rows = []

    def append(self, **kwargs):
        self.rows.append(kwargs)
        return {"appended": True, "note": kwargs["note"]}


class FakeRecallStore:
    def __init__(self):
        self.calls = []

    def record_recall(self, *, decision_id, note_ids):
        self.calls.append({"decision_id": decision_id, "note_ids": note_ids})


def test_decision_recorder_appends_report_ledger_status_and_event(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    report = {
        "ts": "2026-07-02T10:00:00+00:00",
        "decisions": [],
        "model_calls_used": 0,
        "prices": {"SPY": 100.0},
        "symbols_due": ["SPY"],
        "portfolio": {"equity": 100000.0},
    }
    statuses = []
    events = []

    recorder = DecisionRecorder(
        report=report,
        state_dir=state_dir,
        dry_run=True,
        symbols_total=1,
        max_model_calls_per_cycle=25,
        learnings_store=FakeLearnings(),
        decision_ledger_store=decision_ledger.DecisionLedgerStore(state_dir / "decisions.jsonl"),
        refresh_report_portfolio=lambda: None,
        write_current_report=lambda payload: (state_dir / "current_report.json").write_text(json.dumps(payload)),
        write_status=lambda phase, **payload: statuses.append({"phase": phase, **payload}),
        append_event=lambda event, **payload: events.append({"event": event, **payload}),
        news_snapshot=lambda symbol, now: {"coverage": "none"},
        macro_next={"event": "cpi"},
        now=datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc),
        recall_store=None,
    )

    recorder.record({
        "symbol": "SPY",
        "action": "HOLD",
        "qty": 0.0,
        "confidence": 0.0,
        "rationale": "quiet_gate",
        "intent": "HOLD",
        "executed": False,
        "reason": "quiet_gate",
    })

    assert report["decisions"][0]["news"]["macro_next"] == {"event": "cpi"}
    assert statuses[-1]["phase"] == "decision_recorded"
    assert events[-1]["event"] == "decision_recorded"
    rows = [json.loads(line) for line in (state_dir / "decisions.jsonl").read_text().splitlines()]
    assert rows[0]["decision_id"] == "2026-07-02T10:00:00+00:00|0|SPY"
```

- [ ] **Step 2: Run RED**

Run:

```bash
uv run pytest tests/test_decision_recorder.py -q
```

Expected: fails because `trader.application.decision_recorder` does not exist.

- [ ] **Step 3: Implement DecisionRecorder**

Create `trader/application/decision_recorder.py` with:

```python
"""Decision recording service for the runtime cycle."""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from trader import decision_ledger

log = logging.getLogger(__name__)


@dataclass
class DecisionRecorder:
    report: dict
    state_dir: Path
    dry_run: bool
    symbols_total: int
    max_model_calls_per_cycle: int
    learnings_store: Any
    decision_ledger_store: Any
    refresh_report_portfolio: Callable[[], None]
    write_current_report: Callable[[dict], None]
    write_status: Callable[..., None]
    append_event: Callable[..., None]
    news_snapshot: Callable[[str, datetime], dict]
    macro_next: dict | None
    now: datetime
    recall_store: Any | None = None
    merge_gate_feedback: Callable[[Any, Any, Any], str | None] | None = None

    def record(self, decision_entry: dict) -> None:
        symbol = str(decision_entry["symbol"])
        decision_entry.setdefault("news", self.news_snapshot(symbol, self.now))
        news = decision_entry.get("news")
        if isinstance(news, dict) and "macro_next" not in news:
            decision_entry["news"] = {**news, "macro_next": self.macro_next}
        if self.merge_gate_feedback is not None:
            note = self.merge_gate_feedback(
                decision_entry.get("reason"),
                decision_entry.get("context"),
                decision_entry.get("learning"),
            )
            if note:
                decision_entry["learning_recorded"] = self.learnings_store.append(
                    symbol=symbol,
                    note=note,
                    now=self.now,
                    action=decision_entry.get("action"),
                    intent=decision_entry.get("intent"),
                    executed=decision_entry.get("executed"),
                    reason=decision_entry.get("reason"),
                    dry_run=self.dry_run,
                )
        sequence = len(self.report["decisions"])
        self.report["decisions"].append(decision_entry)
        self.refresh_report_portfolio()
        self.decision_ledger_store.append(
            decision_ledger.build_decision_row(
                self.report,
                decision_entry,
                sequence=sequence,
                source="armed_plan" if decision_entry.get("armed_plan_id") else "daemon",
            )
        )
        self._record_recall_trace(decision_entry, sequence)
        self.write_current_report(self.report)
        self.write_status(
            "decision_recorded",
            current_symbol=symbol,
            decisions_done=len(self.report["decisions"]),
            symbols_total=self.symbols_total,
            last_decision=decision_entry,
            model_calls_used=self.report.get("model_calls_used", 0),
            max_model_calls_per_cycle=self.max_model_calls_per_cycle,
        )
        self.append_event(
            "decision_recorded",
            symbol=symbol,
            action=decision_entry.get("action"),
            reason=decision_entry.get("reason"),
            executed=decision_entry.get("executed"),
        )
        log.debug("decision_recorded symbol=%s reason=%s executed=%s", symbol, decision_entry.get("reason"), decision_entry.get("executed"))

    def _record_recall_trace(self, decision_entry: dict, sequence: int) -> None:
        if self.recall_store is None:
            return
        cycle_ts = str(self.report.get("ts") or "")
        symbol = str(decision_entry.get("symbol") or "")
        if not cycle_ts or not symbol:
            return
        decision_id = decision_ledger._decision_id(cycle_ts, sequence, symbol)
        for tool_call in decision_entry.get("tool_calls") or []:
            if tool_call.get("tool") != "recall_learnings" or tool_call.get("outcome") != "ok":
                continue
            note_ids = [
                note_id
                for note_id in (tool_call.get("detail") or {}).get("note_ids", [])
                if isinstance(note_id, int)
            ]
            if note_ids:
                self.recall_store.record_recall(decision_id=decision_id, note_ids=note_ids)
```

- [ ] **Step 4: Replace nested daemon function**

In `trader/daemon.py`, instantiate `DecisionRecorder` after `_cycle_macro_next`.
Replace calls to `record_decision(entry)` with `recorder.record(entry)` and keep
a local alias if that keeps the diff small:

```python
recorder = DecisionRecorder(...)
record_decision = recorder.record
```

- [ ] **Step 5: Run GREEN and commit**

Run:

```bash
uv run pytest tests/test_decision_recorder.py tests/test_daemon_status.py tests/test_decision_ledger.py -q
uv run pytest -q
uv run ruff check trader tests
```

Expected: selected tests and full suite pass.

Commit:

```bash
git add trader/application/decision_recorder.py trader/daemon.py tests/test_decision_recorder.py tests/test_daemon_status.py
git commit -m "refactor(runtime): extract decision recorder"
```

---

## Task 3: Extract Market Snapshot

**Files:**
- Create: `trader/application/market_snapshot.py`
- Modify: `trader/daemon.py`
- Create: `tests/test_market_snapshot.py`

- [ ] **Step 1: Write failing dataclass/import test**

Create `tests/test_market_snapshot.py`:

```python
from datetime import datetime, timezone

from trader.application.market_snapshot import MarketSnapshot, build_market_snapshot
from trader.tools.market import Bar


class FakeSource:
    def __init__(self):
        self.calls = []

    def get_bars(self, symbol, lookback, interval):
        self.calls.append((symbol, lookback, interval))
        return [Bar(ts="2026-07-02T10:00:00+00:00", open=1.0, high=1.0, low=1.0, close=100.0, volume=1.0)]

    def last_source(self, symbol):
        return "fake"


def test_market_snapshot_returns_prices_and_sources(tmp_path):
    source = FakeSource()
    snapshot = build_market_snapshot(
        symbols=["SPY"],
        data_source=source,
        now=datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc),
        max_market_data_age_minutes=40.0,
        runtime_interval="15m",
        runtime_lookback="5d",
        config_dir=tmp_path,
        plan_store=None,
    )

    assert isinstance(snapshot, MarketSnapshot)
    assert snapshot.prices == {"SPY": 100.0}
    assert snapshot.runtime_data_source_by_symbol == {"SPY": "fake"}
```

- [ ] **Step 2: Run RED**

Run:

```bash
uv run pytest tests/test_market_snapshot.py -q
```

Expected: import failure.

- [ ] **Step 3: Implement first MarketSnapshot slice**

Create `trader/application/market_snapshot.py` with `MarketSnapshot` dataclass and move the runtime bar loop first. Keep daily, FX, and exit bars in daemon until follow-up steps inside this task.

Initial dataclass:

```python
@dataclass(frozen=True)
class MarketSnapshot:
    bars_by_symbol: dict[str, list]
    prices: dict[str, float]
    stale_market_data: dict[str, dict]
    data_age_by_symbol: dict[str, float]
    runtime_data_source_by_symbol: dict[str, str | None]
```

Initial `build_market_snapshot(...)` should:

- call `data_source.get_bars` for each symbol;
- fill `bars_by_symbol`, `prices`, and `runtime_data_source_by_symbol`;
- call `market.assess_freshness`;
- fill `stale_market_data` and `data_age_by_symbol`;
- log counts with `logging.getLogger(__name__)`.

- [ ] **Step 4: Expand tests for stale handling and source capture**

Add:

```python
def test_market_snapshot_marks_stale_runtime_data(tmp_path):
    class StaleSource(FakeSource):
        def get_bars(self, symbol, lookback, interval):
            return [Bar(ts="2026-07-01T00:00:00+00:00", open=1.0, high=1.0, low=1.0, close=100.0, volume=1.0)]

    snapshot = build_market_snapshot(
        symbols=["SPY"],
        data_source=StaleSource(),
        now=datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc),
        max_market_data_age_minutes=40.0,
        runtime_interval="15m",
        runtime_lookback="5d",
        config_dir=tmp_path,
        plan_store=None,
    )

    assert "SPY" in snapshot.stale_market_data
    assert snapshot.data_age_by_symbol["SPY"] > 40.0
```

- [ ] **Step 5: Move daily bars, FX, and exit-bar helpers**

Extend `MarketSnapshot` with:

```python
daily_bars_by_symbol: dict[str, list]
tradable_prices: dict[str, float]
tradable_symbols: list[str]
tradable_bars_by_symbol: dict[str, list]
fx_rate_by_ccy: dict[str, float]
rate_for_symbol: Callable[[str], float]
execution_eligibility: dict[str, dict]
exit_bars_by_symbol: dict[str, list]
exit_intervals_by_symbol: dict[str, str]
```

Move logic from `run_cycle` with behavior unchanged. Keep daemon compatibility by assigning local variables from the snapshot.

- [ ] **Step 6: Run verification and commit**

Run:

```bash
uv run pytest tests/test_market_snapshot.py tests/test_daemon_data_sources.py tests/test_daemon_stale_backoff.py tests/test_daemon_exit_engine.py -q
uv run pytest -q
uv run ruff check trader tests
```

Commit:

```bash
git add trader/application/market_snapshot.py trader/daemon.py tests/test_market_snapshot.py
git commit -m "refactor(runtime): extract market snapshot builder"
```

---

## Task 4: Split Agent Protocol Behind `codex_client`

**Files:**
- Create: `trader/agent_protocol/__init__.py`
- Create: `trader/agent_protocol/types.py`
- Create: `trader/agent_protocol/prompts.py`
- Create: `trader/agent_protocol/parsing.py`
- Modify: `trader/codex_client.py`
- Modify: `tests/test_codex_client.py`

- [ ] **Step 1: Write failing module tests**

Add to `tests/test_codex_client.py`:

```python
def test_agent_protocol_modules_exposent_les_contrats_publics():
    from trader.agent_protocol import parsing, prompts, types

    assert types.Decision is codex_client.Decision
    assert callable(prompts.build_batch_prompt)
    assert callable(parsing.parse_batch)
```

- [ ] **Step 2: Run RED**

Run:

```bash
uv run pytest tests/test_codex_client.py::test_agent_protocol_modules_exposent_les_contrats_publics -q
```

Expected: import failure.

- [ ] **Step 3: Move types, prompts, parsing**

Move dataclasses/literals into `types.py`, prompt constants/builders into `prompts.py`, and parse helpers into `parsing.py`. In `codex_client.py`, re-export:

```python
from trader.agent_protocol.types import (
    Action,
    BatchToolCallRequest,
    ContextResearchRequest,
    Decision,
    IndicatorRequest,
    Intent,
)
from trader.agent_protocol.prompts import build_batch_prompt, build_prompt
from trader.agent_protocol.parsing import (
    parse_batch,
    parse_batch_or_tool_calls,
    parse_decision,
    parse_decision_or_context_request,
)
```

Keep `decide` and `decide_batch` in `codex_client.py` as the transport facade.

- [ ] **Step 4: Run verification and commit**

Run:

```bash
uv run pytest tests/test_codex_client.py tests/test_daemon_batch.py tests/test_daemon_tool_round.py -q
uv run pytest -q
uv run ruff check trader tests
```

Commit:

```bash
git add trader/agent_protocol trader/codex_client.py tests/test_codex_client.py
git commit -m "refactor(agent): split protocol contracts"
```

---

## Task 5: Convert Agent Tools to Package

**Files:**
- Create directory: `trader/agent_tools/`
- Create: `trader/agent_tools/__init__.py`
- Create: `trader/agent_tools/core.py`
- Create: `trader/agent_tools/freshness.py`
- Create: `trader/agent_tools/plans.py`
- Create: `trader/agent_tools/risk.py`
- Create: `trader/agent_tools/attribution.py`
- Create: `trader/agent_tools/indicators.py`
- Create: `trader/agent_tools/learnings.py`
- Create: `trader/agent_tools/registry.py`
- Delete after migration: `trader/agent_tools.py`
- Modify: `tests/test_agent_tools.py`

- [ ] **Step 1: Write failing package import tests**

Add to `tests/test_agent_tools.py`:

```python
def test_agent_tools_package_expose_public_registry():
    import trader.agent_tools as agent_tools
    from trader.agent_tools import core, registry

    assert agent_tools.TOOL_REGISTRY is registry.TOOL_REGISTRY
    assert core.ToolContext is agent_tools.ToolContext
    assert "get_freshness" in agent_tools.TOOL_REGISTRY
```

- [ ] **Step 2: Run RED**

Run:

```bash
uv run pytest tests/test_agent_tools.py::test_agent_tools_package_expose_public_registry -q
```

Expected: fails until package split is complete.

- [ ] **Step 3: Move core and handlers**

Move dataclasses, scrubbers, validation, budgets, execution, and serialization helpers into `core.py`.

Move handlers into the named modules and expose each module's `SPECS` list:

```python
SPECS = [
    ToolSpec(name="get_freshness", validate_args=_validate_get_freshness, handler=_handle_get_freshness),
]
```

Assemble in `registry.py`:

```python
from trader.agent_tools import attribution, freshness, indicators, learnings, plans, risk

TOOL_REGISTRY = {}
for module in (freshness, plans, risk, attribution, indicators, learnings):
    for spec in module.SPECS:
        TOOL_REGISTRY[spec.name] = spec
```

In `__init__.py`, re-export public names used today.

- [ ] **Step 4: Run verification and commit**

Run:

```bash
uv run pytest tests/test_agent_tools.py tests/test_daemon_tool_round.py tests/test_tool_trace.py tests/test_tool_usage.py -q
uv run pytest -q
uv run ruff check trader tests
```

Commit:

```bash
git add trader/agent_tools tests/test_agent_tools.py
git rm trader/agent_tools.py
git commit -m "refactor(agent): split domain tools package"
```

---

## Task 6: Extract Read Models and Rich UI Panels

**Files:**
- Create: `trader/read_models/__init__.py`
- Create: `trader/read_models/runtime_state.py`
- Create: `trader/ui/__init__.py`
- Create: `trader/ui/rich_panels.py`
- Modify: `trader/tui.py`
- Modify: `trader/cockpit.py`
- Modify: `tests/test_tui.py`
- Modify: `tests/test_tui_dashboard.py`
- Modify: `tests/test_cockpit_smoke.py`

- [ ] **Step 1: Write failing public import tests**

Add to `tests/test_tui.py`:

```python
def test_runtime_state_read_model_est_import_public():
    from trader.read_models.runtime_state import load_runtime_state

    assert callable(load_runtime_state)


def test_rich_panels_exports_public_builders():
    from trader.ui import rich_panels

    assert callable(rich_panels.build_view)
    assert callable(rich_panels.build_universe_panel)
```

- [ ] **Step 2: Run RED**

Run:

```bash
uv run pytest tests/test_tui.py::test_runtime_state_read_model_est_import_public tests/test_tui.py::test_rich_panels_exports_public_builders -q
```

Expected: import failure.

- [ ] **Step 3: Move state readers**

Move file-reading helpers and `load_runtime_state` from `trader/tui.py` to `trader/read_models/runtime_state.py`.

Keep `trader/tui.py` compatibility imports:

```python
from trader.read_models.runtime_state import load_runtime_state
```

- [ ] **Step 4: Move Rich panel builders**

Move pure render helpers and `build_view` to `trader/ui/rich_panels.py`.

Update `trader/cockpit.py` imports to use `trader.ui.rich_panels` instead of private `trader.tui` symbols.

Keep `trader/tui.py` as CLI shell:

```python
from trader.ui.rich_panels import build_view
```

- [ ] **Step 5: Run verification and commit**

Run:

```bash
uv run pytest tests/test_tui.py tests/test_tui_dashboard.py tests/test_cockpit_smoke.py tests/test_tui_builders_v2.py -q
uv run pytest -q
uv run ruff check trader tests
```

Commit:

```bash
git add trader/read_models trader/ui trader/tui.py trader/cockpit.py tests/test_tui.py tests/test_tui_dashboard.py tests/test_cockpit_smoke.py
git commit -m "refactor(ui): split runtime read models and rich panels"
```

---

## Task 7: Extract Order Admission

**Files:**
- Create: `trader/application/order_admission.py`
- Modify: `trader/daemon.py`
- Create: `tests/test_order_admission.py`
- Modify: relevant daemon risk/exit tests

- [ ] **Step 1: Write failing narrow unit tests**

Create `tests/test_order_admission.py`:

```python
from trader.application.order_admission import invalid_intent_reason, clamp_exit_quantity
from trader.codex_client import Decision


def test_invalid_intent_reason_rejects_buy_hold_mismatch():
    decision = Decision(symbol="SPY", action="BUY", quantity=1.0, confidence=0.8, rationale="x", intent="HOLD")

    assert invalid_intent_reason(decision) == "invalid_intent:action_mismatch"


def test_clamp_exit_quantity_blocks_close_without_position():
    qty, reason = clamp_exit_quantity(intent="CLOSE", action="SELL", quantity=10.0, position_quantity=0.0)

    assert qty == 0.0
    assert reason == "no_position_to_close"
```

- [ ] **Step 2: Run RED**

Run:

```bash
uv run pytest tests/test_order_admission.py -q
```

Expected: import failure.

- [ ] **Step 3: Move pure helpers first**

Move or wrap these functions from `daemon.py`:

- `_invalid_intent_reason` -> `invalid_intent_reason`
- `_clamp_exit_quantity` -> `clamp_exit_quantity`
- `_hard_stop_price`
- `_hard_stop_wrong_side`
- `_reverse_open_quantity`
- `_risk_pct_for_quantity`
- `_set_entry_risk_metrics`

Keep daemon compatibility wrappers until all call sites move.

- [ ] **Step 4: Move admission orchestration**

Create an `OrderAdmissionContext` dataclass only after pure helper tests pass. Move the decision-to-fill block from the main `for sym in symbols_to_decide` loop into a callable that returns an updated decision entry and optional fill effects.

Keep side effects explicit: broker, gate, plan store, model performance appender, scheduler callback, and recorder callback are passed in.

- [ ] **Step 5: Run verification and commit**

Run:

```bash
uv run pytest tests/test_order_admission.py tests/test_daemon_confidence_gate.py tests/test_daemon_sizing.py tests/test_daemon_exit_engine.py tests/test_risk.py -q
uv run pytest -q
uv run ruff check trader tests
```

Commit:

```bash
git add trader/application/order_admission.py trader/daemon.py tests/test_order_admission.py
git commit -m "refactor(runtime): extract order admission"
```

---

## Task 8: Final Architecture Docs and Library Decision Audit

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/superpowers/specs/2026-07-02-modular-architecture-logging-refactor-design.md` if implementation differs.
- Optional modify: `pyproject.toml`, `uv.lock` only if a justified library was added.

- [ ] **Step 1: Document final module map**

Update `docs/architecture.md` with:

- new `application/` runtime services;
- where planner protocol lives;
- where read models/UI builders live;
- statement that `state/decisions.jsonl` remains audit truth;
- logging setup note.

- [ ] **Step 2: Audit library additions**

Run:

```bash
git diff main...HEAD -- pyproject.toml uv.lock
```

If no dependency was added, record in docs:

```text
No third-party architecture helper was added; dataclasses, Protocols, and stdlib logging were sufficient for this tranche.
```

If a dependency was added, record:

- why standard tools were insufficient;
- which module owns the dependency;
- how tests cover the boundary.

- [ ] **Step 3: Final verification and commit**

Run:

```bash
uv run ruff check trader tests
uv run pytest -q
git status --short --branch
```

Expected:

- ruff passes;
- pytest full suite passes;
- status shows only intended docs changes before commit.

Commit:

```bash
git add docs/architecture.md docs/superpowers/specs/2026-07-02-modular-architecture-logging-refactor-design.md pyproject.toml uv.lock
git commit -m "docs(architecture): update modular runtime map"
```

---

## Completion Gate

Before claiming completion:

```bash
uv run ruff check trader tests
uv run pytest -q
git log --oneline --decorate -8
git status --short --branch
```

Completion requires:

- all checks pass;
- no unexpected working-tree changes;
- main checkout remains clean or receives the final merged/cherry-picked branch intentionally;
- `docs/architecture.md` reflects the final code shape;
- the final answer reports exact verification output.
