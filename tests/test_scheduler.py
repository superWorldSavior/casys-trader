import json
from datetime import datetime, timezone

from trader.tools.scheduler import Scheduler


def test_next_wake_utilise_le_defaut_global_sans_override_symbole(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    sched.set_next_wake("2026-06-05T13:00:00+00:00")

    assert sched.next_wake("SPY") == datetime(2026, 6, 5, 13, 0, tzinfo=timezone.utc)


def test_next_wake_symbole_override_le_defaut_global(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    sched.set_next_wake("2026-06-05T13:00:00+00:00")
    sched.set_symbol_next_wake("SPY", "2026-06-05T12:15:00+00:00")

    assert sched.next_wake("SPY") == datetime(2026, 6, 5, 12, 15, tzinfo=timezone.utc)
    assert sched.next_wake("QQQ") == datetime(2026, 6, 5, 13, 0, tzinfo=timezone.utc)


def test_due_symbols_combine_defaut_global_et_overrides_symboles(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    sched.set_next_wake("2026-06-05T13:00:00+00:00")
    sched.set_symbol_next_wake("SPY", "2026-06-05T12:15:00+00:00")
    sched.set_symbol_next_wake("QQQ", "2026-06-05T13:30:00+00:00")

    due = sched.due_symbols(
        ["SPY", "QQQ", "DIA"],
        now=datetime(2026, 6, 5, 13, 0, tzinfo=timezone.utc),
    )

    assert due == ["SPY", "DIA"]


def test_due_symbols_traite_les_timestamps_naifs_comme_utc(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    sched.set_next_wake("2026-06-05T13:00:00")

    due = sched.due_symbols(
        ["SPY"],
        now=datetime(2026, 6, 5, 13, 1, tzinfo=timezone.utc),
    )

    assert due == ["SPY"]


def test_seconds_until_wake_retourne_le_plus_proche_reveil_symbole(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    sched.set_next_wake("2026-06-05T13:00:00+00:00")
    sched.set_symbol_next_wake("SPY", "2026-06-05T12:15:00+00:00")

    wait = sched.seconds_until_wake(
        ["SPY", "QQQ"],
        now=datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc),
    )

    assert wait == 15 * 60


def test_indicator_watch_est_persisted_par_symbole_et_remplace_l_ancienne(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    sched.set_symbol_indicator_watch(
        "SPY",
        {
            "id": "old",
            "symbol": "SPY",
            "expires_at": "2026-06-05T13:00:00+00:00",
            "conditions": [],
        },
    )
    sched.set_symbol_indicator_watch(
        "SPY",
        {
            "id": "new",
            "symbol": "SPY",
            "expires_at": "2026-06-05T14:00:00+00:00",
            "conditions": [],
        },
    )

    watches = sched.active_indicator_watches(now=datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc))

    assert [watch["id"] for watch in watches] == ["new"]


def test_indicator_watch_expiree_est_purgee(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    sched.set_symbol_indicator_watch(
        "SPY",
        {
            "id": "watch-1",
            "symbol": "SPY",
            "expires_at": "2026-06-05T12:01:00+00:00",
            "conditions": [],
        },
    )

    watches = sched.active_indicator_watches(now=datetime(2026, 6, 5, 12, 2, tzinfo=timezone.utc))

    assert watches == []


def test_stale_streak_vaut_zero_sur_un_state_sans_champ(tmp_path) -> None:
    """Vieux scheduler.json sans stale_streaks → pas de crash, streak=0."""
    state_path = tmp_path / "scheduler.json"
    # Écrire un state ancien sans le champ stale_streaks
    state_path.write_text('{"default_next_wake": null, "symbols": {}, "indicator_watches": {}}')
    sched = Scheduler(state_path)
    assert sched.get_stale_streak("SPY") == 0


def test_stale_streak_monte_et_est_persiste(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    assert sched.get_stale_streak("SPY") == 0
    sched.set_stale_streak("SPY", 1)
    assert sched.get_stale_streak("SPY") == 1
    sched.set_stale_streak("SPY", 3)
    assert sched.get_stale_streak("SPY") == 3


def test_stale_streak_est_persiste_a_travers_un_reload(tmp_path) -> None:
    path = tmp_path / "scheduler.json"
    sched1 = Scheduler(path)
    sched1.set_stale_streak("SPY", 5)
    sched2 = Scheduler(path)  # recharge depuis disque
    assert sched2.get_stale_streak("SPY") == 5


def test_stale_streak_reset_passe_a_zero(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    sched.set_stale_streak("SPY", 3)
    sched.reset_stale_streak("SPY")
    assert sched.get_stale_streak("SPY") == 0


def test_stale_streak_independant_par_symbole(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    sched.set_stale_streak("SPY", 4)
    sched.set_stale_streak("QQQ", 1)
    assert sched.get_stale_streak("SPY") == 4
    assert sched.get_stale_streak("QQQ") == 1
    sched.reset_stale_streak("SPY")
    assert sched.get_stale_streak("SPY") == 0
    assert sched.get_stale_streak("QQQ") == 1


def test_reconcile_universe_purge_les_stale_streaks_hors_univers(tmp_path) -> None:
    """Quand l'univers rétrécit, les streaks des symboles retirés disparaissent."""
    sched = Scheduler(tmp_path / "scheduler.json")
    sched.set_stale_streak("2317.TW", 8)  # retiré de l'univers
    sched.set_stale_streak("2330.TW", 3)  # gardé

    sched.reconcile_universe(["2330.TW", "SPY"])

    assert sched.get_stale_streak("2317.TW") == 0
    assert sched.get_stale_streak("2330.TW") == 3


def test_reconcile_universe_purge_les_overrides_de_reveil_hors_univers(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    sched.set_symbol_next_wake("AAPL", "2026-06-10T20:00:00+00:00")  # retiré
    sched.set_symbol_next_wake("SPY", "2026-06-10T20:00:00+00:00")  # gardé

    sched.reconcile_universe(["SPY"])

    state = json.loads((tmp_path / "scheduler.json").read_text())
    assert set(state["symbols"]) == {"SPY"}


def test_reconcile_universe_purge_les_veilles_hors_univers(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    sched.set_symbol_indicator_watch(
        "AAPL", {"id": "w1", "symbol": "AAPL", "expires_at": "2026-06-11T00:00:00+00:00"}
    )
    sched.set_symbol_indicator_watch(
        "SPY", {"id": "w2", "symbol": "SPY", "expires_at": "2026-06-11T00:00:00+00:00"}
    )

    sched.reconcile_universe(["SPY"])

    state = json.loads((tmp_path / "scheduler.json").read_text())
    assert {w["symbol"] for w in state["indicator_watches"].values()} == {"SPY"}


def test_reconcile_universe_ne_reecrit_pas_si_rien_a_purger(tmp_path) -> None:
    """Idempotence : pas de réécriture si tout est déjà dans l'univers."""
    path = tmp_path / "scheduler.json"
    sched = Scheduler(path)
    sched.set_stale_streak("SPY", 2)
    mtime_before = path.stat().st_mtime_ns

    sched.reconcile_universe(["SPY"])

    assert path.stat().st_mtime_ns == mtime_before


def test_has_symbol_wake_distingue_reveil_agent_du_polling(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)

    assert sched.has_symbol_wake("SPY") is False  # rien demandé -> polling défaut

    sched.set_symbol_next_wake_in("SPY", minutes=60, now=now)
    assert sched.has_symbol_wake("SPY") is True  # réveil demandé (agent)

    sched.clear_symbol_next_wake("SPY")
    assert sched.has_symbol_wake("SPY") is False
