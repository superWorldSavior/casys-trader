# Planned Exits Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move deterministic planned-exit execution out of `trader/runtime/daemon.py`.

**Architecture:** `trader.application.planned_exits` owns evaluation of open
trade plans, exit quantity clamping, execution eligibility, broker/store
mutation, and model-performance payload emission through an injected callback.
`daemon.py` remains the composition root and keeps compatibility wrappers.

**Tech Stack:** Python, pytest, Ruff, package-layout guardrails.

---

### Task 1: Guardrails

**Files:**
- Create: `tests/application/test_planned_exits.py`
- Modify: `tests/test_package_layout.py`

- [x] Add a direct application-service test for planned take-profit execution.
- [x] Add a package-layout guard proving `daemon.py` no longer imports
      `evaluate_plan` directly.
- [x] Run these tests and verify they fail before implementation.

### Task 2: Extract Service

**Files:**
- Create: `trader/application/planned_exits.py`
- Modify: `trader/runtime/daemon.py`

- [x] Move `_plan_snapshot` to `planned_exits.plan_snapshot`.
- [x] Move `_apply_planned_exits` body to `planned_exits.apply_planned_exits`.
- [x] Inject runtime constants and `append_model_performance` from the daemon wrapper.
- [x] Keep daemon private wrappers for existing tests and monkeypatch surfaces.

### Task 3: Docs And Verification

**Files:**
- Modify: `docs/architecture.md`

- [x] Document the new service in the architecture map and tranche list.
- [x] Run exit-focused daemon/application suites.
- [x] Run Ruff on touched files.
- [x] Run `git diff --check`.
