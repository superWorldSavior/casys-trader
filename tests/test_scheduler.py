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
