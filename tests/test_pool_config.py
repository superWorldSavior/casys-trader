from pathlib import Path

import pytest

from trader.pool_config import PoolConfigError, load_pool
from trader.semantic.catalog import family_for_symbol


def _write(cfg_dir: Path, body: str) -> Path:
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "pool.yaml").write_text(body, encoding="utf-8")
    return cfg_dir


def test_loads_symbols_and_excludes_hard_exclusions(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        "symbols: [SPY, USO, CL=F]\nhard_exclusions: [CL=F, NG=F, GC=F]\n",
    )

    pool = load_pool(cfg)

    assert "SPY" in pool.symbols
    assert "USO" in pool.symbols
    assert "CL=F" not in pool.symbols
    assert pool.hard_exclusions == {"CL=F", "NG=F", "GC=F"}


def test_empty_pool_is_an_error(tmp_path: Path) -> None:
    cfg = _write(tmp_path, "symbols: []\nhard_exclusions: []\n")

    with pytest.raises(PoolConfigError):
        load_pool(cfg)


def test_pool_config_portes_les_invariants_de_composition() -> None:
    pool = load_pool(Path("config"))
    symbols = set(pool.symbols)

    assert {"EURUSD=X", "USDJPY=X", "^FCHI"} <= symbols
    assert {"CL=F", "NG=F", "GC=F"} <= pool.hard_exclusions
    assert not symbols & {"CL=F", "NG=F", "GC=F"}
    assert not symbols & {
        "GBPUSD=X",
        "USDCHF=X",
        "USDCAD=X",
        "AUDUSD=X",
        "2317.TW",
        "2412.TW",
        "2882.TW",
        "2603.TW",
    }
    for symbol in pool.symbols:
        assert family_for_symbol(symbol) is not None, symbol
