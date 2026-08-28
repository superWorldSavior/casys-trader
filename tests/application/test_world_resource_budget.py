from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone

import yaml

from tests.package_layout._helpers import REPO_ROOT
from trader.domain.world_episode import canonical_sha256
from trader.domain.world_resource import (
    DEFAULT_MAX_DB_BYTES,
    DEFAULT_MIN_FREE_BYTES,
    DEFAULT_WARN_INTERVAL_SECONDS,
    REASON_DB_SIZE_EXCEEDED,
    REASON_FREE_SPACE_BELOW_RESERVE,
    REASON_PROBE_ERROR,
    REASON_WITHIN_BUDGET,
    WORLD_RESOURCE_BUDGET_SCHEMA,
    WorldResourceBudget,
    WorldResourceUsage,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 24, 4, 0, tzinfo=UTC)
CONFIG_PATH = REPO_ROOT / "config" / "world_shadow_resource_budget.yaml"
MODULE_PATH = REPO_ROOT / "trader" / "application" / "world_model" / "resource_budget.py"
PORTS_PATH = REPO_ROOT / "trader" / "application" / "world_model" / "resource_ports.py"
GiB = 1024**3


class FakeProbe:
    def __init__(self, usage: WorldResourceUsage | BaseException) -> None:
        self.usage = usage
        self.calls = 0

    def measure(self) -> WorldResourceUsage:
        self.calls += 1
        if isinstance(self.usage, BaseException):
            raise self.usage
        return self.usage


def _usage(**overrides: object) -> WorldResourceUsage:
    values: dict[str, object] = {
        "logical_bytes": 64 * 1024 * 1024,
        "on_disk_bytes": 80 * 1024 * 1024,
        "free_bytes": 7 * GiB,
        "store_exists": True,
    }
    values.update(overrides)
    return WorldResourceUsage(**values)  # type: ignore[arg-type]


def _guard(probe: FakeProbe, *, budget: WorldResourceBudget | None = None):
    from trader.application.world_model.resource_budget import WorldResourceBudgetGuard

    return WorldResourceBudgetGuard(
        budget=budget or WorldResourceBudget.conservative_defaults(),
        probe=probe,
    )


def test_committed_resource_budget_config_is_versioned_hashed_and_conservative() -> None:
    from trader.application.world_model.resource_budget import (
        WORLD_SHADOW_RESOURCE_BUDGET_CONFIG_NAME,
        load_world_shadow_resource_budget,
    )

    assert CONFIG_PATH.name == WORLD_SHADOW_RESOURCE_BUDGET_CONFIG_NAME
    payload = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    assert payload["schema_version"] == WORLD_RESOURCE_BUDGET_SCHEMA
    assert payload["authority"] == "shadow_only"
    assert payload["decision_effect"] == "none"
    assert payload["recommendation"] == "NO_GO"
    assert payload["causal_claim"] is False
    assert payload["pnl_claim"] is False
    hashed = dict(payload)
    claimed = hashed.pop("content_sha256")
    assert claimed == canonical_sha256(hashed)
    budget = load_world_shadow_resource_budget(REPO_ROOT / "config")
    assert budget.content_sha256 == claimed
    assert budget.max_db_bytes == 3 * GiB
    assert DEFAULT_MAX_DB_BYTES == 2 * GiB
    assert budget.min_free_bytes == DEFAULT_MIN_FREE_BYTES
    assert budget.warn_interval_seconds == DEFAULT_WARN_INTERVAL_SECONDS
    source = CONFIG_PATH.read_text(encoding="utf-8")
    assert "CASYS_" not in source
    assert "os.environ" not in MODULE_PATH.read_text(encoding="utf-8")


def test_missing_or_invalid_config_falls_back_to_conservative_defaults(tmp_path) -> None:
    from trader.application.world_model.resource_budget import load_world_shadow_resource_budget

    missing = load_world_shadow_resource_budget(tmp_path)
    assert missing.max_db_bytes == DEFAULT_MAX_DB_BYTES
    assert missing.content_sha256 is None
    broken = tmp_path / "world_shadow_resource_budget.yaml"
    broken.write_text("not: a: valid\n", encoding="utf-8")
    invalid = load_world_shadow_resource_budget(tmp_path)
    assert invalid.max_db_bytes == DEFAULT_MAX_DB_BYTES


