# Agent Learnings Boundary Implementation Plan

> **For agentic workers:** This tranche is delivered. Keep learnings moves
> protected by layout, legacy import, tools-memory, and recall tests.

**Goal:** Stop exposing machine learnings/RAG as a top-level architecture
package. Learnings are agent memory: raw notes, recall store, embeddings, and
consolidation all serve the planner context and tools.

**Architecture:** `trader.agent.learnings` owns raw runtime learnings,
outcome-weighted recall storage, embeddings, and consolidation. Historical
imports through `trader.learnings.*`, `trader.learnings_store`,
`trader.embeddings`, `trader.consolidator`, and `trader.tools.memory` remain
virtual compatibility aliases.

**Status 2026-07-04:** Delivered in `refactor/agent-learnings-boundary`.

---

### Task 1: Layout Guardrails

**Files:**
- Modify: `tests/test_package_layout.py`

- [x] Remove `learnings` from the declared top-level package set.
- [x] Require `trader/agent/learnings/{raw_store,store,embeddings,consolidator}.py`.
- [x] Prove `trader.learnings.*` remains a virtual alias.
- [x] Forbid internal imports from `trader.learnings`.

### Task 2: Move The Learning Memory

**Files:**
- Move: `trader/learnings/` -> `trader/agent/learnings/`
- Modify: `trader/__init__.py`
- Modify: production imports under `trader/` and `scripts/`

- [x] Move the package with `git mv`.
- [x] Add virtual compatibility for `trader.learnings.*`.
- [x] Keep flat legacy modules and `trader.tools.memory` mapped to canonical modules.
- [x] Rewrite runtime imports to `trader.agent.learnings`.
- [x] Fix path calculations that depend on `__file__` depth.

### Task 3: Docs And Verification

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/README.md`
- Modify: `docs/reference/learnings-rag.md`

- [x] Update current docs to describe `agent/learnings`.
- [x] Run package-layout and learnings focused tests.
- [x] Run Ruff and `git diff --check`.
