# Tools Market Data Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move market-data primitives and data-source adapters out of `trader/tools/` into `trader/market/`, while preserving legacy imports.

**Architecture:** `trader.market.market_data`, `trader.market.data_source`, and `trader.market.ib_source` become the canonical modules. `trader.tools.market`, `trader.tools.data_source`, and `trader.tools.ib_source` stay as mutable compatibility facades so old monkeypatches keep touching the canonical modules. Internal runtime/application/agent imports should target the canonical modules; tests may keep legacy import coverage only where they explicitly prove compatibility.

**Tech Stack:** Python, pytest, ruff, compatibility packages.

---

### Task 1: Canonical Market Data Modules

**Files:**
- Create: `trader/market/market_data.py`
- Create: `trader/market/data_source.py`
- Create: `trader/market/ib_source.py`
- Modify: `trader/tools/market.py`
- Modify: `trader/tools/data_source.py`
- Modify: `trader/tools/ib_source.py`
- Modify: `trader/market/__init__.py`
- Test: `tests/test_package_layout.py`

- [ ] **Step 1: Write the failing package-layout tests**

Add tests that import `Bar`, `MarketError`, `Freshness`, `DataSource`, `CompositeDataSource`, `YFinanceDataSource`, `IBDataSource`, `connect_ib`, `INTERVAL_MAP`, and `LOOKBACK_MAP` from the new canonical modules and assert the old `trader.tools.*` symbols are identical compatibility aliases.

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest -q tests/test_package_layout.py -k 'market_data_imports_are_canonical or market_package_does_not_depend_on_tools_package'
```

Expected: failure because the canonical modules do not exist yet or still depend on `trader.tools`.

- [ ] **Step 3: Move the modules with compatibility facades**

Move the full implementations into `trader/market/`. Replace the old `trader/tools/` modules with mutable facades from the canonical modules.

- [ ] **Step 4: Verify GREEN**

Run the same package-layout command and expect it to pass.

### Task 2: Internal Import Migration

**Files:**
- Modify: runtime/application/agent/reporting modules that import `trader.tools.market`, `trader.tools.data_source`, or `trader.tools.ib_source`
- Modify: tests only where they are testing canonical imports rather than legacy compatibility
- Test: `tests/test_package_layout.py`

- [ ] **Step 1: Add a boundary test**

Assert `trader/application`, `trader/agent`, `trader/runtime`, `trader/reporting`, and `trader/market` no longer import `trader.tools.market`, `trader.tools.data_source`, or `trader.tools.ib_source`.

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest -q tests/test_package_layout.py -k 'core_packages_do_not_depend_on_legacy_market_tools'
```

Expected: failure listing current internal imports.

- [ ] **Step 3: Update internal imports**

Replace internal imports with `trader.market.market_data`, `trader.market.data_source`, or `trader.market.ib_source`. Keep `trader.tools.*` only in legacy tests and compatibility facades.

- [ ] **Step 4: Verify GREEN**

Run the same boundary test and expect it to pass.

### Task 3: Docs and Targeted Verification

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/reference/agent-context.md` or other docs only if they mention the old canonical path

- [ ] **Step 1: Update architecture docs**

Document `trader/market/` as the canonical home for market data, data-source adapters, IB source, freshness, FX, macro, radar, and regime. Document `trader.tools.market`, `trader.tools.data_source`, and `trader.tools.ib_source` as legacy compatibility facades.

- [ ] **Step 2: Run targeted tests**

Run:

```bash
uv run pytest -q tests/test_package_layout.py tests/test_market.py tests/test_market_freshness.py tests/test_daily_freshness.py tests/test_data_source.py tests/test_ib_source.py tests/test_market_context.py tests/test_market_snapshot.py tests/test_daemon_data_sources.py tests/test_daemon_ib_runtime.py tests/test_daemon_ib_attach.py
```

- [ ] **Step 3: Run lint and diff checks**

Run:

```bash
uv run ruff check trader/market trader/tools tests/test_package_layout.py
git diff --check
```
