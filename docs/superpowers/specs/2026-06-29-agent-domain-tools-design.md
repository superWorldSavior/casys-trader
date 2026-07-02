# Agent domain tools for the runtime LLM

**Date**: 2026-06-29
**Status**: Phases 1-2 implémentées (registry V0 lecture-seule + tool loop),
flag `CASYS_AGENT_TOOLS_ENABLED` éteint par défaut. Phases 3-5 (migration
REQUEST_CONTEXT, scheduling/watches, propose_order) non commencées.

---

## 1. Context

The runtime brain currently runs as a pure LLM call through `acpx`:

- terminal disabled;
- tool access disabled;
- strict JSON output parsed by `trader.codex_client`;
- live effects executed by `trader.daemon`, never directly by the model.

This is safe, but the "tools" the agent can use today are encoded as fields in
the final decision JSON:

- `REQUEST_CONTEXT` for bounded indicator research;
- `indicator_watch` and `cancel_watch_ids` for conditional wakeups and plans;
- `next_wake_in_minutes` for scheduling;
- `learning` for runtime memory;
- `action` / `quantity` / `intent` for proposed orders.

The existing design is already daemon-owned: the LLM proposes intent, then the
daemon validates schemas, freshness, sessions, exit plans, risk, and broker
submission. The new design keeps that property and turns those pseudo-tools into
a bounded domain tool layer that can replace the JSON channels step by step.

Related existing contract:

- `trader/llm.py`: `build_acpx_command()` disables terminal and tools.
- `trader/codex_client.py`: owns prompt contracts and strict parsing.
- `trader/daemon.py`: owns context construction, tool-like side effects, risk,
  and broker submission.
- `trader/tool_trace.py` and `trader/tool_usage.py`: derive audit traces from
  decision ledger rows.
- `docs/superpowers/specs/2026-06-08-agent-behavior-tool-tracing-design.md`:
  defines the current pseudo-tool tracing model.

## 2. Problem

The CLI is powerful for humans and for offline debugging, but it is too broad for
the live trading agent. Giving the runtime LLM a shell or unrestricted CLI would
mix three responsibilities that must remain separate:

1. exploration and debugging for humans;
2. bounded market context navigation for the runtime brain;
3. deterministic live execution by the daemon.

The runtime agent needs more freedom to navigate the market state, but that
freedom must be expressed through small domain tools with explicit schemas,
budgets, and audit outcomes. The goal is not to make the model an operator. The
goal is to make it a better planner.

## 3. Goals

- Add a real domain tool layer for the runtime LLM while keeping acpx system
  tools and terminal disabled.
- Replace current JSON pseudo-tools gradually instead of rewriting the live loop
  in one jump.
- Keep all live effects daemon-owned.
- Preserve deterministic gates: execution eligibility, exit-plan validation,
  hard-stop/risk checks, and `RiskGate` before broker submission.
- Make every tool call auditable from `state/decisions.jsonl` and derivable by
  `trader.tool_trace`.
- Keep compatibility with current JSON decisions during migration.

## 4. Non-goals

- No direct shell, filesystem, browser, network, broker, or CLI access for the
  runtime LLM.
- No separate MCP server for the live trading brain in the first implementation.
  MCP can be considered later for offline research or operator tooling.
- No replacement of `RiskGate`.
- No rewrite of D7B armed-plan behavior. Valid `EXECUTE_ORDER` plans continue to
  execute without a global LLM preflight unless a later D12 design explicitly
  changes that.
- No forced chain-of-thought logging. Tool traces record what was requested and
  what the daemon did, not hidden reasoning.

## 5. Recommended approach

Use a daemon-owned domain tool registry.

The LLM still receives one prompt and returns JSON. That JSON may either contain
a final decision or a bounded list of `tool_calls`. The daemon validates and
executes those domain calls, injects compact `tool_results`, and asks the LLM for
the final decision. The final decision then flows through the existing daemon
path.

Rejected alternatives:

- **Improve only the current JSON fields.** This is low risk but keeps the
  interface as pseudo-tools and makes future navigation awkward.
- **Expose CLI / shell directly.** This gives maximum flexibility but removes
  the boundary that currently keeps live trading safe and auditable.
- **Build an MCP server first.** This is attractive architecturally, but it adds
  transport surface before the domain contract is stable. The first version
  should be a local Python registry inside the daemon.

## 6. Architecture

### 6.1 New module: `trader.agent_tools`

`trader.agent_tools` owns the domain registry and pure validation/execution
contracts.

Core types:

```python
AgentToolCall = {
    "id": "tool_call_id",
    "tool": "get_freshness",
    "args": {...},
}

AgentToolResult = {
    "id": "tool_call_id",
    "tool": "get_freshness",
    "ok": true,
    "result": {...},
}

AgentToolTrace = {
    "id": "tool_call_id",
    "tool": "get_freshness",
    "args": {...},
    "outcome": "ok|rejected|error|truncated|budget_exhausted",
    "detail": {...},
}
```

The module should expose:

