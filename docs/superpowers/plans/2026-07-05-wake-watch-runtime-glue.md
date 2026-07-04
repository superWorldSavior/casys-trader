# Wake Watch Runtime Glue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep watch/wake policy in `trader.application`, but move runtime event
and logging glue out of `trader/runtime/daemon.py`.

**Architecture:** `trader.runtime.cycle_scheduling` becomes the small runtime
adapter over `trader.application.cycle_schedule` and
`trader.application.watch_scanner`. `daemon.py` remains the composition root and
keeps private compatibility wrappers for existing tests/imports.

**Tech Stack:** Python, pytest, Ruff, package layout guardrails.

---

### Task 1: Runtime Boundary Guardrails

**Files:**
- Create: `tests/runtime/test_cycle_scheduling.py`
- Modify: `tests/test_package_layout.py`

- [x] Add tests for indicator-watch runtime event emission and expired-watch TTL events.
- [x] Add an AST guard preventing `daemon.py` from importing `cycle_schedule` or
      `watch_scanner` directly from `trader.application`.
- [x] Run the new tests and verify they fail before implementation.

### Task 2: Extract Runtime Adapter

**Files:**
- Create: `trader/runtime/cycle_scheduling.py`
- Modify: `trader/runtime/daemon.py`

- [x] Move scan/event/log glue from daemon private helpers to
      `trader.runtime.cycle_scheduling`.
- [x] Keep daemon private helpers as pass-through compatibility aliases.
- [x] Move expired indicator-watch event emission out of the daemon main loop.
- [x] Keep application modules as policy owners; runtime adapter only wires side effects.

### Task 3: Docs And Verification

**Files:**
- Modify: `docs/architecture.md`

- [x] Record the new runtime adapter in the architecture map and tranche list.
- [x] Run targeted scheduler/watch suites.
- [x] Run Ruff on touched runtime/tests files.
- [x] Run `git diff --check`.
