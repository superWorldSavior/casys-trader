# Infrastructure Boundary Implementation Plan

> **For agentic workers:** This tranche is delivered. Keep future edits staged
> behind layout tests and compatibility checks.

**Goal:** Stop exposing technical backends as top-level `trader/` packages.
Move durable queue and SQLite state storage under a lightweight
`trader.infrastructure` boundary.

**Architecture:** `trader.infrastructure.queue` owns durable task processing.
`trader.infrastructure.state_db` owns SQLite-backed paper state and outbox work.
Historical imports `trader.queue.*` and `trader.state_db.*` remain virtual
compatibility aliases through `trader/__init__.py`.

**Status 2026-07-04:** Delivered in `refactor/infrastructure-boundary`.

---

### Task 1: Layout Guardrails

**Files:**
- Modify: `tests/test_package_layout.py`

- [x] Require `trader/infrastructure/{queue,state_db}`.
- [x] Forbid real Python sources in top-level `trader/queue` and `trader/state_db`.
- [x] Prove legacy packages are virtual and delegate to canonical modules.
- [x] Forbid new internal imports from `trader.queue` and `trader.state_db`.

### Task 2: Move Backends

**Files:**
- Move: `trader/queue/` -> `trader/infrastructure/queue/`
- Move: `trader/state_db/` -> `trader/infrastructure/state_db/`
- Create: `trader/infrastructure/__init__.py`
- Modify: `trader/__init__.py`

- [x] Move packages with `git mv`.
- [x] Add virtual aliases for legacy queue and state_db packages.
- [x] Rewrite production imports to the canonical infrastructure paths.

### Task 3: Docs And Verification

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/reference/task-queue.md`
- Modify: `docs/README.md`

- [x] Update current architecture docs and task-queue reference.
- [x] Run `uv run pytest -q tests/test_package_layout.py -vv --tb=short`.
- [x] Run queue/state_db/application focused suites.
- [x] Run `uv run ruff check trader tests/test_package_layout.py`.
- [x] Run `git diff --check`.

