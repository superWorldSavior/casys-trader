from __future__ import annotations

import ast
import importlib
from pathlib import Path


MOVED_MODULES = (
    "features",
    "regime",
    "family_regime",
    "volatility",
    "fx",
    "gross_priority",
)

FORBIDDEN_DOMAIN_IMPORT_PREFIXES = (
    "trader.market",
    "trader.application",
    "trader.runtime",
    "trader.infrastructure",
    "trader.planning",
)


def _facade_attribute_names(module: object) -> set[str]:
    return {
        name for name in dir(module)
        if not (name.startswith("__") and name.endswith("__"))
    }


def test_domain_market_modules_exist_and_market_facades_reexport_attributes() -> None:
    for module_name in MOVED_MODULES:
        domain_module = importlib.import_module(f"trader.domain.market.{module_name}")
        facade_module = importlib.import_module(f"trader.market.{module_name}")

        missing = _facade_attribute_names(domain_module) - _facade_attribute_names(facade_module)

        assert not missing, f"trader.market.{module_name} misses facade names: {sorted(missing)}"


def test_domain_market_modules_do_not_import_outer_layers() -> None:
    domain_market_dir = Path("trader/domain/market")

    for module_path in domain_market_dir.glob("*.py"):
        tree = ast.parse(module_path.read_text(), filename=str(module_path))
        for node in ast.walk(tree):
            import_name: str | None = None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    import_name = alias.name
                    assert not import_name.startswith(FORBIDDEN_DOMAIN_IMPORT_PREFIXES), (
                        f"{module_path} imports forbidden outer module {import_name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                import_name = node.module
                if import_name is not None:
                    assert not import_name.startswith(FORBIDDEN_DOMAIN_IMPORT_PREFIXES), (
                        f"{module_path} imports forbidden outer module {import_name}"
                    )
