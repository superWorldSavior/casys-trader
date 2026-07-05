# Tool Usage Read Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `trader.reporting.read_models.tool_usage` the canonical projection
for tool-usage reports, while keeping legacy reporting and CLI entrypoints stable.

**Architecture:** `reporting/read_models` owns ex-post projection from persisted
state. `trader.reporting.tool_usage` remains a compatibility facade plus CLI text
rendering, and `trader.interfaces.cli.tool_usage` remains the command owner.

**Tech Stack:** Python, pytest, Ruff, package-layout guardrails.

---

### Task 1: Guardrails

**Files:**
- Modify: `tests/test_tool_usage.py`
- Modify: `tests/test_package_layout.py`

- [x] Repoint aggregation tests to `trader.reporting.read_models.tool_usage`.
- [x] Add a package-layout compatibility test proving `reporting.tool_usage`
      and `trader.tool_usage` re-export the canonical read-model projection.
- [x] Run the new guardrails and verify they fail before implementation.

### Task 2: Extract Read Model

**Files:**
- Create: `trader/reporting/read_models/tool_usage.py`
- Modify: `trader/reporting/tool_usage.py`

- [x] Move report projection, tool aggregation, risk observability and
      tool-vs-quality calculations to `reporting/read_models/tool_usage.py`.
- [x] Keep `trader.reporting.tool_usage` as a facade for legacy imports plus
      `render_cli`.
- [x] Keep `python -m trader.reporting.tool_usage` delegated to the canonical CLI.

### Task 3: Docs And Verification

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/reference/reporting.md`
- Modify: `docs/README.md`

- [x] Document the read-model projection and compatibility facade.
- [x] Run tool-usage, stats/read-model, CLI and package-layout tests.
- [x] Run Ruff on touched files.
- [x] Run `git diff --check`.
