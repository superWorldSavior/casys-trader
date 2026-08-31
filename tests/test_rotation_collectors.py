"""Tests for rotation_collectors — TDD strict."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _make_position(qty: float) -> SimpleNamespace:
    """Fake position avec un champ quantity."""
    return SimpleNamespace(quantity=qty)


def _make_plan(symbol: str) -> SimpleNamespace:
    """Fake TradePlan avec un champ .symbol."""
    return SimpleNamespace(symbol=symbol)


def _seed_sqlite_position(
    state_dir: Path,
    *,
    symbol: str,
    quantity: float,
    price: float,
) -> None:
    """Crée une vraie position broker dans la base canonique."""
    from trader.domain.contracts import Order
    from trader.infrastructure.state_db.broker_store import SqliteBroker
    from trader.infrastructure.state_db.connection import open_state_db
    from trader.infrastructure.state_db.migrations import import_broker_from_json

    db = open_state_db(state_dir / "casys.db")
    import_broker_from_json(
        db,
        state_dir / "_absent_broker.json",
        starting_cash=100_000.0,
    )
    SqliteBroker(db).submit(
        Order(symbol, "BUY", quantity),
        price,
        "2026-08-20T00:00:00+00:00",
        dry_run=False,
    )


# ---------------------------------------------------------------------------
# sticky_collector
# ---------------------------------------------------------------------------


class TestStickyCollector:
    def test_union_of_positions_and_plans(self):
        """AAA (qty=10) + BBB (plan) → {'AAA','BBB'} ; FLAT (qty=0) exclu."""
        from trader.market.rotation.collectors import sticky_collector

        positions_fn = lambda: {  # noqa: E731
            "AAA": _make_position(10),
            "FLAT": _make_position(0),
        }
        plans_fn = lambda: [_make_plan("BBB")]  # noqa: E731

        result = sticky_collector(positions_fn=positions_fn, plans_fn=plans_fn)

        assert result == {"AAA", "BBB"}

    def test_zero_quantity_excluded(self):
        """Une position à qty=0 n'est pas sticky."""
        from trader.market.rotation.collectors import sticky_collector

        positions_fn = lambda: {"FLAT": _make_position(0.0)}  # noqa: E731
        plans_fn = lambda: []  # noqa: E731

        assert sticky_collector(positions_fn=positions_fn, plans_fn=plans_fn) == set()

    def test_empty_inputs_return_empty_set(self):
        from trader.market.rotation.collectors import sticky_collector

        assert sticky_collector(positions_fn=lambda: {}, plans_fn=lambda: []) == set()

    def test_negative_quantity_is_sticky(self):
        """Position short (qty < 0) est non-nulle → sticky."""
        from trader.market.rotation.collectors import sticky_collector

        positions_fn = lambda: {"SHORT": _make_position(-5)}  # noqa: E731
        assert sticky_collector(positions_fn=positions_fn, plans_fn=lambda: []) == {"SHORT"}

    def test_duplicate_symbol_in_both_sources(self):
        """Si un symbole est en position ET en plan, il apparaît une seule fois."""
        from trader.market.rotation.collectors import sticky_collector

        positions_fn = lambda: {"AAA": _make_position(3)}  # noqa: E731
        plans_fn = lambda: [_make_plan("AAA")]  # noqa: E731

        assert sticky_collector(positions_fn=positions_fn, plans_fn=plans_fn) == {"AAA"}

    def test_union_includes_active_watches_and_pending_replays(self):
        from trader.market.rotation.collectors import sticky_collector

        result = sticky_collector(
            positions_fn=lambda: {"POSITION": _make_position(3)},
            plans_fn=lambda: [_make_plan("PLAN")],
            watches_fn=lambda: [
                {"symbol": "WATCH"},
                SimpleNamespace(symbol="OBJECT_WATCH"),
            ],
            pending_symbols_fn=lambda: {"PENDING"},
        )

        assert result == {
            "OBJECT_WATCH",
            "PENDING",
            "PLAN",
            "POSITION",
            "WATCH",
        }


# ---------------------------------------------------------------------------
# build_positions_fn / build_plans_fn — fail-safe sur state_dir vide
# ---------------------------------------------------------------------------


