import yaml


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
