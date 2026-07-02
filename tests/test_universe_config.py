import yaml

from trader.agent.context import _family_code
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