class TestBuildPositionsFn:
    def test_empty_state_dir_returns_empty_dict(self, tmp_path: Path):
        """Aucun broker.json → closure retourne {} sans lever."""
        from trader.runtime.market_rotation_runtime import build_positions_fn

        fn = build_positions_fn(tmp_path)
        result = fn()
        assert result == {}

    def test_returns_callable(self, tmp_path: Path):
        from trader.runtime.market_rotation_runtime import build_positions_fn

        fn = build_positions_fn(tmp_path)
        assert callable(fn)

    def test_lit_les_positions_reelles_depuis_sqlite(self, tmp_path: Path):
        from trader.runtime.market_rotation_runtime import build_positions_fn

        _seed_sqlite_position(
            tmp_path,
            symbol="2330.TW",
            quantity=7.0,
            price=2425.0,
        )

        positions = build_positions_fn(tmp_path)()

        assert set(positions) == {"2330.TW"}
        assert positions["2330.TW"].quantity == 7.0
        assert positions["2330.TW"].avg_price == 2425.0

    def test_sqlite_prime_sur_un_broker_json_contradictoire(self, tmp_path: Path):
        from trader.runtime.market_rotation_runtime import build_positions_fn

        _seed_sqlite_position(
            tmp_path,
            symbol="SQLITE",
            quantity=3.0,
            price=100.0,
        )
        (tmp_path / "broker.json").write_text(
            json.dumps(
                {
                    "cash": 90_000.0,
                    "positions": {
                        "STALE": {
                            "symbol": "STALE",
                            "quantity": 99.0,
                            "avg_price": 1.0,
                        }
                    },
                    "fills": [],
                }
            ),
            encoding="utf-8",
        )

        positions = build_positions_fn(tmp_path)()

        assert set(positions) == {"SQLITE"}
        assert positions["SQLITE"].quantity == 3.0


class TestBuildPlansFn:
    def test_empty_state_dir_returns_empty_list(self, tmp_path: Path):
        """Aucun plans.json → closure retourne [] sans lever."""
        from trader.runtime.market_rotation_runtime import build_plans_fn

        fn = build_plans_fn(tmp_path)
        result = fn()
        # Doit être un conteneur vide itérable
        assert list(result) == []

    def test_returns_callable(self, tmp_path: Path):
        from trader.runtime.market_rotation_runtime import build_plans_fn

        fn = build_plans_fn(tmp_path)
        assert callable(fn)

    def test_ignore_le_shadow_json_si_casys_db_absent(self, tmp_path: Path):
        """Le collecteur prod lit casys.db ; sans DB, le shadow JSON ne fait pas foi."""
        from trader.runtime.market_rotation_runtime import build_plans_fn
        from trader.planning.trade_plan import create_trade_plan

        (tmp_path / "trade_plans.json").write_text(
            '{"plans": ['
            + create_trade_plan(
                symbol="SPY",
                side="LONG",
                quantity=10.0,
                entry_price=100.0,
                opened_at="2026-06-05T12:00:00+00:00",
                raw_exit_plan={"hard_stop": 95.0},
            ).model_dump_json()
            + "]}",
            encoding="utf-8",
        )

        plans = build_plans_fn(tmp_path)()

        assert plans == []

    def test_lit_les_plans_depuis_sqlite_si_casys_db_existe(self, tmp_path: Path):
        """En prod SQLite, le collecteur sticky lit casys.db sans dépendre du shadow JSON."""
        from trader.infrastructure.state_db.connection import open_state_db
        from trader.planning.trade_plan import create_trade_plan
        from trader.state_db.migrations import import_trade_plans_from_json
        from trader.state_db.trade_plan_store import SqliteTradePlanStore
        from trader.runtime.market_rotation_runtime import build_plans_fn

        db = open_state_db(tmp_path / "casys.db")
        import_trade_plans_from_json(db, tmp_path / "_absent_trade_plans.json")
        SqliteTradePlanStore(db).upsert(
            create_trade_plan(
                symbol="SPY",
                side="LONG",
                quantity=10.0,
                entry_price=100.0,
                opened_at="2026-06-05T12:00:00+00:00",
                raw_exit_plan={"hard_stop": 95.0},
            )
        )

        plans = build_plans_fn(tmp_path)()

        assert [p.symbol for p in plans] == ["SPY"]


