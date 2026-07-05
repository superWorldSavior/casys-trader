# Execute Queue Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move execute-order queue producer/collector glue out of
`trader/runtime/daemon.py` without changing queue-on behavior.

**Architecture:** `trader.application.execute_queue_dispatch` owns the
`execute_order` task payload, dedup key, queue enqueue, polling, fill decoding and
fail-closed outcome mapping. `daemon.py` remains the composition root: it
pre-computes plan payloads, calls the dispatcher, records decisions, and applies
post-fill runtime effects.

**Tech Stack:** Python, pytest, Ruff, package-layout guardrails.

---

### Task 1: Guardrails

**Files:**
- Create: `tests/application/test_execute_queue_dispatch.py`
- Modify: `tests/test_package_layout.py`

- [x] Add tests for `done`, `dead`, `not_found`, `timeout`, `no_fill`,
      `dry_run` and `enqueue_failed` outcomes.
- [x] Add an AST guard proving `daemon.py` no longer calls
      `execute_ledger.enqueue(...)` inline.
- [x] Run the new guardrails and verify they fail before implementation.

### Task 2: Extract Dispatcher

**Files:**
- Create: `trader/application/execute_queue_dispatch.py`
- Modify: `trader/runtime/daemon.py`

- [x] Move execute queue payload construction, stable dedup key, enqueue,
      polling and fill decoding to `execute_queue_dispatch`.
- [x] Preserve fail-closed reason strings:
      `queue_execute_enqueue_failed`, `queue_execute_dead`,
      `queue_execute_not_found`, `queue_execute_timeout`,
      `queue_execute_no_fill`.
- [x] Keep plan pre-computation and decision recording in `daemon.py`.

### Task 3: Docs And Verification

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/reference/task-queue.md`
- Modify: `docs/README.md`

- [x] Document the application dispatcher and queue-on boundary.
- [x] Run queue, execute handler, state-db UoW and package-layout tests.
- [x] Run Ruff on touched files.
- [x] Run `git diff --check`.
