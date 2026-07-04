# Decision Reason Domain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `trader.domain.decision_reason` the canonical home for shared
`decision_reason_code` vocabulary used by the LLM protocol, ledgers, and audits.

**Architecture:** Reason codes are stable domain vocabulary, not a reporting
concern. `trader.reporting.decision_reason` remains a compatibility facade for
legacy imports, while production internals import from `trader.domain`.

**Tech Stack:** Python, pytest, Ruff, package-layout guardrails.

---

### Task 1: Guardrails

**Files:**
- Modify: `tests/test_decision_reason.py`
- Modify: `tests/test_package_layout.py`

- [x] Add a compatibility test proving `trader.reporting.decision_reason`
      re-exports the canonical domain functions.
- [x] Add an AST guard preventing production internals from importing
      `decision_reason` from `trader.reporting`.
- [x] Run the new guardrails and verify they fail before implementation.

### Task 2: Move Vocabulary

**Files:**
- Create: `trader/domain/decision_reason.py`
- Modify: `trader/reporting/decision_reason.py`
- Modify: `trader/agent/protocol/prompts.py`
- Modify: `trader/agent/protocol/parsing.py`
- Modify: `trader/reporting/decision_audit.py`
- Modify: `trader/reporting/decision_ledger.py`

- [x] Move the implementation to `trader.domain.decision_reason`.
- [x] Keep `trader.reporting.decision_reason` as a thin compatibility facade.
- [x] Update production imports to use the canonical domain module.

### Task 3: Docs And Verification

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/reference/reporting.md`
- Modify: `docs/reference/llm-contract.md`
- Modify: `docs/README.md`

- [x] Document the canonical domain vocabulary and reporting compatibility path.
- [x] Run decision/protocol/reporting/package-layout tests.
- [x] Run Ruff on touched files.
- [x] Run `git diff --check`.
