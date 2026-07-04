"""Tests TDD — SqliteScheduler (parité JSON ↔ SQLite, shadow, factory).

Couvre :
- Parité SqliteScheduler ↔ Scheduler pour chaque famille de méthodes :
  réveils global + symbole (set/clear/next_wake/due/symbols_with_wake/has/seconds),
  stale streaks (get/set/reset, borne MAX),
  watches (set avec coexistence et remplacement, active, pop, remove, reconcile).
- Cas critiques :
  active vs pop (une expirée + une active),
  coexistence (2 EXECUTE_ORDER + 1 WAKE sur le même symbole),
  reconcile idempotent,
  shadow miroir exact après chaque mutation.
- make_scheduler : json/sqlite/bogus.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Union
from unittest.mock import patch

import pytest

from trader.state_db.connection import StateDb
from trader.state_db.migrations import import_scheduler_from_json
from trader.state_db.scheduler_store import SqliteScheduler
from trader.planning.scheduler import Scheduler, STALE_BACKOFF_MAX_STREAK

AnyScheduler = Union[Scheduler, SqliteScheduler]

_NOW = datetime(2026, 7, 3, 10, 0, 0, tzinfo=timezone.utc)
_ISO_NOW = _NOW.isoformat()  # "2026-07-03T10:00:00+00:00"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _future(minutes: float = 30.0) -> str:
    return (_NOW + timedelta(minutes=minutes)).astimezone(timezone.utc).isoformat()


def _past(minutes: float = 30.0) -> str:
    return (_NOW - timedelta(minutes=minutes)).astimezone(timezone.utc).isoformat()


def _armed_watch(watch_id: str, symbol: str, expires_at: str | None = None) -> dict:
    """Watch armée (on_trigger=EXECUTE_ORDER + order=dict)."""
    w = {
        "id": watch_id,
        "symbol": symbol,
        "on_trigger": "EXECUTE_ORDER",
        "order": {"intent": "OPEN_LONG", "qty": 100.0},
        "conditions": [],
        "created_at": _ISO_NOW,
    }
    if expires_at is not None:
        w["expires_at"] = expires_at
    return w


def _wake_watch(watch_id: str, symbol: str, expires_at: str | None = None,
                on_trigger: str = "WAKE") -> dict:
    """Watch simple (non-armée)."""
    w = {
        "id": watch_id,
        "symbol": symbol,
        "on_trigger": on_trigger,
        "conditions": [],
        "created_at": _ISO_NOW,
    }
    if expires_at is not None:
        w["expires_at"] = expires_at
    return w


# ---------------------------------------------------------------------------
# Fixtures pytest
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> StateDb:
    db = StateDb(tmp_path / "casys.db")
    import_scheduler_from_json(db, tmp_path / "scheduler.json")  # absent → tables vides
    return db


@pytest.fixture()
def sqlite_sched(db: StateDb, tmp_path: Path) -> SqliteScheduler:
    return SqliteScheduler(db, json_path=tmp_path / "scheduler.json")


@pytest.fixture()
def json_sched(tmp_path: Path) -> Scheduler:
    return Scheduler(tmp_path / "scheduler.json")


# ---------------------------------------------------------------------------
# Famille réveils globaux
# ---------------------------------------------------------------------------


class TestDefaultNextWake:
    def test_set_default_next_wake_stored(self, sqlite_sched: SqliteScheduler) -> None:
        iso = _future(60)
        sqlite_sched.set_default_next_wake(iso)
        nxt = sqlite_sched.next_wake()
        assert nxt is not None
        assert nxt == datetime.fromisoformat(iso)

    def test_set_next_wake_alias(self, sqlite_sched: SqliteScheduler) -> None:
        iso = _future(60)
        sqlite_sched.set_next_wake(iso)
        assert sqlite_sched.next_wake() is not None

    def test_set_next_wake_in(self, sqlite_sched: SqliteScheduler) -> None:
        sqlite_sched.set_next_wake_in(minutes=30, now=_NOW)
        nxt = sqlite_sched.next_wake()
        assert nxt is not None
        assert abs((nxt - _NOW).total_seconds() - 30 * 60) < 1

    def test_invalid_iso_raises(self, sqlite_sched: SqliteScheduler) -> None:
        with pytest.raises(ValueError):
            sqlite_sched.set_default_next_wake("not-a-date")

    def test_parity_with_json_set_next_wake(
        self, sqlite_sched: SqliteScheduler, json_sched: Scheduler
    ) -> None:
        iso = _future(45)
        sqlite_sched.set_next_wake(iso)
        json_sched.set_next_wake(iso)
        assert sqlite_sched.next_wake() == json_sched.next_wake()

    def test_next_wake_no_symbol_returns_default(
        self, sqlite_sched: SqliteScheduler
    ) -> None:
        sqlite_sched.set_next_wake(_future(10))
        nxt = sqlite_sched.next_wake()
        assert nxt is not None

    def test_next_wake_unknown_symbol_falls_back_to_default(
        self, sqlite_sched: SqliteScheduler
    ) -> None:
        iso = _future(10)
        sqlite_sched.set_next_wake(iso)
        # Symbole sans override → défaut global
        assert sqlite_sched.next_wake("UNKNOWN") == datetime.fromisoformat(iso)

    # FIX 1 — now naïf : attacher UTC, ne pas convertir
    def test_set_next_wake_in_naive_now_parity_with_json(
        self, sqlite_sched: SqliteScheduler, json_sched: Scheduler
    ) -> None:
        """now naïf doit être traité comme UTC (replace, pas astimezone)."""
        naive_now = datetime(2026, 7, 3, 10, 30)  # sans tzinfo
        sqlite_sched.set_next_wake_in(minutes=5, now=naive_now)
        json_sched.set_next_wake_in(minutes=5, now=naive_now)
        assert sqlite_sched.next_wake() == json_sched.next_wake()
        # Valeur attendue : 10:35 UTC, PAS une conversion depuis le fuseau local
        expected = datetime(2026, 7, 3, 10, 35, tzinfo=timezone.utc)
        assert sqlite_sched.next_wake() == expected


# ---------------------------------------------------------------------------
# Famille réveils par symbole
# ---------------------------------------------------------------------------


class TestSymbolWake:
    def test_set_and_get_symbol_next_wake(self, sqlite_sched: SqliteScheduler) -> None:
        iso = _future(20)
        sqlite_sched.set_symbol_next_wake("SPY", iso)
        assert sqlite_sched.next_wake("SPY") == datetime.fromisoformat(iso)

    def test_symbol_wake_overrides_default(self, sqlite_sched: SqliteScheduler) -> None:
        sqlite_sched.set_next_wake(_future(60))
        sqlite_sched.set_symbol_next_wake("SPY", _future(15))
        nxt = sqlite_sched.next_wake("SPY")
        assert nxt is not None
        assert abs((nxt - _NOW).total_seconds() - 15 * 60) < 1

    def test_clear_symbol_next_wake(self, sqlite_sched: SqliteScheduler) -> None:
        iso = _future(10)
        sqlite_sched.set_next_wake(iso)
        sqlite_sched.set_symbol_next_wake("SPY", _future(5))
        sqlite_sched.clear_symbol_next_wake("SPY")
        # Retourne le défaut
        assert sqlite_sched.next_wake("SPY") == datetime.fromisoformat(iso)

    def test_clear_nonexistent_symbol_is_noop(self, sqlite_sched: SqliteScheduler) -> None:
        sqlite_sched.clear_symbol_next_wake("UNKNOWN")  # pas d'erreur

    def test_set_symbol_next_wake_in(self, sqlite_sched: SqliteScheduler) -> None:
        sqlite_sched.set_symbol_next_wake_in("MSFT", minutes=20, now=_NOW)
        nxt = sqlite_sched.next_wake("MSFT")
        assert nxt is not None
        assert abs((nxt - _NOW).total_seconds() - 20 * 60) < 1

    def test_symbols_with_wake(self, sqlite_sched: SqliteScheduler) -> None:
        sqlite_sched.set_symbol_next_wake("SPY", _future(10))
        sqlite_sched.set_symbol_next_wake("QQQ", _future(20))
        assert sqlite_sched.symbols_with_wake() == {"SPY", "QQQ"}

    def test_has_symbol_wake_true_false(self, sqlite_sched: SqliteScheduler) -> None:
        sqlite_sched.set_symbol_next_wake("SPY", _future(10))
        assert sqlite_sched.has_symbol_wake("SPY") is True
        assert sqlite_sched.has_symbol_wake("QQQ") is False

    def test_due_symbols_combines_global_and_symbol(
        self, sqlite_sched: SqliteScheduler
    ) -> None:
        sqlite_sched.set_next_wake(_future(60))          # global : dans 1h
        sqlite_sched.set_symbol_next_wake("SPY", _past(5))   # SPY : passé → dû
        sqlite_sched.set_symbol_next_wake("QQQ", _future(90))  # QQQ : dans 90 min
        # DIA : pas d'override → regarde le défaut global (dans 1h, pas dû à now)
        due = sqlite_sched.due_symbols(["SPY", "QQQ", "DIA"], now=_NOW)
        assert due == ["SPY"]

    def test_due_symbols_without_timer_is_always_due(
        self, sqlite_sched: SqliteScheduler
    ) -> None:
        """Sans timer (no default_next_wake), un symbole est toujours dû."""
        due = sqlite_sched.due_symbols(["SPY"], now=_NOW)
        assert due == ["SPY"]

    def test_seconds_until_wake_global(self, sqlite_sched: SqliteScheduler) -> None:
        sqlite_sched.set_next_wake(_future(30))
        secs = sqlite_sched.seconds_until_wake(now=_NOW)
        assert abs(secs - 30 * 60) < 2

    def test_seconds_until_wake_min_across_symbols(
        self, sqlite_sched: SqliteScheduler
    ) -> None:
        sqlite_sched.set_next_wake(_future(60))
        sqlite_sched.set_symbol_next_wake("SPY", _future(15))
        # QQQ : sans override → utilise default (60 min)
        secs = sqlite_sched.seconds_until_wake(["SPY", "QQQ"], now=_NOW)
        assert abs(secs - 15 * 60) < 2

    def test_seconds_until_wake_no_timer_returns_zero(
        self, sqlite_sched: SqliteScheduler
    ) -> None:
        assert sqlite_sched.seconds_until_wake(["SPY"], now=_NOW) == 0.0

    def test_parity_with_json_symbol_wake_sequence(
        self, sqlite_sched: SqliteScheduler, json_sched: Scheduler
    ) -> None:
        for sched in (sqlite_sched, json_sched):
            sched.set_next_wake("2026-07-03T13:00:00+00:00")
            sched.set_symbol_next_wake("SPY", "2026-07-03T12:15:00+00:00")
            sched.set_symbol_next_wake("QQQ", "2026-07-03T13:30:00+00:00")

        now = datetime(2026, 7, 3, 13, 0, tzinfo=timezone.utc)
        assert (
            sqlite_sched.due_symbols(["SPY", "QQQ", "DIA"], now=now)
            == json_sched.due_symbols(["SPY", "QQQ", "DIA"], now=now)
        )

    # FIX 1 — now naïf sur set_symbol_next_wake_in
    def test_set_symbol_next_wake_in_naive_now_parity_with_json(
        self, sqlite_sched: SqliteScheduler, json_sched: Scheduler
    ) -> None:
        """now naïf sur set_symbol_next_wake_in : same result as Scheduler."""
        naive_now = datetime(2026, 7, 3, 10, 30)
        sqlite_sched.set_symbol_next_wake_in("SPY", minutes=5, now=naive_now)
        json_sched.set_symbol_next_wake_in("SPY", minutes=5, now=naive_now)
        assert sqlite_sched.next_wake("SPY") == json_sched.next_wake("SPY")
        expected = datetime(2026, 7, 3, 10, 35, tzinfo=timezone.utc)
        assert sqlite_sched.next_wake("SPY") == expected


# ---------------------------------------------------------------------------
# Famille stale streaks
# ---------------------------------------------------------------------------


class TestStaleStreaks:
    def test_get_stale_streak_default_zero(self, sqlite_sched: SqliteScheduler) -> None:
        assert sqlite_sched.get_stale_streak("AAPL") == 0

    def test_set_and_get_stale_streak(self, sqlite_sched: SqliteScheduler) -> None:
        sqlite_sched.set_stale_streak("AAPL", 3)
        assert sqlite_sched.get_stale_streak("AAPL") == 3

    def test_streak_capped_at_max_on_set(self, sqlite_sched: SqliteScheduler) -> None:
        sqlite_sched.set_stale_streak("AAPL", STALE_BACKOFF_MAX_STREAK + 10)
        assert sqlite_sched.get_stale_streak("AAPL") == STALE_BACKOFF_MAX_STREAK

    def test_streak_capped_at_max_on_get(self, db: StateDb, tmp_path: Path) -> None:
        """Streak en DB > MAX (state corrompu) → get retourne MAX."""
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO scheduler_stale_streaks(symbol, streak) VALUES ('SPY', ?)",
                (STALE_BACKOFF_MAX_STREAK + 99,),
            )
        sched = SqliteScheduler(db)
        assert sched.get_stale_streak("SPY") == STALE_BACKOFF_MAX_STREAK

    def test_reset_stale_streak(self, sqlite_sched: SqliteScheduler) -> None:
        sqlite_sched.set_stale_streak("AAPL", 5)
        sqlite_sched.reset_stale_streak("AAPL")
        assert sqlite_sched.get_stale_streak("AAPL") == 0

    def test_reset_nonexistent_is_noop(self, sqlite_sched: SqliteScheduler) -> None:
        sqlite_sched.reset_stale_streak("UNKNOWN")  # pas d'erreur

    def test_parity_with_json_streaks(
        self, sqlite_sched: SqliteScheduler, json_sched: Scheduler
    ) -> None:
        for sched in (sqlite_sched, json_sched):
            sched.set_stale_streak("AAPL", 4)
            sched.set_stale_streak("MSFT", 1)
            sched.reset_stale_streak("MSFT")

        assert sqlite_sched.get_stale_streak("AAPL") == json_sched.get_stale_streak("AAPL")
        assert sqlite_sched.get_stale_streak("MSFT") == json_sched.get_stale_streak("MSFT")


# ---------------------------------------------------------------------------
# Famille indicator watches — set / coexistence
# ---------------------------------------------------------------------------


class TestSetIndicatorWatch:
    def test_set_and_retrieve_wake_watch(
        self, sqlite_sched: SqliteScheduler
    ) -> None:
        w = _wake_watch("SPY:001", "SPY", _future(30))
        sqlite_sched.set_symbol_indicator_watch("SPY", w)
        active = sqlite_sched.active_indicator_watches(now=_NOW)
        assert len(active) == 1
        assert active[0]["id"] == "SPY:001"

    def test_non_armed_watch_replaces_previous_same_symbol(
        self, sqlite_sched: SqliteScheduler
    ) -> None:
        """Deux WAKE sur le même symbole : le 2e remplace le 1er."""
        w1 = _wake_watch("SPY:old", "SPY", _future(60))
        w2 = _wake_watch("SPY:new", "SPY", _future(120))
        sqlite_sched.set_symbol_indicator_watch("SPY", w1)
        sqlite_sched.set_symbol_indicator_watch("SPY", w2)

        active = sqlite_sched.active_indicator_watches(now=_NOW)
        ids = {w["id"] for w in active}
        assert "SPY:old" not in ids
        assert "SPY:new" in ids

    def test_wake_with_order_intent_replaces_previous_non_armed(
        self, sqlite_sched: SqliteScheduler
    ) -> None:
        """WAKE_WITH_ORDER_INTENT est non-armé → remplace aussi."""
        w1 = _wake_watch("SPY:wake", "SPY", _future(60), on_trigger="WAKE")
        w2 = _wake_watch("SPY:woi", "SPY", _future(60), on_trigger="WAKE_WITH_ORDER_INTENT")
        sqlite_sched.set_symbol_indicator_watch("SPY", w1)
        sqlite_sched.set_symbol_indicator_watch("SPY", w2)

        active = sqlite_sched.active_indicator_watches(now=_NOW)
        ids = {w["id"] for w in active}
        assert "SPY:wake" not in ids
        assert "SPY:woi" in ids

    def test_armed_watches_coexist_on_same_symbol(
        self, sqlite_sched: SqliteScheduler
    ) -> None:
        """Deux EXECUTE_ORDER sur le même symbole coexistent."""
        w1 = _armed_watch("SPY:arm1", "SPY", _future(240))
        w2 = _armed_watch("SPY:arm2", "SPY", _future(240))
        sqlite_sched.set_symbol_indicator_watch("SPY", w1)
        sqlite_sched.set_symbol_indicator_watch("SPY", w2)

        active = sqlite_sched.active_indicator_watches(now=_NOW)
        ids = {w["id"] for w in active}
        assert "SPY:arm1" in ids
        assert "SPY:arm2" in ids

    def test_armed_watches_and_wake_coexist(
        self, sqlite_sched: SqliteScheduler
    ) -> None:
        """2 EXECUTE_ORDER + 1 WAKE sur le même symbole : les 3 coexistent.

        (Les armées ne s'évincent pas, le WAKE remplace uniquement les WAKE précédents.)
        """
        w_arm1 = _armed_watch("SPY:arm1", "SPY", _future(240))
        w_arm2 = _armed_watch("SPY:arm2", "SPY", _future(240))
        w_wake = _wake_watch("SPY:wake", "SPY", _future(60))

        sqlite_sched.set_symbol_indicator_watch("SPY", w_arm1)
        sqlite_sched.set_symbol_indicator_watch("SPY", w_arm2)
        sqlite_sched.set_symbol_indicator_watch("SPY", w_wake)

        active = sqlite_sched.active_indicator_watches(now=_NOW)
        ids = {w["id"] for w in active}
        assert "SPY:arm1" in ids
        assert "SPY:arm2" in ids
        assert "SPY:wake" in ids

    def test_second_wake_replaces_first_armed_stay(
        self, sqlite_sched: SqliteScheduler
    ) -> None:
        """2 armées + WAKE W1, puis WAKE W2 → W2 remplace W1, armées restent."""
        w_arm1 = _armed_watch("SPY:arm1", "SPY", _future(240))
        w_arm2 = _armed_watch("SPY:arm2", "SPY", _future(240))
        w_wake1 = _wake_watch("SPY:wake1", "SPY", _future(60))
        w_wake2 = _wake_watch("SPY:wake2", "SPY", _future(90))

        for w in (w_arm1, w_arm2, w_wake1):
            sqlite_sched.set_symbol_indicator_watch("SPY", w)
        sqlite_sched.set_symbol_indicator_watch("SPY", w_wake2)

        active = sqlite_sched.active_indicator_watches(now=_NOW)
        ids = {w["id"] for w in active}
        assert "SPY:arm1" in ids
        assert "SPY:arm2" in ids
        assert "SPY:wake1" not in ids
        assert "SPY:wake2" in ids

    def test_watches_different_symbols_do_not_interfere(
        self, sqlite_sched: SqliteScheduler
    ) -> None:
        w_spy = _wake_watch("SPY:001", "SPY", _future(60))
        w_qqq = _wake_watch("QQQ:001", "QQQ", _future(60))
        sqlite_sched.set_symbol_indicator_watch("SPY", w_spy)
        sqlite_sched.set_symbol_indicator_watch("QQQ", w_qqq)

        active = sqlite_sched.active_indicator_watches(now=_NOW)
        assert len(active) == 2

    def test_parity_with_json_set_watch_replacement(
        self, sqlite_sched: SqliteScheduler, json_sched: Scheduler
    ) -> None:
        w1 = _wake_watch("SPY:old", "SPY", _future(60))
        w2 = _wake_watch("SPY:new", "SPY", _future(120))
        for sched in (sqlite_sched, json_sched):
            sched.set_symbol_indicator_watch("SPY", w1)
            sched.set_symbol_indicator_watch("SPY", w2)

        sq = {w["id"] for w in sqlite_sched.active_indicator_watches(now=_NOW)}
        js = {w["id"] for w in json_sched.active_indicator_watches(now=_NOW)}
        assert sq == js

    def test_parity_with_json_armed_coexistence(
        self, sqlite_sched: SqliteScheduler, json_sched: Scheduler
    ) -> None:
        w1 = _armed_watch("SPY:arm1", "SPY", _future(240))
        w2 = _armed_watch("SPY:arm2", "SPY", _future(240))
        for sched in (sqlite_sched, json_sched):
            sched.set_symbol_indicator_watch("SPY", w1)
            sched.set_symbol_indicator_watch("SPY", w2)

        sq = {w["id"] for w in sqlite_sched.active_indicator_watches(now=_NOW)}
        js = {w["id"] for w in json_sched.active_indicator_watches(now=_NOW)}
        assert sq == js

    # FIX 2 — upsert d'une watch ARMÉE existante ne doit PAS changer l'ordre
    def test_upsert_existing_armed_watch_preserves_order(
        self, sqlite_sched: SqliteScheduler
    ) -> None:
        """Re-upsert d'un id armé existant : la position (seq) est préservée.

        Pour les armées (EXECUTE_ORDER), la source Scheduler utilise
        `watches[id] = watch` sur une clé existante → position Python dict préservée.
        SQLite doit faire pareil : ON CONFLICT DO UPDATE sans toucher seq.
        (Pour les non-armées, elles sont toujours supprimées + réinsérées à la fin
        — les deux implémentations s'alignent donc.)
        """
        w_x = _armed_watch("SPY:X", "SPY", _future(240))
        w_y = _armed_watch("QQQ:Y", "QQQ", _future(240))
        sqlite_sched.set_symbol_indicator_watch("SPY", w_x)
        sqlite_sched.set_symbol_indicator_watch("QQQ", w_y)
        # Ré-upsert X (armée) avec expires_at modifié → doit garder sa position
        w_x_modified = _armed_watch("SPY:X", "SPY", _future(360))
        sqlite_sched.set_symbol_indicator_watch("SPY", w_x_modified)

        active = sqlite_sched.active_indicator_watches(now=_NOW)
        ids = [a["id"] for a in active]
        assert ids == ["SPY:X", "QQQ:Y"], f"ordre attendu [X, Y], obtenu {ids}"
        # Vérifier que la valeur a bien été mise à jour (watch_json reflète le nouveau dict)
        assert active[0]["expires_at"] == w_x_modified["expires_at"]


# ---------------------------------------------------------------------------
# Famille indicator watches — active vs pop_expired
# ---------------------------------------------------------------------------


class TestActiveVsPopExpired:
    """⚠️ Cas critique : active ≠ pop_expired (scheduler.py:173-211)."""

    def _setup_one_active_one_expired(self, sched: AnyScheduler) -> None:
        w_active = _wake_watch("SPY:active", "SPY", _future(30))
        w_expired = _wake_watch("MSFT:expired", "MSFT", _past(30))
        sched.set_symbol_indicator_watch("SPY", w_active)
        sched.set_symbol_indicator_watch("MSFT", w_expired)

    def test_active_returns_non_expired(self, sqlite_sched: SqliteScheduler) -> None:
        self._setup_one_active_one_expired(sqlite_sched)
        active = sqlite_sched.active_indicator_watches(now=_NOW)
        ids = {w["id"] for w in active}
        assert "SPY:active" in ids
        assert "MSFT:expired" not in ids

    def test_active_purges_expired_from_db(
        self, sqlite_sched: SqliteScheduler, db: StateDb
    ) -> None:
        self._setup_one_active_one_expired(sqlite_sched)
        sqlite_sched.active_indicator_watches(now=_NOW)
        # La watch expirée doit avoir été supprimée de la DB
        row = db.query_one("SELECT id FROM scheduler_watches WHERE id='MSFT:expired'")
        assert row is None

    def test_pop_returns_expired(self, sqlite_sched: SqliteScheduler) -> None:
        self._setup_one_active_one_expired(sqlite_sched)
        popped = sqlite_sched.pop_expired_indicator_watches(now=_NOW)
        ids = {w["id"] for w in popped}
        assert "MSFT:expired" in ids
        assert "SPY:active" not in ids

    def test_pop_deletes_expired_from_db(
        self, sqlite_sched: SqliteScheduler, db: StateDb
    ) -> None:
        self._setup_one_active_one_expired(sqlite_sched)
        sqlite_sched.pop_expired_indicator_watches(now=_NOW)
        row = db.query_one("SELECT id FROM scheduler_watches WHERE id='MSFT:expired'")
        assert row is None

    def test_pop_leaves_active_in_db(
        self, sqlite_sched: SqliteScheduler, db: StateDb
    ) -> None:
        self._setup_one_active_one_expired(sqlite_sched)
        sqlite_sched.pop_expired_indicator_watches(now=_NOW)
        row = db.query_one("SELECT id FROM scheduler_watches WHERE id='SPY:active'")
        assert row is not None

    def test_pop_is_idempotent(self, sqlite_sched: SqliteScheduler) -> None:
        self._setup_one_active_one_expired(sqlite_sched)
        popped1 = sqlite_sched.pop_expired_indicator_watches(now=_NOW)
        popped2 = sqlite_sched.pop_expired_indicator_watches(now=_NOW)
        assert len(popped1) == 1
        assert popped2 == []

    def test_active_no_expiry_never_purges(self, sqlite_sched: SqliteScheduler) -> None:
        """Watch sans expires_at (expires_at IS NULL) → toujours active."""
        w = _wake_watch("SPY:no-expiry", "SPY")  # no expires_at
        sqlite_sched.set_symbol_indicator_watch("SPY", w)

        active = sqlite_sched.active_indicator_watches(now=_NOW)
        assert any(a["id"] == "SPY:no-expiry" for a in active)

    def test_parity_with_json_active_vs_pop(
        self, sqlite_sched: SqliteScheduler, json_sched: Scheduler
    ) -> None:
        for sched in (sqlite_sched, json_sched):
            self._setup_one_active_one_expired(sched)  # type: ignore[arg-type]

        sq_active = {w["id"] for w in sqlite_sched.active_indicator_watches(now=_NOW)}
        js_active = {w["id"] for w in json_sched.active_indicator_watches(now=_NOW)}
        assert sq_active == js_active

    def test_active_preserves_order_by_seq(self, sqlite_sched: SqliteScheduler) -> None:
        """active_indicator_watches retourne les watches dans l'ordre d'insertion (seq)."""
        ids_in_order = ["SPY:w1", "QQQ:w2", "AAPL:w3"]
        for wid in ids_in_order:
            sym = wid.split(":")[0]
            w = _wake_watch(wid, sym, _future(60))
            sqlite_sched.set_symbol_indicator_watch(sym, w)

        active = sqlite_sched.active_indicator_watches(now=_NOW)
        returned_ids = [a["id"] for a in active]
        assert returned_ids == ids_in_order


# ---------------------------------------------------------------------------
# remove_indicator_watch
# ---------------------------------------------------------------------------


class TestRemoveIndicatorWatch:
    def test_remove_existing_watch(
        self, sqlite_sched: SqliteScheduler
    ) -> None:
        w = _wake_watch("SPY:001", "SPY", _future(60))
        sqlite_sched.set_symbol_indicator_watch("SPY", w)
        sqlite_sched.remove_indicator_watch("SPY:001")

        active = sqlite_sched.active_indicator_watches(now=_NOW)
        assert not any(a["id"] == "SPY:001" for a in active)

    def test_remove_nonexistent_is_noop(
        self, sqlite_sched: SqliteScheduler
    ) -> None:
        sqlite_sched.remove_indicator_watch("nonexistent")  # pas d'erreur

    def test_parity_with_json_remove(
        self, sqlite_sched: SqliteScheduler, json_sched: Scheduler
    ) -> None:
        w = _wake_watch("SPY:001", "SPY", _future(60))
        for sched in (sqlite_sched, json_sched):
            sched.set_symbol_indicator_watch("SPY", w)
            sched.remove_indicator_watch("SPY:001")

        sq = sqlite_sched.active_indicator_watches(now=_NOW)
        js = json_sched.active_indicator_watches(now=_NOW)
        assert [a["id"] for a in sq] == [a["id"] for a in js]

    # FIX 3 — remove inexistant ne doit PAS réécrire le shadow
    def test_remove_nonexistent_does_not_rewrite_shadow(
        self, sqlite_sched: SqliteScheduler
    ) -> None:
        """remove d'un id inexistant → _write_shadow NON appelé."""
        with patch.object(sqlite_sched, "_write_shadow") as mock_write:
            sqlite_sched.remove_indicator_watch("nonexistent-id")
            mock_write.assert_not_called()

    def test_remove_existing_does_rewrite_shadow(
        self, sqlite_sched: SqliteScheduler
    ) -> None:
        """remove d'un id existant → _write_shadow appelé."""
        w = _wake_watch("SPY:001", "SPY", _future(60))
        sqlite_sched.set_symbol_indicator_watch("SPY", w)
        with patch.object(sqlite_sched, "_write_shadow") as mock_write:
            sqlite_sched.remove_indicator_watch("SPY:001")
            mock_write.assert_called_once()


# ---------------------------------------------------------------------------
# reconcile_universe
# ---------------------------------------------------------------------------


class TestReconcileUniverse:
    def _populate(self, sched: AnyScheduler) -> None:
        sched.set_symbol_next_wake("SPY", _future(30))
        sched.set_symbol_next_wake("QQQ", _future(60))
        sched.set_symbol_next_wake("AAPL", _future(90))
        sched.set_stale_streak("SPY", 2)
        sched.set_stale_streak("QQQ", 1)
        sched.set_symbol_indicator_watch("SPY", _wake_watch("SPY:001", "SPY", _future(60)))
        sched.set_symbol_indicator_watch("QQQ", _wake_watch("QQQ:001", "QQQ", _future(60)))

    def test_reconcile_purges_out_of_universe(
        self, sqlite_sched: SqliteScheduler, db: StateDb
    ) -> None:
        self._populate(sqlite_sched)
        sqlite_sched.reconcile_universe(["SPY"])

        # SPY conservé
        assert sqlite_sched.has_symbol_wake("SPY")
        assert sqlite_sched.get_stale_streak("SPY") == 2
        assert len(sqlite_sched.active_indicator_watches(now=_NOW)) == 1

        # QQQ et AAPL purgés
        assert not sqlite_sched.has_symbol_wake("QQQ")
        assert not sqlite_sched.has_symbol_wake("AAPL")
        assert sqlite_sched.get_stale_streak("QQQ") == 0

    def test_reconcile_in_one_transaction(
        self, sqlite_sched: SqliteScheduler, db: StateDb
    ) -> None:
        """Les 3 tables sont purgées ensemble (atomique)."""
        self._populate(sqlite_sched)
        sqlite_sched.reconcile_universe(["SPY"])
        # Si la transaction était partielle, on aurait des incohérences :
        # QQQ dans symbol_wake mais pas dans stale_streaks — vérifions les deux.
        assert not sqlite_sched.has_symbol_wake("QQQ")
        assert sqlite_sched.get_stale_streak("QQQ") == 0

    def test_reconcile_idempotent(
        self, sqlite_sched: SqliteScheduler
    ) -> None:
        self._populate(sqlite_sched)
        sqlite_sched.reconcile_universe(["SPY"])
        sqlite_sched.reconcile_universe(["SPY"])  # deuxième appel → no-op
        # Données SPY intactes
        assert sqlite_sched.has_symbol_wake("SPY")

    def test_reconcile_empty_universe_deletes_all(
        self, sqlite_sched: SqliteScheduler
    ) -> None:
        self._populate(sqlite_sched)
        sqlite_sched.reconcile_universe([])
        assert not sqlite_sched.has_symbol_wake("SPY")
        assert sqlite_sched.get_stale_streak("SPY") == 0
        assert sqlite_sched.active_indicator_watches(now=_NOW) == []

    def test_reconcile_noop_if_universe_unchanged(
        self, sqlite_sched: SqliteScheduler, tmp_path: Path
    ) -> None:
        """Si l'univers n'a pas changé, shadow NON régénéré inutilement.

        On vérifie l'idempotence comportementale (pas de crash, état intact).
        """
        self._populate(sqlite_sched)
        universe = ["SPY", "QQQ", "AAPL"]
        sqlite_sched.reconcile_universe(universe)  # no-op (tous présents)
        # Aucune purge : SPY, QQQ, AAPL toujours là
        assert sqlite_sched.has_symbol_wake("SPY")
        assert sqlite_sched.has_symbol_wake("QQQ")
        assert sqlite_sched.has_symbol_wake("AAPL")

    def test_parity_with_json_reconcile(
        self, sqlite_sched: SqliteScheduler, json_sched: Scheduler
    ) -> None:
        for sched in (sqlite_sched, json_sched):
            self._populate(sched)  # type: ignore[arg-type]
            sched.reconcile_universe(["SPY"])

        assert sqlite_sched.has_symbol_wake("SPY") == json_sched.has_symbol_wake("SPY")
        assert sqlite_sched.has_symbol_wake("QQQ") == json_sched.has_symbol_wake("QQQ")


# ---------------------------------------------------------------------------
# Shadow JSON
# ---------------------------------------------------------------------------


class TestShadowJson:
    def test_shadow_written_after_set_default_next_wake(
        self, sqlite_sched: SqliteScheduler, tmp_path: Path
    ) -> None:
        sqlite_sched.set_default_next_wake(_future(30))
        shadow = json.loads((tmp_path / "scheduler.json").read_text())
        assert shadow["default_next_wake"] is not None
        assert "+00:00" in shadow["default_next_wake"]

    def test_shadow_written_after_set_symbol_next_wake(
        self, sqlite_sched: SqliteScheduler, tmp_path: Path
    ) -> None:
        sqlite_sched.set_symbol_next_wake("SPY", _future(30))
        shadow = json.loads((tmp_path / "scheduler.json").read_text())
        assert "SPY" in shadow["symbols"]

    def test_shadow_written_after_set_stale_streak(
        self, sqlite_sched: SqliteScheduler, tmp_path: Path
    ) -> None:
        sqlite_sched.set_stale_streak("AAPL", 3)
        shadow = json.loads((tmp_path / "scheduler.json").read_text())
        assert shadow["stale_streaks"]["AAPL"] == 3

    def test_shadow_mirrors_watches(
        self, sqlite_sched: SqliteScheduler, tmp_path: Path
    ) -> None:
        w = _wake_watch("SPY:001", "SPY", _future(60))
        sqlite_sched.set_symbol_indicator_watch("SPY", w)
        shadow = json.loads((tmp_path / "scheduler.json").read_text())
        assert "SPY:001" in shadow["indicator_watches"]

    def test_shadow_has_correct_format(
        self, sqlite_sched: SqliteScheduler, tmp_path: Path
    ) -> None:
        """Format shadow identique à scheduler.json (Scheduler)."""
        sqlite_sched.set_default_next_wake(_future(60))
        sqlite_sched.set_symbol_next_wake("SPY", _future(30))
        sqlite_sched.set_stale_streak("AAPL", 2)
        sqlite_sched.set_symbol_indicator_watch(
            "SPY", _wake_watch("SPY:001", "SPY", _future(60))
        )

        shadow = json.loads((tmp_path / "scheduler.json").read_text())
        assert set(shadow.keys()) >= {"default_next_wake", "symbols", "stale_streaks", "indicator_watches"}
        assert isinstance(shadow["symbols"], dict)
        assert isinstance(shadow["stale_streaks"], dict)
        assert isinstance(shadow["indicator_watches"], dict)

    def test_shadow_purged_after_active_watches(
        self, sqlite_sched: SqliteScheduler, tmp_path: Path
    ) -> None:
        """Shadow mis à jour quand active_indicator_watches purge des expirées."""
        w_active = _wake_watch("SPY:active", "SPY", _future(30))
        w_expired = _wake_watch("MSFT:expired", "MSFT", _past(30))
        sqlite_sched.set_symbol_indicator_watch("SPY", w_active)
        sqlite_sched.set_symbol_indicator_watch("MSFT", w_expired)

        sqlite_sched.active_indicator_watches(now=_NOW)
        shadow = json.loads((tmp_path / "scheduler.json").read_text())
        assert "MSFT:expired" not in shadow["indicator_watches"]
        assert "SPY:active" in shadow["indicator_watches"]

    def test_regenerate_shadow_from_db(
        self, sqlite_sched: SqliteScheduler, tmp_path: Path, db: StateDb
    ) -> None:
        """regenerate_shadow() reconstruit le shadow depuis SQLite."""
        sqlite_sched.set_default_next_wake(_future(30))
        # Effacer le shadow
        (tmp_path / "scheduler.json").unlink(missing_ok=True)
        # Régénérer
        sqlite_sched.regenerate_shadow()
        shadow = json.loads((tmp_path / "scheduler.json").read_text())
        assert shadow["default_next_wake"] is not None

    def test_shadow_none_json_path_no_write(
        self, db: StateDb, tmp_path: Path
    ) -> None:
        """SqliteScheduler sans json_path → pas de fichier écrit."""
        sched_no_shadow = SqliteScheduler(db, json_path=None)
        sched_no_shadow.set_default_next_wake(_future(30))
        # Aucun fichier créé
        assert not (tmp_path / "scheduler.json").exists()


# ---------------------------------------------------------------------------
# make_scheduler factory
# ---------------------------------------------------------------------------


class TestMakeScheduler:
    def test_make_scheduler_json(self, tmp_path: Path) -> None:
        from trader.state_db.broker_factory import make_scheduler
        from trader.planning.scheduler import Scheduler

        sched = make_scheduler(state_dir=tmp_path, backend="json")
        assert isinstance(sched, Scheduler)

    def test_make_scheduler_sqlite(self, tmp_path: Path) -> None:
        from trader.state_db.broker_factory import make_scheduler

        sched = make_scheduler(state_dir=tmp_path, backend="sqlite")
        assert isinstance(sched, SqliteScheduler)

    def test_make_scheduler_sqlite_with_existing_json(self, tmp_path: Path) -> None:
        from trader.state_db.broker_factory import make_scheduler

        # Créer un scheduler.json avec des données
        data = {
            "default_next_wake": _future(60),
            "symbols": {"SPY": _future(30)},
            "stale_streaks": {},
            "indicator_watches": {},
        }
        (tmp_path / "scheduler.json").write_text(json.dumps(data))
        sched = make_scheduler(state_dir=tmp_path, backend="sqlite")

        assert isinstance(sched, SqliteScheduler)
        assert sched.has_symbol_wake("SPY")

    def test_make_scheduler_bogus_raises(self, tmp_path: Path) -> None:
        from trader.state_db.broker_factory import make_scheduler

        with pytest.raises(ValueError, match="inconnu"):
            make_scheduler(state_dir=tmp_path, backend="bogus")

    def test_make_scheduler_default_is_json(self, tmp_path: Path) -> None:
        from trader.state_db.broker_factory import make_scheduler
        from trader.planning.scheduler import Scheduler

        sched = make_scheduler(state_dir=tmp_path)
        assert isinstance(sched, Scheduler)
