"""Classification execution/planning par symbole (design §5.1, §13.2).

Sépare deux capacités aujourd'hui mélangées par le verrou stale :
- planning : analyser / poser une veille (autorisé si daily/swing context valide) ;
- execution : passer un ordre (exige prix runtime frais + session tradable + non stale).
"""

from trader.tools import market


def test_runtime_stale_mais_daily_frais_autorise_planning_pas_execution():
    """Cœur swing : marché fermé / runtime stale mais daily frais → le LLM peut
    analyser et planifier, jamais exécuter."""
    ctx = market.classify_symbol_context(
        runtime_interval="15m",
        has_runtime_price=False,
        is_runtime_stale=True,
        session_open=False,
        daily_fresh=True,
        last_runtime_bar_ts="2026-06-16T13:15:00+08:00",
        data_age_minutes=1186.46,
        daily_as_of="2026-06-16",
        next_session_open="2026-06-17T09:00:00+08:00",
    )

    assert ctx["execution"]["enabled"] is False
    assert ctx["execution"]["reason"] == "session_closed"
    assert ctx["execution"]["runtime_interval"] == "15m"
    assert ctx["execution"]["data_age_minutes"] == 1186.46
    assert ctx["planning"]["enabled"] is True
    assert ctx["planning"]["reason"] == "daily_context_fresh"
    assert ctx["planning"]["next_session_open"] == "2026-06-17T09:00:00+08:00"


def test_symbole_pleinement_tradable_active_execution_et_planning():
    ctx = market.classify_symbol_context(
        runtime_interval="15m",
        has_runtime_price=True,
        is_runtime_stale=False,
        session_open=True,
        daily_fresh=True,
        data_age_minutes=3.0,
    )
    assert ctx["execution"]["enabled"] is True
    assert ctx["execution"]["reason"] == "tradable"
    assert ctx["planning"]["enabled"] is True


def test_stale_en_seance_ouverte_donne_reason_runtime_stale():
    """Séance ouverte mais barres intraday en retard (lag yfinance) → execution off
    avec la raison spécifique runtime_stale (pas session_closed)."""
    ctx = market.classify_symbol_context(
        runtime_interval="15m",
        has_runtime_price=False,
        is_runtime_stale=True,
        session_open=True,
        daily_fresh=True,
        data_age_minutes=80.0,
    )
    assert ctx["execution"]["enabled"] is False
    assert ctx["execution"]["reason"] == "runtime_stale"


def test_session_ouverte_non_stale_sans_prix_donne_no_price():
    ctx = market.classify_symbol_context(
        runtime_interval="15m",
        has_runtime_price=False,
        is_runtime_stale=False,
        session_open=True,
        daily_fresh=True,
    )
    assert ctx["execution"]["enabled"] is False
    assert ctx["execution"]["reason"] == "no_price"


def test_daily_stale_desactive_le_planning():
    ctx = market.classify_symbol_context(
        runtime_interval="15m",
        has_runtime_price=True,
        is_runtime_stale=False,
        session_open=True,
        daily_fresh=False,
    )
    assert ctx["planning"]["enabled"] is False
    assert ctx["planning"]["reason"] == "daily_stale"
    # execution reste possible même si le planning daily est périmé
    assert ctx["execution"]["enabled"] is True
