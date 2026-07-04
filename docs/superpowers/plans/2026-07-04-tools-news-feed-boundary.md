# News Feed Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the news feed collector out of `trader.tools` and make `trader.market.news_feed` the canonical home.

**Architecture:** `trader.market.news_feed` owns Yahoo/news snapshot ingestion, news item archiving, and cache state. `trader.tools.news_feed` remains a mutable compatibility facade so old imports and monkeypatches still touch the canonical module. Runtime/application imports should use `trader.market.news_feed`.

**Tech Stack:** Python modules, pytest layout-boundary tests, existing mutable facade pattern from `trader.tools.market`.

---

### Task 1: Canonical News Feed Module

**Files:**
- Modify: `tests/test_package_layout.py`
- Create: `trader/market/news_feed.py`
- Modify: `trader/tools/news_feed.py`
- Modify: `trader/market/__init__.py`

- [ ] **Step 1: Write the failing boundary test**

Add tests proving that `trader.market.news_feed` exists, legacy imports resolve to the same public objects, legacy monkeypatches mutate the canonical module, and core packages do not import `trader.tools.news_feed`.

- [ ] **Step 2: Run the boundary test in RED**

Run: `uv run pytest -q tests/test_package_layout.py -k 'news_feed or legacy_news'`
Expected: fail because `trader.market.news_feed` does not exist and runtime still imports the legacy module.

- [ ] **Step 3: Move implementation and add compatibility facade**

Move the implementation from `trader/tools/news_feed.py` to `trader/market/news_feed.py`. Replace `trader/tools/news_feed.py` with a mutable module proxy matching `trader/tools/market.py`.

- [ ] **Step 4: Run the boundary test in GREEN**

Run: `uv run pytest -q tests/test_package_layout.py -k 'news_feed or legacy_news'`
Expected: pass.

### Task 2: Runtime Imports And Docs

**Files:**
- Modify: `trader/runtime/daemon.py`
- Modify: `tests/test_news_feed.py`
- Modify: `tests/test_news_feed_wiring.py`
- Modify: `tests/test_news_items_archive.py`
- Modify: `docs/architecture.md`
- Modify: `docs/README.md`
- Modify: `docs/reference/news.md`
- Modify: `docs/reference/macro.md`

- [ ] **Step 1: Migrate imports**

Change runtime and news tests to import `trader.market.news_feed` canonically, leaving legacy import coverage only in `tests/test_package_layout.py`.

- [ ] **Step 2: Update docs**

Replace `tools/news_feed` references with `market/news_feed`, noting `trader.tools.news_feed` as a legacy compatibility facade.

- [ ] **Step 3: Verify**

Run: `uv run pytest -q tests/test_package_layout.py tests/test_news_feed.py tests/test_news_feed_wiring.py tests/test_news_items_archive.py`
Expected: pass.

Run: `uv run ruff check trader/market/news_feed.py trader/tools/news_feed.py trader/runtime/daemon.py tests/test_package_layout.py tests/test_news_feed.py tests/test_news_feed_wiring.py tests/test_news_items_archive.py`
Expected: pass.

Run: `git diff --check`
Expected: pass.
