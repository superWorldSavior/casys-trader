# Planning Scheduling Boundary Implementation Plan

> **For agentic workers:** This tranche is delivered. Keep scheduler moves
> protected by layout, legacy import, and scheduler behavior tests.

**Goal:** Stop exposing scheduler state as a top-level architecture package.
Wake scheduling is part of planning: it stores when symbols, watches, and stale
backoff should be reconsidered by the planner.

**Architecture:** `trader.planning.scheduler` owns the JSON scheduler API and
backoff constants. Historical imports through `trader.scheduling.scheduler` and
`trader.tools.scheduler` remain virtual compatibility aliases.

**Status 2026-07-04:** Delivered in `refactor/planning-scheduling-boundary`.

---

### Task 1: Layout Guardrails

**Files:**
- Modify: `tests/test_package_layout.py`

- [x] Remove `scheduling` from the declared top-level package set.
- [x] Require `trader/planning/scheduler.py`.
- [x] Prove `trader.scheduling.scheduler` remains a virtual alias.
- [x] Forbid internal imports from `trader.scheduling`.

### Task 2: Move The Scheduler

**Files:**
- Move: `trader/scheduling/scheduler.py` -> `trader/planning/scheduler.py`
- Delete: `trader/scheduling/__init__.py`
- Modify: `trader/__init__.py`
- Modify: production imports under `trader/`

- [x] Move the module with `git mv`.
- [x] Add virtual compatibility for `trader.scheduling.scheduler`.
- [x] Keep `trader.tools.scheduler` mapped to the canonical module.
- [x] Rewrite runtime imports to `trader.planning.scheduler`.

### Task 3: Docs And Verification

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/README.md`

- [x] Update current docs to describe `planning/scheduler`.
- [x] Run package-layout and scheduler focused tests.
- [x] Run Ruff and `git diff --check`.