class TestBuildStickyFn:
    def test_json_watch_snapshot_filters_expired_without_purging(self, tmp_path: Path):
        from trader.runtime.market_rotation_runtime import build_watches_fn

        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        scheduler_path = tmp_path / "scheduler.json"
        scheduler_path.write_text(
            json.dumps(
                {
                    "indicator_watches": {
                        "active": {
                            "id": "active",
                            "symbol": "ACTIVE",
                            "expires_at": (now + timedelta(hours=1)).isoformat(),
                        },
                        "expired": {
                            "id": "expired",
                            "symbol": "EXPIRED",
                            "expires_at": now.isoformat(),
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        before = scheduler_path.read_text(encoding="utf-8")

        watches = build_watches_fn(tmp_path, now_fn=lambda: now)()

        assert [watch["symbol"] for watch in watches] == ["ACTIVE"]
        assert scheduler_path.read_text(encoding="utf-8") == before

    def test_sqlite_watch_and_recent_pending_replay_are_sticky_without_purge(
        self,
        tmp_path: Path,
    ) -> None:
        from trader.domain.planning.relevance_gate import (
            STALE_REVIEW_FINGERPRINT_KEY,
            STALE_REVIEW_PENDING,
        )
        from trader.infrastructure.state_db.connection import open_state_db
        from trader.infrastructure.state_db.llm_gate_store import LlmGateStore
        from trader.infrastructure.state_db.migrations import (
            LLM_GATE_MIGRATIONS,
            SCHEDULER_MIGRATION,
        )
        from trader.infrastructure.state_db.scheduler_store import SqliteScheduler
        from trader.runtime.market_rotation_runtime import build_sticky_fn

        now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        db = open_state_db(tmp_path / "casys.db")
        db.apply_migrations([SCHEDULER_MIGRATION, *LLM_GATE_MIGRATIONS])
        scheduler = SqliteScheduler(db)
        scheduler.set_symbol_indicator_watch(
            "WATCH.ACTIVE",
            {
                "id": "watch-active",
                "symbol": "WATCH.ACTIVE",
                "created_at": (now - timedelta(hours=1)).isoformat(),
                "expires_at": (now + timedelta(hours=1)).isoformat(),
                "on_trigger": "WAKE",
            },
        )
        scheduler.set_symbol_indicator_watch(
            "WATCH.EXPIRED",
            {
                "id": "watch-expired",
                "symbol": "WATCH.EXPIRED",
                "created_at": (now - timedelta(hours=2)).isoformat(),
                "expires_at": now.isoformat(),
                "on_trigger": "WAKE",
            },
        )
        scheduler.set_symbol_indicator_watch(
            "OUTBOX.PENDING",
            {
                "id": "outbox-pending",
                "symbol": "OUTBOX.PENDING",
                "created_at": (now - timedelta(minutes=5)).isoformat(),
                "expires_at": (now + timedelta(hours=1)).isoformat(),
                "on_trigger": "WAKE",
            },
        )
        assert scheduler.claim_indicator_watch_trigger(
            "outbox-pending",
            symbol="OUTBOX.PENDING",
            closed_bar_key=None,
            when_iso=now.isoformat(),
            trigger_payload={
                "watch_id": "outbox-pending",
                "symbol": "OUTBOX.PENDING",
                "on_trigger": "WAKE",
            },
        )
        gate_store = LlmGateStore(db)
        gate_store.record(
            str(tmp_path),
            "REPLAY.RECENT",
            now - timedelta(hours=36),
            wake_fingerprints={
                STALE_REVIEW_FINGERPRINT_KEY: STALE_REVIEW_PENDING,
            },
        )
        gate_store.record(
            str(tmp_path),
            "REPLAY.OLD",
            now - timedelta(hours=36, seconds=1),
            wake_fingerprints={
                STALE_REVIEW_FINGERPRINT_KEY: STALE_REVIEW_PENDING,
            },
        )
        gate_store.record(
            str(tmp_path / "other-state"),
            "REPLAY.OTHER_STATE",
            now,
            wake_fingerprints={
                STALE_REVIEW_FINGERPRINT_KEY: STALE_REVIEW_PENDING,
            },
        )
        scheduler.set_symbol_next_wake(
            "REPLAY.RECENT",
            (now + timedelta(minutes=15)).isoformat(),
        )
        scheduler.set_symbol_next_wake(
            "REPLAY.OLD",
            (now + timedelta(days=8)).isoformat(),
        )

        sticky = build_sticky_fn(tmp_path, now_fn=lambda: now)()

        assert "WATCH.ACTIVE" in sticky
        assert "REPLAY.RECENT" in sticky
        assert "OUTBOX.PENDING" in sticky
        assert "WATCH.EXPIRED" not in sticky
        assert "REPLAY.OLD" not in sticky
        assert "REPLAY.OTHER_STATE" not in sticky
        assert "watch-expired" in scheduler.watches()

        scheduler.reconcile_universe({"BASE", *sticky}, now=now)
        assert "watch-active" in scheduler.watches()
        assert scheduler.symbols_with_wake() == {
            "OUTBOX.PENDING",
            "REPLAY.RECENT",
        }

        # Même si un caller oubliait d'unir le sticky, le scheduler protège le
        # wake d'une livraison durable non acquittée.
        scheduler.reconcile_universe({"BASE"}, now=now)
        assert scheduler.symbols_with_wake() == {"OUTBOX.PENDING"}

    def test_weekend_pending_replay_keeps_agent_wake_without_probe_marker(
        self,
        tmp_path: Path,
    ) -> None:
        from trader.domain.planning.relevance_gate import (
            STALE_REVIEW_FINGERPRINT_KEY,
            STALE_REVIEW_PENDING,
        )
        from trader.infrastructure.state_db.connection import open_state_db
        from trader.infrastructure.state_db.llm_gate_store import LlmGateStore
        from trader.infrastructure.state_db.migrations import (
            LLM_GATE_MIGRATIONS,
            SCHEDULER_MIGRATION,
        )
        from trader.infrastructure.state_db.scheduler_store import SqliteScheduler
        from trader.runtime.market_rotation_runtime import build_sticky_fn

        friday_review = datetime(2026, 8, 28, 19, 45, tzinfo=timezone.utc)
        sunday = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        monday_open = datetime(2026, 8, 31, 13, 30, tzinfo=timezone.utc)
        db = open_state_db(tmp_path / "casys.db")
        db.apply_migrations([SCHEDULER_MIGRATION, *LLM_GATE_MIGRATIONS])
        SqliteScheduler(db).set_symbol_next_wake("SPY", monday_open.isoformat())
        LlmGateStore(db).record(
            str(tmp_path),
            "SPY",
            friday_review,
            wake_fingerprints={
                STALE_REVIEW_FINGERPRINT_KEY: STALE_REVIEW_PENDING,
            },
        )

        sticky = build_sticky_fn(tmp_path, now_fn=lambda: sunday)()

        assert "SPY" in sticky

        just_after_open = monday_open + timedelta(minutes=5)
        assert "SPY" in build_sticky_fn(
            tmp_path,
            now_fn=lambda: just_after_open,
        )()

        orphaned_after_grace = monday_open + timedelta(hours=37)
        assert "SPY" not in build_sticky_fn(
            tmp_path,
            now_fn=lambda: orphaned_after_grace,
        )()

    def test_friday_probe_marker_survives_weekend_restart_and_rotation(
        self,
        tmp_path: Path,
    ) -> None:
        from trader.domain.planning.relevance_gate import (
            FRESH_PROBE_WAKE_FINGERPRINT_KEY,
            STALE_REVIEW_FINGERPRINT_KEY,
            STALE_REVIEW_PENDING,
        )
        from trader.infrastructure.state_db.connection import (
            close_all_state_dbs,
            open_state_db,
        )
        from trader.infrastructure.state_db.llm_gate_store import LlmGateStore
        from trader.infrastructure.state_db.migrations import (
            LLM_GATE_MIGRATIONS,
            SCHEDULER_MIGRATION,
        )
        from trader.infrastructure.state_db.scheduler_store import SqliteScheduler
        from trader.runtime.market_rotation_runtime import build_sticky_fn

        friday_review = datetime(2026, 8, 28, 19, 45, tzinfo=timezone.utc)
        friday_probe = datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc)
        sunday = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        monday_open = datetime(2026, 8, 31, 13, 30, tzinfo=timezone.utc)
        db = open_state_db(tmp_path / "casys.db")
        db.apply_migrations([SCHEDULER_MIGRATION, *LLM_GATE_MIGRATIONS])
        SqliteScheduler(db).set_symbol_next_wake("SPY", friday_probe.isoformat())
        LlmGateStore(db).record(
            str(tmp_path),
            "SPY",
            friday_review,
            wake_fingerprints={
                STALE_REVIEW_FINGERPRINT_KEY: STALE_REVIEW_PENDING,
                FRESH_PROBE_WAKE_FINGERPRINT_KEY: friday_probe.isoformat(),
            },
        )
        close_all_state_dbs()

        sunday_sticky = build_sticky_fn(tmp_path, now_fn=lambda: sunday)()
        assert "SPY" in sunday_sticky

        restarted_scheduler = SqliteScheduler(open_state_db(tmp_path / "casys.db"))
        restarted_scheduler.reconcile_universe({"BASE", *sunday_sticky})
        assert restarted_scheduler.has_symbol_wake("SPY") is True

        assert "SPY" in build_sticky_fn(
            tmp_path,
            now_fn=lambda: monday_open + timedelta(minutes=5),
        )()

        lease_deadline = monday_open + timedelta(hours=36)
        assert "SPY" not in build_sticky_fn(
            tmp_path,
            now_fn=lambda: lease_deadline + timedelta(seconds=1),
        )()
        close_all_state_dbs()


# ---------------------------------------------------------------------------
# default_override_fn
# ---------------------------------------------------------------------------


class TestDefaultOverrideFn:
    def test_returns_noop_override(self):
        from trader.market.rotation.collectors import default_override_fn

        result = default_override_fn({"symbols": ["AAA", "BBB"]})
        assert result == {"add": [], "remove": []}

    def test_noop_regardless_of_payload(self):
        from trader.market.rotation.collectors import default_override_fn

        assert default_override_fn(None) == {"add": [], "remove": []}
        assert default_override_fn({}) == {"add": [], "remove": []}
        assert default_override_fn("anything") == {"add": [], "remove": []}
