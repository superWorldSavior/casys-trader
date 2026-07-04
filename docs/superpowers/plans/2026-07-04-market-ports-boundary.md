# Market Ports Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the market data source contract a small hexagonal port instead of a type embedded in the adapter/config module.

**Architecture:** `trader.market.ports` owns the `DataSource` protocol. `trader.market.data_source` keeps yfinance/composite adapters and config parsing, and re-exports `DataSource` for compatibility. Application services depend on `trader.market.ports`, while runtime composition may still build adapters from `trader.market.data_source`.

**Tech Stack:** Python 3.12, pytest, Ruff, existing compatibility-facade tests.

---

### Task 1: Boundary Tests

**Files:**
- Modify: `tests/test_package_layout.py`

- [ ] **Step 1: Write failing tests**

Add tests proving:
- `trader.market.ports.DataSource` is the canonical port.
- `trader.market.data_source.DataSource` and `trader.tools.data_source.DataSource` remain compatible aliases.
- `trader/application/` does not import `trader.market.data_source`; it should type against `trader.market.ports`.

- [ ] **Step 2: Run red test**

Run: `uv run pytest -q tests/test_package_layout.py -k "market_data_imports or market_ports or application_uses_market_ports" -vv --tb=short`

Expected: FAIL because `trader.market.ports` does not exist yet and `trader/application/market_snapshot.py` imports from `trader.market.data_source`.

### Task 2: Extract Market Port

**Files:**
- Create: `trader/market/ports.py`
- Modify: `trader/market/data_source.py`
- Modify: `trader/market/__init__.py`
- Modify: `trader/application/market_snapshot.py`

- [ ] **Step 1: Create canonical port module**

Move the `DataSource` protocol into `trader.market.ports`.

- [ ] **Step 2: Keep adapters compatible**

Import and re-export `DataSource` from `trader.market.data_source`, so legacy imports still work.

- [ ] **Step 3: Migrate application service imports**

Change `trader.application.market_snapshot` to import `DataSource` from `trader.market.ports`.

### Task 3: Docs And Verification

**Files:**
- Modify: `docs/architecture.md`

- [ ] **Step 1: Document the boundary**

Update the architecture map to mention `market/ports.py` as the market-data contract and `market/data_source.py` as adapters/config.

- [ ] **Step 2: Run targeted verification**

Run:
- `uv run pytest -q tests/test_package_layout.py tests/test_data_source.py tests/test_daemon_data_sources.py`
- `uv run ruff check trader/market/ports.py trader/market/data_source.py trader/market/__init__.py trader/application/market_snapshot.py tests/test_package_layout.py`
- `git diff --check`

### Task 4: Review And Merge

**Files:** all changed files above.

- [ ] **Step 1: Request code review**

Ask a reviewer to check the diff for compatibility, boundary-test coverage, and accidental behavior changes in the data-source adapters.

- [ ] **Step 2: Fix review feedback**

Fix Critical/Important findings, rerun verification, then commit/amend.

- [ ] **Step 3: Merge**

Fast-forward merge into `main`, then rerun the targeted verification from `main`.
