# Execution Ports Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split execution contracts and ports out of the concrete broker module while preserving legacy imports.

**Architecture:** `trader.execution.contracts` owns pure order/fill/position/commission dataclasses and aliases. `trader.execution.ports` owns `Broker` and `CommissionModel` protocols. `trader.execution.broker` remains the JSON paper broker adapter plus fee helpers, and re-exports the canonical contracts/ports for compatibility.

**Tech Stack:** Python 3.12, pytest, Ruff, existing compatibility-facade tests.

---

### Task 1: Boundary Tests

**Files:**
- Modify: `tests/test_package_layout.py`

- [ ] **Step 1: Write failing tests**

Add tests proving:
- `Order`, `Fill`, `Commission`, `Position`, and `CommissionModelName` are canonical in `trader.execution.contracts`.
- `Broker` and `CommissionModel` are canonical in `trader.execution.ports`.
- `trader.execution.broker` and `trader.tools.execution` keep compatibility aliases.
- `trader/application/`, `trader/execution/portfolio.py`, and `trader/execution/risk.py` do not import contracts/ports from `trader.execution.broker`.

- [ ] **Step 2: Run red test**

Run: `uv run pytest -q tests/test_package_layout.py -k "execution_broker_imports or execution_contracts or application_uses_execution_ports" -vv --tb=short`

Expected: FAIL because `trader.execution.contracts` and `trader.execution.ports` do not exist yet and internal modules still import from `trader.execution.broker`.

### Task 2: Extract Contracts And Ports

**Files:**
- Create: `trader/execution/contracts.py`
- Create: `trader/execution/ports.py`
- Modify: `trader/execution/broker.py`
- Modify: `trader/execution/__init__.py`

- [ ] **Step 1: Create contracts**

Move `CommissionModelName`, `Order`, `Fill`, `Commission`, and `Position` to `trader.execution.contracts`.

- [ ] **Step 2: Create ports**

Move `CommissionModel` and `Broker` protocols to `trader.execution.ports`.

- [ ] **Step 3: Re-export compatibility**

Import the canonical names back into `trader.execution.broker` so existing imports continue to work.

### Task 3: Migrate Internal Imports

**Files:**
- Modify: `trader/execution/portfolio.py`
- Modify: `trader/execution/risk.py`
- Modify: `trader/infrastructure/queue/order_handler.py`
- Modify: `trader/state_db/unit_of_work.py`
- Modify: `trader/state_db/broker_store.py`
- Modify: `trader/runtime/daemon.py`
- Modify: `backtest/engine.py`
- Modify: `backtest/__main__.py`

- [ ] **Step 1: Use contracts/ports where no concrete broker is needed**

Application services and pure execution modules import `Order`/`Fill` from contracts and `Broker`/`CommissionModel` from ports.

- [ ] **Step 2: Keep composition/adapters concrete**

Runtime composition, state-db adapters, and backtest may still import concrete adapters or fee helpers from `trader.execution.broker`.

### Task 4: Docs And Verification

**Files:**
- Modify: `docs/architecture.md`

- [ ] **Step 1: Document the boundary**

Update the architecture map to mention execution contracts/ports separately from broker adapters.

- [ ] **Step 2: Run targeted verification**

Run:
- `uv run pytest -q tests/test_package_layout.py tests/test_execution.py tests/test_portfolio.py tests/test_risk.py tests/infrastructure/queue/test_order_handler.py tests/application/test_execute_via_queue.py tests/state_db/test_sqlite_broker.py tests/state_db/test_unit_of_work.py`
- `uv run ruff check trader/execution/contracts.py trader/execution/ports.py trader/execution/broker.py trader/execution/portfolio.py trader/execution/risk.py trader/infrastructure/queue/order_handler.py trader/state_db/unit_of_work.py trader/state_db/broker_store.py trader/runtime/daemon.py backtest/engine.py backtest/__main__.py tests/test_package_layout.py`
- `git diff --check`

### Task 5: Review And Merge

**Files:** all changed files above.

- [ ] **Step 1: Request code review**

Ask a reviewer to check compatibility aliases, internal import boundaries, and absence of behavior changes in `SimBroker`/`SqliteBroker`.

- [ ] **Step 2: Fix review feedback and merge**

Fix Critical/Important findings, rerun verification, amend, fast-forward merge into `main`, then rerun targeted verification from `main`.
