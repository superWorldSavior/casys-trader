# Modular architecture and logging refactor

**Date**: 2026-07-02
**Status**: Implemented in staged slices on `main`.
The implementation is behavior-preserving and keeps compatibility wrappers.

---

## 1. Context

`casys-trader` is already conceptually well bounded: the daemon owns execution,
the LLM plans, `RiskGate` is the fuse, and `state/decisions.jsonl` is the durable
audit truth. The code shape has drifted away from that clean conceptual model.

The main pressure points are:

- `trader/daemon.py` is the runtime shell, the market snapshot builder, the
  planner batch coordinator, the order admission path, the decision recorder,
  and part of observability.
- The former flat `trader/tui.py` mixed state-file reads, projection/enrichment,
  Rich rendering, and functions imported privately by the Textual cockpit.
- `trader/codex_client.py` combines prompt text, response contracts, parsing,
  and the planner facade.
- `trader/agent_tools.py` now contains a useful domain-tool registry, but the
  validation/execution core and individual tool handlers live in one growing
  file.
- Logging exists and is tested through `trader/logging_setup.py`; the refactor
  should strengthen that standard logging path rather than introduce `loguru`
  unless the standard-library logger cannot cover a concrete need.
- Third-party architecture helpers are allowed when they buy a concrete boundary,
  validation model, dependency seam, or observability improvement that would be
  noisy to hand-roll.

The refactor target is a modular monolith with hexagonal boundaries. It should
make the current doctrine visible in code without creating a framework that is
heavier than the trading system itself.

## 2. Goals

- Reduce the cognitive load of the daemon by extracting cohesive application
  services with explicit inputs and outputs.
- Preserve all live behavior: prompt contracts, state file formats, ledger rows,
  execution gates, scheduler semantics, and report fields.
- Keep execution daemon-owned. The LLM remains a planner and never obtains shell,
  filesystem, broker, or unrestricted network access.
- Make runtime stages observable with structured, consistent logging at the
  important boundaries.
- Keep logging on the existing `logging` + `RichHandler` framework unless a
  later implementation task proves a missing feature. Supporting libraries are
  acceptable for structured events, dependency injection, or schema validation
  when their value is explicit.
- Keep compatibility shims during the migration so tests and imports can move in
  small slices.
- Update architecture documentation in the same branch as the structural moves.

## 3. Non-goals

- No microservices.
- No async rewrite of the daemon loop.
- No change to trading strategy, risk thresholds, universe rotation policy, or
  exit-plan semantics.
- No direct broker or execution action from agent tools.
- No wholesale rename of every module into abstract layers before there is a
  working slice.
- No replacement of JSONL state with a database in this refactor.
- No adoption of `loguru` by default. A new logging dependency is allowed only if
  it removes a proven limitation that `logging_setup.py` cannot solve cleanly.
- No dependency added for aesthetics only. Each new library must have a written
  reason in the implementation task and a small usage surface.

## 4. Architecture Target

Use a modular monolith with Python-owned contracts close to their consumers. The
post-refactor target deliberately avoids generic `domain/`, `ports/`, or
`adapters/` packages until repeated slices prove those names carry their weight.
This is closer to capability packaging than to strict Clean/Hexagonal
Architecture: boundaries follow runtime ownership and auditability first.

```text
trader/
  application/
    market_snapshot.py
    planner_batch.py
    order_admission.py
    decision_recorder.py

  agent_protocol/
    types.py
    prompts.py
    parsing.py

  agent_tools/
    core.py
    freshness.py
    plans.py
    risk.py
    attribution.py
    indicators.py
    learnings.py

  rotation/
    core.py
    venues.py
    schedule.py
    wiring.py
    ...

  reporting/
    decision_ledger.py
    attribution.py
    stats.py
    tool_usage.py
    ...

  market/
    features.py
    fx.py
    radar.py
    regime.py
    ...

  config/
    pool.py
    portfolio.py

  runtime/
    process_env.py

  read_models/
    runtime_state.py
    cockpit_projection.py
    attribution_projection.py

  cockpit/
    app.py
    events.py
    supervisor.py

  ui/
    tui.py
    rich_panels.py
    palette.py
```

Protocols and type aliases live in the module that consumes the collaborator
unless at least two production modules share the same contract. This keeps the
daemon-owned execution model explicit without turning the repo into an
architecture framework.

## 5. Dependency Rules

- `application/*` owns cohesive runtime services and may define local
  `Protocol` classes for injected collaborators such as stores, providers, and
  data sources.
- Pure helpers such as `order_admission.py` should depend on primitive fields or
  small project DTOs, not on broad transport objects.
- Agent tool contracts stay in `agent_tools/core.py`; agent response contracts
  stay in `agent_protocol/`; `codex_client.py` remains the transport facade.
- `read_models/*` reads persisted state and builds display-friendly projections.
  It must not influence live trading decisions.
- `ui/*` renders read models. It must not read random state files directly once
  `read_models.runtime_state` exists.
- `trader/daemon.py`, `trader.tui`, `trader.cockpit`,
  `trader/codex_client.py`, and `trader/agent_tools.py` may keep compatibility
  re-exports or thin wrappers until tests and callers have moved.

