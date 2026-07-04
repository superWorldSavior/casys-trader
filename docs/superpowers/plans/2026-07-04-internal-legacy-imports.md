# Internal Legacy Imports Cleanup Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep compatibility facades for external/historical callers while stopping repo-owned backtest and scripts code from depending on them.

**Architecture:** `backtest/` and `scripts/` should import canonical packages (`agent`, `planning`, `execution`, `ui`) directly. Existing legacy aliases under `trader.__init__`, `trader.tui`, and `trader.tools.*` remain for compatibility and are still covered by compatibility tests.

**Tech Stack:** Python 3.12, pytest, Ruff, AST-based package-layout tests.

---

### Task 1: Boundary Test

**Files:**
- Modify: `tests/test_package_layout.py`

- [x] **Step 1: Write failing test**

Add an AST scan over `backtest/` and `scripts/` rejecting imports from internal legacy facades such as `trader.codex_client`, `trader.risk`, `trader.exit_engine`, `trader.indicator_watch`, `trader.trade_plan`, `trader.tui`, and `trader.tools`.

- [x] **Step 2: Run red test**

Run: `uv run pytest -q tests/test_package_layout.py -k internal_backtest_scripts -vv --tb=short`

Expected: FAIL on the current legacy imports in backtest and scripts.

### Task 2: Migrate Imports

**Files:**
- Modify: `backtest/__main__.py`
- Modify: `backtest/engine.py`
- Modify: `backtest/plan_replay.py`
- Modify: `scripts/tui_demo.py`

- [x] **Step 1: Replace legacy aliases**

Use canonical modules:
- `trader.agent.client`
- `trader.execution.risk`
- `trader.planning.exit_engine`
- `trader.planning.indicator_watch`
- `trader.planning.trade_plan`
- `trader.ui.rich_panels`

### Task 3: Verification And Review

**Files:** all changed files above.

- [x] **Step 1: Run targeted verification**

Run:
- `uv run pytest -q tests/test_package_layout.py tests/test_backtest_engine.py tests/test_plan_replay.py`
- `uv run ruff check backtest/__main__.py backtest/engine.py backtest/plan_replay.py scripts/tui_demo.py tests/test_package_layout.py`
- `git diff --check`

- [x] **Step 2: Request code review**

Ask a reviewer to check behavior preservation and boundary-test coverage before merge.
