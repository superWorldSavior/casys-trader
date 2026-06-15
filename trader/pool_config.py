"""Pool Tier-1 et exclusions dures, source explicite du radar."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


class PoolConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Pool:
    symbols: tuple[str, ...]
    hard_exclusions: frozenset[str]


def load_pool(config_dir: Path) -> Pool:
    """Charge pool.yaml, deduplique les symboles et retire les exclusions dures."""
    path = config_dir / "pool.yaml"
    if not path.exists():
        raise PoolConfigError(f"pool_yaml_missing: {path}")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = [str(symbol) for symbol in (cfg.get("symbols") or [])]
    hard_exclusions = frozenset(
        str(symbol) for symbol in (cfg.get("hard_exclusions") or [])
    )
    symbols = tuple(
        symbol for symbol in dict.fromkeys(raw) if symbol not in hard_exclusions
    )
    if not symbols:
        raise PoolConfigError("pool_empty_after_exclusions")
    return Pool(symbols=symbols, hard_exclusions=hard_exclusions)
