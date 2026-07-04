# Portfolio Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move portfolio snapshot/projection code out of `trader.tools` and make `trader.execution.portfolio` canonical.

**Architecture:** Portfolio snapshots depend on broker positions/cash and pricing inputs, so they belong with execution projections rather than reporting or market data. `trader.tools.portfolio` remains a mutable compatibility facade. Runtime imports should use `trader.execution.portfolio`.

**Tech Stack:** Python modules, pytest boundary tests, existing mutable facade pattern from `trader.tools.market`.

---

### Task 1: Canonical Portfolio Module

**Files:**
- Modify: `tests/test_package_layout.py`
- Create: `trader/execution/portfolio.py`
- Modify: `trader/tools/portfolio.py`
- Modify: `trader/execution/__init__.py`

- [ ] **Step 1: Write failing boundary tests**

Add tests proving that `trader.execution.portfolio` exists, legacy imports resolve to the same public objects, legacy monkeypatches mutate the canonical module, and execution/runtime code does not import `trader.tools.portfolio`.

- [ ] **Step 2: Run RED**

Run: `uv run pytest -q tests/test_package_layout.py -k 'portfolio'`
Expected: fail because `trader.execution.portfolio` does not exist and runtime/tests still use the legacy module.

- [ ] **Step 3: Move implementation and add compatibility facade**

Move `trader/tools/portfolio.py` to `trader/execution/portfolio.py`, update imports inside the moved file to use `trader.execution.broker`, and replace `trader/tools/portfolio.py` with a mutable proxy.

- [ ] **Step 4: Run GREEN**

Run: `uv run pytest -q tests/test_package_layout.py -k 'portfolio'`
Expected: pass.

### Task 2: Runtime Imports, Docs, And Leveling

**Files:**
- Modify: `trader/runtime/daemon.py`
- Modify: `tests/test_portfolio.py`
- Modify: `docs/architecture.md`
- Modify: `docs/README.md`
- Modify: `docs/reference/execution.md`

- [ ] **Step 1: Migrate canonical imports**

Change runtime and portfolio tests to import `trader.execution.portfolio`. Keep legacy import coverage only in `tests/test_package_layout.py`.

- [ ] **Step 2: Update docs and architecture levels**

Replace active docs references to `tools/portfolio` with `execution/portfolio`. Add a short architecture-level note explaining that top-level packages are not all the same architectural level: compatibility facades, runtime composition, application services, capability packages, domain primitives, and reporting/read models.

- [ ] **Step 3: Verify**

Run: `uv run pytest -q tests/test_package_layout.py tests/test_portfolio.py`
Expected: pass.

Run: `uv run ruff check trader/execution/portfolio.py trader/tools/portfolio.py trader/runtime/daemon.py tests/test_package_layout.py tests/test_portfolio.py`
Expected: pass.

Run: `git diff --check`
Expected: pass.
