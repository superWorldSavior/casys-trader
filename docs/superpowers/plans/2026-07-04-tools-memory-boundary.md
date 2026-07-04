# Tools Memory Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move agent prompt memory and raw runtime learnings out of `trader.tools.memory` so `tools/` becomes a compatibility layer instead of a mixed architecture bucket.

**Architecture:** `trader.agent.memory` owns mandate/memory Markdown access. `trader.learnings.raw_store` owns the bounded JSONL runtime notes as `RawLearningsStore`, with `LearningsStore` kept as a local/legacy alias. `trader.tools.memory` remains a mutable compatibility facade that routes the old public names to the canonical modules.

**Tech Stack:** Python 3.12, pytest, Ruff, existing mutable compatibility-facade pattern.

---

### Task 1: Architecture Boundary Tests

**Files:**
- Modify: `tests/test_package_layout.py`

- [ ] **Step 1: Write failing tests**

Add tests proving:
- `trader.agent.memory.Memory` is canonical.
- `trader.learnings.raw_store.RawLearningsStore` is canonical.
- `trader.tools.memory.Memory` and `trader.tools.memory.LearningsStore` remain compatible aliases.
- internal packages no longer import `trader.tools.memory`.

- [ ] **Step 2: Run red test**

Run: `uv run pytest -q tests/test_package_layout.py -k "memory or learnings" -vv --tb=short`

Expected: FAIL because `trader.agent.memory` / `trader.learnings.raw_store` do not exist yet and legacy imports remain in runtime/learnings/read-models.

### Task 2: Canonical Modules And Compatibility Facade

**Files:**
- Create: `trader/agent/memory.py`
- Create: `trader/learnings/raw_store.py`
- Modify: `trader/tools/memory.py`
- Modify: `trader/agent/__init__.py`
- Modify: `trader/learnings/__init__.py`

- [ ] **Step 1: Move code**

Move `Memory` to `trader.agent.memory`.
Move the JSONL store to `trader.learnings.raw_store.RawLearningsStore`, and expose `LearningsStore = RawLearningsStore`.

- [ ] **Step 2: Keep legacy imports mutable**

Replace `trader.tools.memory` with a facade that routes `Memory` to `trader.agent.memory` and `LearningsStore`/`RawLearningsStore` to `trader.learnings.raw_store`.

### Task 3: Import Migration And Docs

**Files:**
- Modify: `trader/runtime/daemon.py`
- Modify: `trader/learnings/consolidator.py`
- Modify: `trader/read_models/runtime_state.py`
- Modify: `tests/test_learnings.py`
- Modify: `tests/test_consolidator.py`
- Modify: `tests/test_daemon_confidence_gate.py`
- Modify: `tests/test_daemon_learnings.py`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/superpowers/specs/2026-07-02-agent-data-lifecycle.md`

- [ ] **Step 1: Migrate internal imports**

Runtime imports `trader.agent.memory` and `trader.learnings.raw_store`.
Learnings consolidation and read models import `RawLearningsStore` from the new canonical module.

- [ ] **Step 2: Update docs**

Architecture docs and tree snippets should describe `tools/` as compatibility facades and place prompt memory/raw learnings under `agent/` and `learnings/`.

### Task 4: Verification And Merge

**Files:** all changed files above.

- [ ] **Step 1: Run targeted tests**

Run: `uv run pytest -q tests/test_package_layout.py tests/test_learnings.py tests/test_consolidator.py tests/test_daemon_confidence_gate.py tests/test_daemon_learnings.py`

- [ ] **Step 2: Run lint and diff checks**

Run Ruff on the changed Python files, then `git diff --check`.

- [ ] **Step 3: Commit and fast-forward merge**

Commit on `refactor/tools-boundaries`, fast-forward merge into `main`, and repeat the targeted verification from `main`.