def test_guard_under_budget_allows_and_probes_once() -> None:
    probe = FakeProbe(_usage())
    evaluation = _guard(probe).evaluate(now=NOW)
    assert evaluation.decision.allowed is True
    assert evaluation.decision.reason == REASON_WITHIN_BUDGET
    assert evaluation.emit_warning is False
    assert probe.calls == 1
    status = evaluation.to_status()
    assert status["status"] == "allowed"
    assert status["reason"] == REASON_WITHIN_BUDGET
    assert status["authority"] == "shadow_only"
    assert status["decision_effect"] == "none"


def test_guard_db_limit_skips_and_emits_first_warning() -> None:
    probe = FakeProbe(_usage(logical_bytes=DEFAULT_MAX_DB_BYTES))
    evaluation = _guard(probe).evaluate(now=NOW)
    assert evaluation.decision.allowed is False
    assert evaluation.decision.reason == REASON_DB_SIZE_EXCEEDED
    assert evaluation.emit_warning is True
    assert probe.calls == 1


def test_guard_free_space_limit_skips() -> None:
    probe = FakeProbe(_usage(free_bytes=DEFAULT_MIN_FREE_BYTES - 1))
    evaluation = _guard(probe).evaluate(now=NOW)
    assert evaluation.decision.allowed is False
    assert evaluation.decision.reason == REASON_FREE_SPACE_BELOW_RESERVE
    assert evaluation.emit_warning is True


def test_port_error_is_fail_safe_skip_without_raising() -> None:
    probe = FakeProbe(OSError("stat failed"))
    evaluation = _guard(probe).evaluate(now=NOW)
    assert evaluation.decision.allowed is False
    assert evaluation.decision.reason == REASON_PROBE_ERROR
    assert evaluation.decision.usage is None
    assert evaluation.emit_warning is True
    assert probe.calls == 1


def test_missing_store_probe_does_not_create_the_database(tmp_path) -> None:
    from trader.infrastructure.state_db.world_resource_probe import FilesystemWorldResourceProbe

    db_path = tmp_path / "world_model.db"
    probe = FilesystemWorldResourceProbe(db_path)
    usage = probe.measure()
    assert db_path.exists() is False
    assert usage.store_exists is False
    assert usage.logical_bytes == 0
    assert usage.on_disk_bytes == 0
    assert usage.free_bytes > 0
    evaluation = _guard(probe).evaluate(now=NOW)
    assert db_path.exists() is False
    assert list(tmp_path.iterdir()) == []
    if usage.free_bytes >= DEFAULT_MIN_FREE_BYTES:
        assert evaluation.decision.allowed is True
    else:
        assert evaluation.decision.reason == REASON_FREE_SPACE_BELOW_RESERVE


def test_rate_limited_observability_emits_at_most_once_per_interval() -> None:
    probe = FakeProbe(_usage(logical_bytes=DEFAULT_MAX_DB_BYTES))
    guard = _guard(probe)
    first = guard.evaluate(now=NOW)
    second = guard.evaluate(now=NOW + timedelta(seconds=DEFAULT_WARN_INTERVAL_SECONDS - 1))
    third = guard.evaluate(now=NOW + timedelta(seconds=DEFAULT_WARN_INTERVAL_SECONDS))
    allowed = _guard(FakeProbe(_usage()), budget=guard.budget).evaluate(now=NOW)
    assert first.emit_warning is True
    assert second.emit_warning is False
    assert third.emit_warning is True
    assert allowed.emit_warning is False
    assert probe.calls == 3


def test_resource_ports_and_budget_module_stay_application_owned() -> None:
    from trader.application.world_model.resource_ports import WorldResourceProbe

    assert WorldResourceProbe.__module__ == "trader.application.world_model.resource_ports"
    forbidden = ("trader.runtime", "trader.infrastructure", "trader.reporting")
    for path in (MODULE_PATH, PORTS_PATH):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        leaks: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if any(node.module == prefix or node.module.startswith(f"{prefix}.") for prefix in forbidden):
                    leaks.append(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if any(alias.name == prefix or alias.name.startswith(f"{prefix}.") for prefix in forbidden):
                        leaks.append(alias.name)
        assert leaks == []
        source = path.read_text(encoding="utf-8").lower()
        assert "vacuum" not in source
        assert "truncate" not in source
        assert "os.environ" not in source


def test_application_guard_never_mentions_a_trader_callback() -> None:
    import inspect

    from trader.application.world_model.resource_budget import WorldResourceBudgetGuard

    signature = inspect.signature(WorldResourceBudgetGuard.__init__)
    for name in signature.parameters:
        lowered = name.lower()
        assert "trader" not in lowered
        assert "brain" not in lowered
        assert "broker" not in lowered
        assert "universe" not in lowered
        assert "callback" not in lowered
