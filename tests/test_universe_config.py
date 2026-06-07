import yaml

from trader.agent_context import _family_code
from trader.semantic.catalog import family_for_symbol


def test_bitcoin_dans_univers_et_famille_crypto() -> None:
    cfg = yaml.safe_load(open("config/universe.yaml"))
    assert "BTC-USD" in set(cfg["symbols"])
    assert family_for_symbol("BTC-USD") == "crypto"
    assert _family_code("crypto") == "cry"


def test_or_dans_univers_et_famille_metals() -> None:
    cfg = yaml.safe_load(open("config/universe.yaml"))
    assert "GC=F" in set(cfg["symbols"])
    assert family_for_symbol("GC=F") == "metals"
    assert _family_code("metals") == "met"


def test_universe_inclut_forex_majors_et_cac40() -> None:
    cfg = yaml.safe_load(open("config/universe.yaml"))
    symbols = set(cfg["symbols"])

    assert {
        "EURUSD=X",
        "GBPUSD=X",
        "USDJPY=X",
        "USDCHF=X",
        "USDCAD=X",
        "AUDUSD=X",
        "NZDUSD=X",
        "EURJPY=X",
        "^FCHI",
        "CL=F",
        "BZ=F",
        "NG=F",
    } <= symbols
    assert "USO" not in symbols
    assert "UNG" not in symbols
