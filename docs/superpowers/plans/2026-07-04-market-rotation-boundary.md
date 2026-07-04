# Market Rotation Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop exposing universe rotation as a top-level architecture package.

**Architecture:** `trader.market.rotation` owns radar-driven universe rotation,
venue hot-sets, schedules, override, state, and rotation ledger writes. Historical
imports through `trader.rotation.*` and flat `trader.rotation_*` modules remain
virtual compatibility aliases.

**Tech Stack:** Python package layout, importlib compatibility finder, pytest,
Ruff.

---

### Task 1: Layout Guardrails

**Files:**
- Modify: `tests/test_package_layout.py`

- [x] Remove `rotation` from the declared top-level package set.
- [x] Require `trader/market/rotation/{core,venues,schedule,wiring}.py`.
- [x] Prove `trader.rotation.*` remains a virtual alias.
- [x] Forbid internal production imports from `trader.rotation`.
- [x] Prove `python -m trader.rotation` and submodules still run.

### Task 2: Move Market Rotation

**Files:**
- Move: `trader/rotation/` -> `trader/market/rotation/`
- Modify: `trader/__init__.py`
- Modify: production imports under `trader/`

- [x] Move the package with `git mv`.
- [x] Add virtual compatibility for `trader.rotation.*`.
- [x] Keep flat legacy modules mapped to canonical modules.
- [x] Rewrite runtime, reporting, cockpit, and UI imports to `trader.market.rotation`.
- [x] Export `rotation` from `trader.market`.

### Task 3: Docs And Verification

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/README.md`
- Modify: `docs/reference/universe-rotation.md`
- Modify: `docs/reference/config.md`

- [x] Update current docs to describe `market/rotation`.
- [x] Run package-layout and rotation focused tests.
- [x] Run Ruff and `git diff --check`.
- [x] Smoke `python -m` for canonical and legacy rotation modules.
