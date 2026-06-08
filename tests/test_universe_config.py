import yaml

from trader.agent_context import _family_code
from trader.semantic.catalog import family_for_symbol


def test_bitcoin_hors_univers_actif_mais_famille_crypto_connue() -> None:
    cfg = yaml.safe_load(open("config/universe.yaml"))
    assert "BTC-USD" not in set(cfg["symbols"])
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

    # Univers resserré 2026-06-08 (28 → 15) : 2 paires FX non redondantes,
    # CAC40, énergie (WTI + gaz). Cf. en-tête de config/universe.yaml.
    assert {
        "EURUSD=X",
        "USDJPY=X",
        "^FCHI",
        "CL=F",
        "NG=F",
    } <= symbols
    # Doublons corrélés retirés lors du trim — verrouillés absents.
    assert not symbols & {
        "GBPUSD=X",
        "USDCHF=X",
        "USDCAD=X",
        "AUDUSD=X",
        "NZDUSD=X",
        "EURJPY=X",
        "BZ=F",
    }
    assert "USO" not in symbols
    assert "UNG" not in symbols
