import ast

from tests.package_layout._helpers import (
    REPO_ROOT,
    _has_python_sources,
)


def test_agent_learnings_are_nested_under_agent() -> None:
    trader_dir = REPO_ROOT / "trader"
    learnings_dir = trader_dir / "agent" / "learnings"

    assert learnings_dir.exists()
    assert (learnings_dir / "raw_store.py").exists()
    assert (learnings_dir / "store.py").exists()
    assert (learnings_dir / "embeddings.py").exists()
    assert (learnings_dir / "consolidator.py").exists()
    assert (learnings_dir / "recall_provider.py").exists()
    assert not (trader_dir / "application" / "decide" / "learnings_recall.py").exists()
    assert not _has_python_sources(trader_dir / "learnings")



def test_legacy_agent_packages_are_virtual_compatibility_layers(monkeypatch) -> None:
    trader_dir = REPO_ROOT / "trader"

    for legacy_dir in ("agent_protocol", "agent_tools"):
        assert not (trader_dir / legacy_dir).exists()

    import trader.agent_protocol as legacy_protocol
    import trader.agent_tools as legacy_tools
    from trader.domain.decisions import IndicatorRequest as DomainIndicatorRequest
    from trader.agent.protocol.parsing import parse_batch
    from trader.agent.protocol import IndicatorRequest
    from trader.agent.tools import core, registry
    from trader.agent_protocol.parsing import parse_batch as legacy_parse_batch
    from trader.agent_tools.core import ToolContext as LegacyToolContext

    assert getattr(legacy_protocol, "__file__", None) is None
    assert getattr(legacy_protocol, "__path__", None) == []
    assert getattr(legacy_tools, "__file__", None) is None
    assert getattr(legacy_tools, "__path__", None) == []
    assert IndicatorRequest is DomainIndicatorRequest
    assert legacy_parse_batch is parse_batch
    assert legacy_tools.TOOL_REGISTRY is registry.TOOL_REGISTRY
    assert LegacyToolContext is core.ToolContext

    sentinel = object()
    monkeypatch.setattr(legacy_tools.core, "_compat_probe", sentinel, raising=False)

    assert core._compat_probe is sentinel



def test_agent_package_does_not_depend_on_runtime_package() -> None:
    trader_dir = REPO_ROOT / "trader"
    agent_dir = trader_dir / "agent"

    violations: list[str] = []
    for path in sorted(agent_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel_path = path.relative_to(trader_dir.parent)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("trader.runtime"):
                violations.append(f"{rel_path}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("trader.runtime"):
                        violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []



def test_agent_memory_and_raw_learnings_imports_are_canonical_with_tools_compatibility() -> None:
    from trader.agent.memory import Memory
    from trader.agent.learnings.raw_store import LearningsStore
    from trader.agent.learnings.raw_store import RawLearningsStore
    from trader.tools.memory import LearningsStore as LegacyLearningsStore
    from trader.tools.memory import Memory as LegacyMemory
    from trader.tools.memory import RawLearningsStore as LegacyRawLearningsStore

    assert LegacyMemory is Memory
    assert LearningsStore is RawLearningsStore
    assert LegacyLearningsStore is RawLearningsStore
    assert LegacyRawLearningsStore is RawLearningsStore



def test_legacy_memory_tool_module_proxies_mutations_to_canonical_modules(monkeypatch) -> None:
    from trader.agent import memory as canonical_agent_memory
    from trader.agent.learnings import raw_store as canonical_raw_store
    from trader.tools import memory as legacy_memory

    memory_sentinel = object()
    learnings_sentinel = object()
    raw_sentinel = object()

    monkeypatch.setattr(legacy_memory, "Memory", memory_sentinel)
    monkeypatch.setattr(legacy_memory, "LearningsStore", learnings_sentinel)

    assert canonical_agent_memory.Memory is memory_sentinel
    assert canonical_raw_store.RawLearningsStore is learnings_sentinel
    assert canonical_raw_store.LearningsStore is learnings_sentinel

    monkeypatch.setattr(legacy_memory, "RawLearningsStore", raw_sentinel)

    assert canonical_raw_store.RawLearningsStore is raw_sentinel
    assert canonical_raw_store.LearningsStore is raw_sentinel



def test_runtime_agent_and_learnings_do_not_depend_on_legacy_memory_tool() -> None:
    repo_root = REPO_ROOT
    trader_dir = repo_root / "trader"
    checked_roots = (
        repo_root / "backtest",
        trader_dir / "agent",
        trader_dir / "reporting" / "read_models",
        trader_dir / "runtime",
    )
    forbidden_modules = {"trader.tools.memory"}
    forbidden_from_tools = {"memory"}

    violations: list[str] = []
    for root in checked_roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            rel_path = path.relative_to(repo_root)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in forbidden_modules:
                    violations.append(f"{rel_path}: from {node.module} import ...")
                elif isinstance(node, ast.ImportFrom) and node.module == "trader.tools":
                    forbidden_names = sorted(alias.name for alias in node.names if alias.name in forbidden_from_tools)
                    if forbidden_names:
                        violations.append(f"{rel_path}: from trader.tools import {forbidden_names}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in forbidden_modules:
                            violations.append(f"{rel_path}: import {alias.name}")

    assert violations == []

