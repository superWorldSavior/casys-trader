# Flat Compatibility Facades Implementation Plan

> **For agentic workers:** This tranche is delivered. Keep root-level package
> cleanup protected by import and `python -m` compatibility tests.

**Goal:** Remove the last physical legacy shim modules from the root of
`trader/` so the package root no longer looks like a mixed bucket.

**Architecture:** `trader/__init__.py` owns virtual compatibility for historical
flat modules. Canonical code remains in `runtime/`, `interfaces/cli/`,
`interfaces/ui/`, and `reporting/`.

**Status 2026-07-04:** Delivered in `refactor/flat-compat-facades`.

---

### Task 1: Layout Guardrails

**Files:**
- Modify: `tests/test_package_layout.py`

- [x] Require `trader/` root to contain only `__init__.py` as a Python file.
- [x] Prove `trader.attribution`, `trader.cli`, `trader.daemon`,
  `trader.stats`, `trader.tool_usage`, and `trader.tui` are virtual aliases.
- [x] Preserve import mutation behavior for `trader.daemon` and `trader.cli`.
- [x] Preserve `python -m` entrypoints for daemon, CLI, and reporting commands.

### Task 2: Virtualize The Shims

**Files:**
- Modify: `trader/__init__.py`
- Delete: `trader/attribution.py`
- Delete: `trader/cli.py`
- Delete: `trader/daemon.py`
- Delete: `trader/stats.py`
- Delete: `trader/tool_usage.py`
- Delete: `trader/tui.py`

- [x] Add flat module aliases to `_COMPAT_MODULES`.
- [x] Add extra `main` attributes for reporting command facades.
- [x] Delete the physical compatibility files.

### Task 3: Docs And Verification

**Files:**
- Modify: `docs/architecture.md`
- Modify: `docs/reference/reporting.md`
- Modify: `docs/README.md`

- [x] Update docs to describe virtual flat compatibility rather than physical
  root shims.
- [x] Run layout and compatibility tests.
- [x] Run TUI/reporting/runtime focused tests.
- [x] Run Ruff and `git diff --check`.