## 6. Runtime Slices

### 6.1 Decision Recorder

First extraction candidate because it is cohesive and easy to test.

Create `trader/application/decision_recorder.py` with a `DecisionRecorder` or
`record_decision(...)` service that owns:

- enriching rows with news and `macro_next`;
- merging gate feedback into learnings;
- appending to report decisions;
- refreshing the report portfolio through an injected callback;
- appending to `DecisionLedgerStore`;
- recording `recall_learnings` note ids;
- writing `current_report.json`;
- writing `daemon_status.json`;
- appending `decision_recorded` events.

The service should receive explicit collaborators. It must not reach into global
`STATE_DIR` except through injected callables or stores.

### 6.2 Planner Batch

Move `_batch_decide()` into `trader/application/planner_batch.py`.

Responsibilities:

- chunking due symbols;
- enforcing model-call budgets;
- building per-symbol facts;
- running one optional domain-tool round;
- resolving legacy `REQUEST_CONTEXT`;
- returning `{symbol: Decision}` and call count.

The module may depend on `codex_client` during migration. Its tests should use a
fake planner callable instead of invoking `acpx`.

### 6.3 Market Snapshot

Move market-data preparation into `trader/application/market_snapshot.py`.

Responsibilities:

- runtime bar fetch and source capture;
- freshness/stale calculation;
- daily bar fetch and daily freshness;
- 5m exit-check bar fetch for open plans;
- FX rate loading/fetching;
- execution/planning eligibility construction.

Output should be a dataclass such as `MarketSnapshot` containing maps currently
passed around as separate dicts. The daemon can still unpack it while migration
is in progress.

### 6.4 Order Admission

Move decision-to-order handling into `trader/application/order_admission.py`.

Responsibilities:

- invalid intent checks;
- execution eligibility blocking;
- exit-plan validation and late binding;
- risk sizing and confidence gates;
- broker submission;
- model-performance row creation;
- trade-plan creation/sync/close;
- post-entry review scheduling hint.

This is the riskiest extraction and should happen only after recorder, planner,
and market snapshot have created clear seams.

Implementation note (2026-07-02): this branch extracted the pure admission
helpers only (`invalid_intent_reason`, `clamp_exit_quantity`, hard-stop helpers,
reverse quantity, risk metrics). The full decision-to-fill orchestration remains
in `daemon.py` because it couples scheduler semantics, recorder callbacks,
`RiskGate`, broker fills, model performance rows, and trade-plan mutations. This
keeps the live side-effect order unchanged; a later extraction should introduce
an explicit orchestration DTO/callback boundary first.

## 7. Agent Protocol Split

`trader/codex_client.py` remains the public facade, but internals should move
behind compatibility wrappers:

- `trader/agent_protocol/types.py`: `Decision`, `IndicatorRequest`,
  `ContextResearchRequest`, `BatchToolCallRequest`, literal action/intent types.
- `trader/agent_protocol/prompts.py`: prompt contracts, output schemas, tool
  catalog text, `build_prompt`, `build_batch_prompt`.
- `trader/agent_protocol/parsing.py`: `_extract_json`, decision parsing, batch
  parsing, tool-call parsing.

The public functions `codex_client.decide`, `codex_client.decide_batch`,
`codex_client.parse_batch`, and public dataclasses must keep working.

## 8. Agent Tools Split

`trader.agent_tools` should become a package with an explicit registry:

- `agent_tools/core.py`: dataclasses, scrubbers, validation, budget enforcement,
  `execute_tool_round`, serialization helpers.
- `agent_tools/freshness.py`: `get_freshness`.
- `agent_tools/plans.py`: `get_active_plans`.
- `agent_tools/risk.py`: `get_position_risk`.
- `agent_tools/attribution.py`: `get_attribution`, `get_recent_decisions`.
- `agent_tools/indicators.py`: `describe_data`, `find_indicators`,
  `get_indicator_context`.
- `agent_tools/learnings.py`: `recall_learnings`.
- `agent_tools/registry.py`: assembles `TOOL_REGISTRY`.

Keep `import trader.agent_tools as agent_tools` working by exporting the current
public names from `agent_tools/__init__.py`.

## 9. Read Models and UI

Create `trader/read_models/runtime_state.py` for state-file loading and
projection assembly previously owned by the flat `trader/tui.py`.

Then move Rich builders into `trader/ui/rich_panels.py` so both the simple Rich
TUI and the Textual cockpit import public UI builders instead of private
functions from `tui.py`.

Rules:

- read models can be tolerant and best-effort;
- UI builders are pure renderers over dicts/projections;
- cockpit does not import private `_build_*` symbols from `tui.py` after the
  migration;
- `trader.tui` remains a runnable module and public compatibility layer.

Implementation note (2026-07-02): market/radar/FX/regime helpers now live under
`trader/market/`; config loaders under `trader/config/`; process helpers under
`trader/runtime/`; Textual cockpit code under `trader/cockpit/`; and TUI/palette
code under `trader/ui/`. Import compatibility is preserved for historical module
names, but `python -m` is guaranteed only for real CLI wrappers and entrypoints.

