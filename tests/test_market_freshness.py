from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from trader.market import market_data as market
from trader.market.market_data import Bar, assess_freshness


def _bar(ts: str, close: float = 100.0) -> Bar:
    return Bar(ts=ts, open=close, high=close, low=close, close=close, volume=1.0)


def test_barre_recente_est_fraiche() -> None:
    now = datetime(2026, 6, 5, 16, 0, tzinfo=timezone.utc)
    bars = [_bar((now - timedelta(minutes=10)).isoformat())]
    verdict = assess_freshness(bars, now=now, max_age_minutes=40)
    assert verdict.fresh is True
    assert verdict.reason is None
    assert round(verdict.age_minutes, 1) == 10.0


def test_barre_trop_vieille_est_stale() -> None:
    now = datetime(2026, 6, 6, 2, 0, tzinfo=timezone.utc)
    bars = [_bar((now - timedelta(hours=6)).isoformat())]
    verdict = assess_freshness(bars, now=now, max_age_minutes=40)
    assert verdict.fresh is False
    assert verdict.reason == "too_old"
    assert round(verdict.age_minutes, 1) == 360.0


def test_absence_de_barres_est_stale() -> None:
    now = datetime(2026, 6, 5, 16, 0, tzinfo=timezone.utc)
    verdict = assess_freshness([], now=now, max_age_minutes=40)
    assert verdict.fresh is False
    assert verdict.reason == "no_data"
    assert verdict.age_minutes is None


def test_ts_imparsable_est_stale_fail_safe() -> None:
    now = datetime(2026, 6, 5, 16, 0, tzinfo=timezone.utc)
    verdict = assess_freshness([_bar("t1")], now=now, max_age_minutes=40)
    assert verdict.fresh is False
    assert verdict.reason == "unparseable_ts"
    assert verdict.age_minutes is None


def test_ts_naif_traite_comme_utc() -> None:
    now = datetime(2026, 6, 5, 16, 0, tzinfo=timezone.utc)
    # ts sans offset -> interprété UTC, age = 5 min
    bars = [_bar("2026-06-05T15:55:00")]
    verdict = assess_freshness(bars, now=now, max_age_minutes=40)
    assert verdict.fresh is True
    assert round(verdict.age_minutes, 1) == 5.0


def test_ts_dans_le_futur_est_stale_fail_safe() -> None:
    now = datetime(2026, 6, 5, 16, 0, tzinfo=timezone.utc)
    # barre datée 2h dans le futur => donnée temporellement invalide, ne pas trader
    bars = [_bar((now + timedelta(hours=2)).isoformat())]
    verdict = assess_freshness(bars, now=now, max_age_minutes=40)
    assert verdict.fresh is False
    assert verdict.reason == "future_ts"


def test_petit_skew_horloge_reste_frais() -> None:
    now = datetime(2026, 6, 5, 16, 0, tzinfo=timezone.utc)
    # 1 min dans le futur => toléré (skew d'horloge), reste frais
    bars = [_bar((now + timedelta(minutes=1)).isoformat())]
    verdict = assess_freshness(bars, now=now, max_age_minutes=40)
    assert verdict.fresh is True


def test_ts_dans_un_autre_fuseau_compare_en_utc() -> None:
    now = datetime(2026, 6, 5, 20, 5, tzinfo=timezone.utc)
    # 16:00-04:00 == 20:00 UTC -> age 5 min malgré l'offset
    bars = [_bar("2026-06-05T16:00:00-04:00")]
    verdict = assess_freshness(bars, now=now, max_age_minutes=40)
    assert verdict.fresh is True
    assert round(verdict.age_minutes, 1) == 5.0


def test_freshness_budget_minutes_ajoute_la_marge_a_la_duree_de_barre() -> None:
    assert hasattr(market, "freshness_budget_minutes")
    assert market.freshness_budget_minutes("1h") == 75.0
    assert market.freshness_budget_minutes("15m") == 30.0
    assert market.freshness_budget_minutes("1d") == 1455.0
    assert market.freshness_budget_minutes("inconnu") == 75.0


def test_budget_horaire_garde_fraiche_une_barre_en_cours_de_59_minutes() -> None:
    now = datetime(2026, 6, 5, 8, 59, tzinfo=timezone.utc)
    bars = [
        _bar((now - timedelta(minutes=179)).isoformat()),
        _bar((now - timedelta(minutes=119)).isoformat()),
        _bar((now - timedelta(minutes=59)).isoformat()),
    ]

    verdict_ancien_budget = assess_freshness(bars, now=now, max_age_minutes=40)
    verdict_budget_horaire = assess_freshness(
        bars,
        now=now,
        max_age_minutes=market.freshness_budget_minutes("1h"),
    )

    assert verdict_ancien_budget.fresh is False
    assert verdict_ancien_budget.reason == "too_old"
    assert verdict_budget_horaire.fresh is True
    assert verdict_budget_horaire.reason is None


def test_budget_horaire_signale_stale_au_dela_de_75_minutes() -> None:
    now = datetime(2026, 6, 5, 9, 20, tzinfo=timezone.utc)
    bars = [
        _bar((now - timedelta(minutes=200)).isoformat()),
        _bar((now - timedelta(minutes=140)).isoformat()),
        _bar((now - timedelta(minutes=80)).isoformat()),
    ]

    verdict = assess_freshness(
        bars,
        now=now,
        max_age_minutes=market.freshness_budget_minutes("1h"),
    )

    assert verdict.fresh is False
    assert verdict.reason == "too_old"


def test_session_snapshot_suffixe_two_utilise_le_calendrier_taiwan() -> None:
    now = datetime(2026, 6, 17, 9, 32, tzinfo=ZoneInfo("Asia/Taipei"))

    snapshot = market.session_snapshot("6488.TWO", now=now)

    assert snapshot == {"open": True, "since_open_m": 32, "to_close_m": 238}
