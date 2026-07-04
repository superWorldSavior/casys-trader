from trader.market import family_regime


def test_biais_par_famille_sens_force_et_comptes() -> None:
    momentum = {
        "HO.PA": -0.6,
        "AM.PA": -0.4,
        "RHM.DE": 0.2,
        "EURUSD=X": 0.1,  # seul de sa famille avec data -> famille exclue
    }
    families = {
        "defense": ["ITA", "HO.PA", "AM.PA", "RHM.DE"],
        "forex_majors": ["EURUSD=X", "USDJPY=X"],
    }

    out = family_regime.compute_family_bias(momentum, families, min_symbols=2)

    d = out["defense"]
    assert d["dir"] == "down"
    assert d["up"] == 1 and d["down"] == 2 and d["n"] == 3
    assert d["frac"] == 0.67  # 2/3 arrondi à 2 décimales
    # cluster = 2 symboles min du même univers (décision D2)
    assert "forex_majors" not in out


def test_momentum_nul_ou_absent_ignore() -> None:
    momentum = {"USO": 0.0, "UNG": None, "XLE": -0.5}
    families = {"energy": ["XLE", "USO", "UNG"]}

    out = family_regime.compute_family_bias(momentum, families, min_symbols=2)

    # XLE seul a un momentum signé -> n=1 < min_symbols -> famille exclue
    assert out == {}


def test_famille_unanime() -> None:
    momentum = {"EURUSD=X": -0.3, "USDJPY=X": -0.2}
    families = {"forex_majors": ["EURUSD=X", "USDJPY=X"]}

    out = family_regime.compute_family_bias(momentum, families, min_symbols=2)

    fx = out["forex_majors"]
    assert fx["dir"] == "down"
    assert fx["frac"] == 1.0
    assert fx["n"] == 2


def _bar(ts: str, close: float):
    from trader.market.market_data import Bar

    return Bar(ts=ts, open=close, high=close, low=close, close=close, volume=0.0)


def test_momentum_depuis_les_barres() -> None:
    bars = [
        _bar("2026-06-10T17:00:00+00:00", 100.0),
        _bar("2026-06-10T17:15:00+00:00", 100.5),
        _bar("2026-06-10T17:30:00+00:00", 100.8),
        _bar("2026-06-10T17:45:00+00:00", 101.0),
    ]
    # close[-1] vs close[-4] : (101 - 100) / 100 = +1 %
    assert family_regime.momentum_from_bars(bars, lookback_bars=3) == 1.0


def test_momentum_depuis_les_barres_daily_lookback_3_seances() -> None:
    bars = [
        _bar("2026-06-02T00:00:00+00:00", 100.0),
        _bar("2026-06-03T00:00:00+00:00", 101.0),
        _bar("2026-06-04T00:00:00+00:00", 102.0),
        _bar("2026-06-05T00:00:00+00:00", 104.0),
    ]

    assert family_regime.momentum_from_bars(bars, lookback_bars=3) == 4.0


def test_momentum_barres_insuffisantes() -> None:
    assert family_regime.momentum_from_bars([], lookback_bars=3) is None
    assert family_regime.momentum_from_bars([_bar("t", 100.0)], lookback_bars=3) is None


def test_familles_restreintes_a_l_univers() -> None:
    fams = family_regime.families_for_universe(["EURUSD=X", "USDJPY=X", "SPY"])

    # forex_majors garde ses 2 membres présents ; indices n'a que SPY -> exclue
    assert fams["forex_majors"] == ["EURUSD=X", "USDJPY=X"]
    assert "indices" not in fams


def test_guidance_decision_presente_regime_families_comme_opportunite() -> None:
    from trader.agent import client as codex_client

    prompt = codex_client.build_batch_prompt(
        mandate="m", memory="mem", shared_context={}, symbols_payload=[]
    )
    assert "regime_families" in prompt
    # signal d'opportunité directionnelle, pas un frein de plus (décision D2)
    assert "opportunité" in prompt.lower()
