import yaml

from trader.agent_context import _family_code
from trader.semantic.catalog import family_for_symbol


def test_bitcoin_hors_univers_actif_mais_famille_crypto_connue() -> None:
    cfg = yaml.safe_load(open("config/universe.yaml"))
    assert "BTC-USD" not in set(cfg["symbols"])
    assert family_for_symbol("BTC-USD") == "crypto"
    assert _family_code("crypto") == "cry"


def test_or_hors_univers_calibration_mais_famille_metals_connue() -> None:
    # Futures CME retirés 2026-06-10 (data différée, calibration) — cf en-tête
    # de config/universe.yaml et docs/postmortems/2026-06-09-short-clf-hard-stop.md.
    # La famille reste connue pour la ré-expansion en prod.
    cfg = yaml.safe_load(open("config/universe.yaml"))
    assert "GC=F" not in set(cfg["symbols"])
    assert family_for_symbol("GC=F") == "metals"
    assert _family_code("metals") == "met"


def test_universe_inclut_forex_majors_et_cac40() -> None:
    cfg = yaml.safe_load(open("config/universe.yaml"))
    symbols = set(cfg["symbols"])

    # Univers calibration 2026-06-10 : 2 paires FX non redondantes, CAC40.
    # Cf. en-tête de config/universe.yaml.
    assert {
        "EURUSD=X",
        "USDJPY=X",
        "^FCHI",
    } <= symbols
    # Futures CME retirés le temps de la calibration — verrouillés absents.
    assert not symbols & {"CL=F", "NG=F", "GC=F"}
    # Doublons corrélés retirés lors du trim 2026-06-08 — verrouillés absents.
    assert not symbols & {
        "GBPUSD=X",
        "USDCHF=X",
        "USDCAD=X",
        "AUDUSD=X",
        "NZDUSD=X",
        "EURJPY=X",
        "BZ=F",
    }
    # Univers thématique 2026-06-10 (thèses Erwan) : énergie via ETF (les
    # futures CME restent exclus), défense EU, semis. Le verrou historique
    # « USO/UNG absents » datait du trim 2026-06-08 où ils doublonnaient
    # CL=F/NG=F ; les futures partis, les ETF sont le véhicule énergie.
    assert {"USO", "UNG", "HO.PA", "AM.PA", "RHM.DE"} <= symbols
    # Panier TW dégraissé : seuls les semis restent.
    assert {"2330.TW", "2454.TW"} <= symbols
    assert not symbols & {"2317.TW", "2412.TW", "2882.TW", "2603.TW"}


def test_nouveaux_symboles_ont_une_famille_semantique() -> None:
    # L'edge cross-asset (rs, sz) repose sur family_for_symbol ; un symbole
    # sans famille en est exclu silencieusement.
    cfg = yaml.safe_load(open("config/universe.yaml"))
    for sym in ["USO", "UNG", "HO.PA", "AM.PA", "RHM.DE", "2330.TW", "2454.TW"]:
        assert sym in cfg["symbols"]
        assert family_for_symbol(sym) is not None, sym