- `TOOL_REGISTRY`: stable names mapped to handlers and schemas.
- `validate_tool_call(raw, allowed_tools)`.
- `execute_tool_call(call, context)`.
- `execute_tool_round(calls, context, limits)`.
- helpers to serialize traces into decision rows.

Handlers receive a narrow context object constructed by the daemon for the
current cycle. They do not read global live state directly when the daemon can
pass an already-computed value.

### 6.2 Runtime loop

Initial tool loop shape:

1. Build the usual shared context and per-symbol facts.
2. Call the LLM with `allow_tool_calls=True`.
3. Parse response:
   - final decision array: continue as today;
   - `tool_calls`: validate and execute bounded domain tools.
4. Re-call the LLM with `tool_results` and `allow_tool_calls=False`.
5. Parse final decisions.
6. Apply existing scheduling, watch, exit-plan, execution, and risk paths.

First limits:

- max rounds: 1 tool round plus 1 final decision round;
- max tool calls per symbol: 3;
- max total tool calls per batch: configurable, default 24;
- max returned rows per tool: tool-specific and small;
- invalid tool call: returned as a compact error result, not a daemon crash;
- budget exhaustion: returned as `budget_exhausted`, then final decision is
  still requested.

This mirrors the existing `REQUEST_CONTEXT` two-pass loop, but generalizes the
domain contract.

### 6.3 Prompt contract

The prompt exposes a short tool catalog. It must state that tools are optional:
if the cockpit is enough, decide directly. Tool calls are for missing bounded
facts, not for exploratory browsing.

Response option A, final:

```json
{"decisions": [ ... current decision objects ... ]}
```

Response option B, tool round:

```json
{
  "tool_calls": [
    {"id": "c1", "tool": "get_freshness", "args": {"symbols": ["2330.TW"]}}
  ]
}
```

During the final round, `tool_calls` are rejected and converted to HOLD with a
reason such as `tool_loop_blocked`. This prevents unbounded loops.

## 7. Tool catalog

### 7.1 V0 read-only tools

`get_freshness`

- Args: `symbols: list[str]`.
- Returns: per-symbol `execution`, `planning`, `session.open`, `data_age_m`,
  stale reason, daily availability, next regular session open.
- Source: already computed execution eligibility and market session facts.
- Purpose: remove ambiguity between "can analyze" and "can execute".

`get_active_plans`

- Args: `symbol: str | null`, `limit: int | null`.
- Returns: active indicator watches, armed plans, trigger type, expiration,
  last LLM review when available.
- Source: scheduler and trade plan summaries already used in daemon context.
- Purpose: help the agent correct or cancel existing plans instead of stacking.

`get_position_risk`

- Args: `symbol: str`.
- Returns: current position, native price/currency, USD exposure, open trade
  plan if any, risk boundaries relevant to the symbol.
- Source: broker snapshot, FX rates, risk limits, portfolio context.
- Purpose: let the agent reason about reducing, closing, or sizing without
  recomputing portfolio state.

`get_attribution`

- Args: `scope: "summary|symbol|confidence|exit_reason"`, optional `symbol`.
- Returns: compact attribution already computed by `trader.attribution`.
- Source: current attribution payload.
- Purpose: make performance feedback pullable without flooding the base prompt.

`get_recent_decisions`

- Args: `symbol: str | null`, `limit: int`.
- Returns: compact rows from `state/decisions.jsonl`: ts, symbol, action,
  intent, reason, executed, reason code, selected tool usage.
- Source: decision ledger.
- Purpose: expose recent behavior and blocked attempts in a bounded way.

`get_indicator_context`

- Args: same bounded cube as current `REQUEST_CONTEXT`: symbol, indicators,
  timeframe, lookback, window, as_of.
- Returns: bounded indicator snapshot.
- Source: `resolve_indicator_requests`.
- Purpose: replace `REQUEST_CONTEXT` first, because that path already has a
  two-pass shape and tests.

### 7.2 V1 action tools, still non-broker

`set_next_wake`

- Replaces `next_wake_in_minutes`.
- Outcome: `applied|clamped|rejected`.
- Effect remains scheduler-owned by daemon.

`propose_indicator_watch`

- Replaces `indicator_watch`.
- Outcome: `created|rejected`.
- Effect remains scheduler-owned by daemon.

`cancel_watch`

- Replaces `cancel_watch_ids`.
- The daemon must preserve the existing ownership check: a symbol can only
  cancel watches owned by that symbol prefix.

`record_learning`

- Replaces `learning`.
- Same max length and consolidation behavior as today.

### 7.3 V2 broker-intent tool

`propose_order`

- Replaces final `action` / `quantity` / `intent` for order intent.
- The tool does not submit to the broker.
- The daemon converts the proposal into the existing decision path, then applies:
  execution eligibility, intent validation, exit-plan validation, hard-stop risk
  checks, confidence/risk checks when enabled, `RiskGate.check`, and only then
  broker submission.

This tool is intentionally last. It should only be added after read-only and
non-broker tools are traced and stable.

## 8. Data and audit model

