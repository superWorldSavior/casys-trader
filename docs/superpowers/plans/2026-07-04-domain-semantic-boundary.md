# Domain Semantic Boundary Implementation Plan

> **For agentic workers:** This tranche is delivered. Keep semantic catalog
> moves protected by layout, legacy import, and vocabulary-prompt tests.

**Goal:** Stop exposing the governed semantic catalog as a top-level
architecture package. It is domain vocabulary, so it belongs under
`trader.domain`.

**Architecture:** `trader.domain.semantic.catalog` owns indicator IDs, label
values, families, and temporal query normalization. Historical imports through
`trader.semantic.catalog` remain virtual compatibility aliases.

**Status 2026-07-04:** Delivered in `refactor/domain-semantic-boundary`.

---

### Task 1: Layout Guardrails

**Files:**
- Modify: `tests/test_package_layout.py`

- [x] Remove `semantic` from the declared top-level package set.
- [x] Require `trader/domain/semantic/catalog.py`.
- [x] Prove `trader.semantic` and `trader.semantic.catalog` are virtual aliases.
- [x] Forbid internal imports from `trader.semantic`.

### Task 2: Move The Catalog

**Files:**
- Move: `trader/semantic/` -> `trader/domain/semantic/`
- Modify: `trader/__init__.py`
- Modify: production imports under `trader/` and `scripts/`

- [x] Move the package with `git mv`.
- [x] Add virtual compatibility for `trader.semantic.catalog`.
- [x] Rewrite production imports to `trader.domain.semantic.catalog`.

### Task 3: Docs And Verification

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/README.md`
- Modify: `docs/reference/semantic.md`
- Modify: `docs/decisions/registre-decisions-metier.md`

- [x] Update current docs to describe `domain/semantic`.
- [x] Run package-layout and semantic/prompt/config focused tests.
- [x] Run Ruff and `git diff --check`.