## 10. Logging Design

Use the existing logging framework:

- `trader/logging_setup.py` remains the setup entrypoint;
- `logging.getLogger(__name__)` should be the default in extracted modules;
- `casys-trader` can remain the top-level daemon logger for high-level cycle
  milestones;
- all `trader.*` module loggers propagate to the configured `trader` logger.

Add a small helper module `trader/application/runtime_logging.py` only if it
keeps call sites clearer. It should provide convenience functions for structured
event logs without hiding the standard logger.

Logging expectations:

- cycle start/end: symbols due, tradable count, stale count, calls used, elapsed;
- market snapshot: fetch counts, stale reasons summary, daily coverage, FX
  fallback events;
- planner batch: chunk count, skipped budget symbols, model failure class,
  tool round outcomes;
- decision recorder: persisted decision id or sequence, source, executed flag,
  reason;
- order admission: validation block reason, risk gate code, fill summary,
  trade-plan creation/update;
- read-model/UI loading: only debug-level logs for missing optional files, to
  avoid noisy cockpit output.

Use machine-readable fragments where useful, but do not turn every line into
raw JSON. Durable truth remains in `state/*.jsonl`; logs are operational
telemetry.

## 10.1 Library Policy

Architecture-support libraries are allowed, but only behind small project-owned
interfaces.

Candidates that may be useful:

- `pydantic` or `attrs` for boundary DTOs if dataclasses become too weak for
  parsing external payloads. Use only at boundaries; do not infect pure domain
  functions with framework requirements.
- `dependency-injector` only if constructor injection becomes repetitive after
  the first two application slices. Prefer plain dataclasses and explicit
  factory functions first.
- `structlog` only if operational logs need consistent key-value rendering that
  the current `logging` formatter makes painful. It must integrate with
  `logging_setup.py`.
- `loguru` only if there is a concrete deficiency in standard logging/RichHandler
  that cannot be solved by a small formatter/helper.

Default choice remains standard library `dataclasses`, `typing.Protocol`, and
`logging` until a task proves a library would reduce real complexity.

## 11. Migration Strategy

Migration must be branch-safe and test-first.

Order:

1. Add the architecture spec and implementation plan.
2. Extract decision recording with compatibility tests around ledger/report
   shape.
3. Extract planner batch with tests for budgets, tool loop, and
   `REQUEST_CONTEXT`.
4. Extract market snapshot with tests for stale handling, daily planning, 5m
   exit bars, FX fallback, and source capture.
5. Split agent protocol internals while keeping `codex_client` facade tests
   green.
6. Split agent tools into a package while keeping `trader.agent_tools` imports
   green.
7. Extract read models and UI builders.
8. Extract order admission once the daemon body is small enough to make the
   move safe.
9. Update `docs/architecture.md` to describe the final module map and live
   dataflow.

Each tranche should end with:

- targeted tests for the moved behavior;
- `uv run ruff check trader tests`;
- `uv run pytest -q`;
- a focused commit.

## 12. Acceptance Criteria

- `trader/daemon.py` remains the runtime entrypoint but no longer contains the
  bulk of decision recording, planner batching, market snapshot building, and
  pure order-admission helper logic. Full order-admission orchestration remains
  in `daemon.py` for this tranche and is documented as the remaining seam.
- Existing CLI commands and imports still work. Alias-only compatibility modules
  preserve imports, not arbitrary `python -m trader.<old_name>` execution.
- Existing state files keep their current schemas unless a later explicit
  migration document says otherwise.
- `state/decisions.jsonl` remains the audit source of truth.
- `RiskGate` remains on every live order path.
- The runtime LLM still runs without shell or terminal access.
- `trader/logging_setup.py` remains the logging setup path and covers extracted
  modules.
- No new logging dependency is added unless a specific implementation step
  records why standard logging is insufficient.
- Full test suite and ruff pass at the end of each committed tranche.

## 13. Risks and Controls

- **Risk: behavior drift in live trading.** Control: move one cohesive slice at
  a time, keep compatibility wrappers, assert ledger/report output shape.
- **Risk: import churn breaks tests without improving readability.** Control:
  extract only modules that remove real responsibility from large files.
- **Risk: logs become noisy and hide important runtime events.** Control: INFO
  for cycle and decisions, WARNING for degradation, DEBUG for optional read-model
  misses.
- **Risk: new architecture vocabulary obscures the domain.** Control: module
  names must describe trading/runtime concepts, not generic enterprise layers
  alone.
- **Risk: ignored live config makes worktrees fail tests.** Control: copy
  ignored `config/universe.yaml` into refactor worktrees for baseline tests, and
  consider a later test fixture cleanup outside this refactor if needed.

## 14. Open Decisions Resolved

- Framework choice: modular monolith with light hexagonal boundaries.
- Logging choice: existing standard-library logging setup first; no `loguru` by
  default.
- Source of truth: `state/decisions.jsonl` and current state artifacts remain
  canonical.
- Worktree: the approved refactor has been integrated directly on `main` after
  user approval.