The single durable source remains `state/decisions.jsonl`.

Add `runtime.tool_rounds` and `runtime.tool_calls` to the decision row:

```json
{
  "runtime": {
    "tool_rounds": 1,
    "tool_calls": [
      {
        "id": "c1",
        "tool": "get_freshness",
        "args": {"symbols": ["2330.TW"]},
        "outcome": "ok",
        "detail": {"result_count": 1}
      }
    ]
  }
}
```

`trader.tool_trace.summarize_tools(row)` should derive both legacy pseudo-tool
usage and new domain tool usage. The function remains pure. Stored rows should
hold primitives, not duplicated conclusions.

`trader.tool_usage` should count:

- legacy pseudo-tools during migration;
- new domain tools;
- budget exhaustion and rejected tool calls;
- tool usage joined to forward quality when decision IDs are available.

## 9. Compatibility and rollout

Phase 1: registry and read-only handlers.

- Add `trader.agent_tools`.
- Unit-test validation, bounds, error outcomes, and compact outputs.
- No prompt change yet.

Phase 2: tool loop behind a feature flag.

- Add parser support for `tool_calls`.
- Add daemon helper for one bounded tool round.
- Keep default off until tests prove no behavior change when disabled.

Phase 3: migrate `REQUEST_CONTEXT`.

- Expose `get_indicator_context`.
- Accept both `REQUEST_CONTEXT` and `tool_calls`.
- Trace both forms through the same audit surface.

Phase 4: migrate scheduling and watches.

- Add `set_next_wake`, `propose_indicator_watch`, `cancel_watch`,
  `record_learning`.
- Keep legacy final-decision fields accepted.

Phase 5: add `propose_order`.

- Reuse the existing execution path.
- No broker direct access.
- Preserve D7B armed-plan semantics unless D12 is separately approved.

## 10. Error handling

- Unknown tool: `rejected`, reason `unknown_tool`.
- Args fail schema: `rejected`, reason `invalid_args`.
- Symbol outside batch/universe: `rejected`, reason `symbol_not_allowed`.
- Tool budget exhausted: `budget_exhausted`.
- Handler exception: `error`, compact message and no live side effect.
- Final round returns tool calls: per-symbol HOLD, reason `tool_loop_blocked`.
- LLM failure remains fail-safe HOLD as today.

No tool error should crash the live daemon loop.

## 11. Invariants

- Runtime acpx terminal and system tools remain disabled.
- The daemon is the only executor of tool effects.
- Read-only tools cannot mutate scheduler, broker, memory, or files.
- Non-broker action tools mutate only through existing daemon helpers.
- No order reaches `broker.submit` without the existing execution and risk path.
- `execution.enabled=false` blocks openings and reverse intents.
- Protective exits remain fail-open where the current daemon already allows it.
- Tool traces are machine-readable and use closed outcome enums.
- Old decision JSON remains accepted until the corresponding channel is fully
  migrated and measured.

## 12. Test strategy

Unit tests:

- registry lookup and allowlist enforcement;
- schema validation for each V0 tool;
- budget exhaustion;
- read-only handlers do not mutate state;
- compact result shapes.

Parser tests:

- batch final decision still works;
- batch tool call response parses;
- malformed global JSON still HOLDs safely;
- malformed individual tool calls become rejected tool results.

Daemon tests:

- feature flag off: behavior unchanged;
- tool round then final decision consumes bounded calls;
- final-round tool call is blocked;
- `get_indicator_context` reproduces current `REQUEST_CONTEXT` bounds;
- blocked order with non-broker tool side effects preserves watch/wake behavior
  where current behavior already does.

Trace tests:

- `summarize_tools` includes new domain tools;
- rejected/error/budget outcomes are represented;
- `tool_usage` reports legacy and new tools during migration.

Regression safety:

- tests around stale runtime but valid daily planning remain green;
- tests around session-closed order blocking remain green;
- risk-gate tests remain green.

## 13. Implementation notes

- Start with a narrow context object passed from `daemon._batch_decide` rather
  than letting tools read arbitrary files.
- Prefer pure helpers and small dataclasses over stringly dynamic plumbing.
- Keep V0 read-only tools independent from live broker submission.
- Avoid a second durable tool log unless `decisions.jsonl` proves insufficient.
- Keep the CLI as an operator/debug surface. The runtime tool layer may reuse
  CLI internals, but it should not call the CLI binary.

## 14. Open follow-ups

These are deliberately outside the first implementation plan:

- Whether to expose the same registry as an MCP server for offline research.
- Whether D12 preflight should become a tool or a separate daemon path.
- When to remove legacy JSON channels after enough live trace proves parity.

## 15. Acceptance criteria for this design

- A committed spec exists for the bounded domain tool layer.
- The first implementation plan starts with V0 read-only tools and tracing, not
  broker-intent tools.
- The plan preserves daemon-owned execution and current risk gates.
- The plan includes compatibility with current JSON contracts during migration.
- The plan includes tests for safety, traceability, and no behavior change when
  the feature flag is disabled.
