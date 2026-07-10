"""Tests TDD — SqliteTradePlanStore (mapping SQLite, factory, mutations).

Couvre :
- Round-trip d'un plan riche (tous les sous-objets, dump identique).
- Séquence complète : upsert ×3, close, close_symbol, sync partiel, sync→0.
- open_plans() stable après chaque op (ordre inclus).
- sync_symbol_quantity : rescale TP non remplis, conserve remplis,
  remaining→0 ferme le symbole, en UNE transaction.
- make_trade_plan_store : json fast-fail/sqlite/bogus.
"""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from trader.planning.trade_plan import (
    ProfitProtection,
    TakeProfit,
    TradePlan,
    TrailingStop,
)
from trader.state_db.connection import StateDb
from trader.state_db.migrations import import_trade_plans_from_json
from trader.state_db.trade_plan_store import (
    SqliteTradePlanStore,
    plan_to_columns,
    row_to_plan,
)

# ---------------------------------------------------------------------------
# Fixtures helpers
# ---------------------------------------------------------------------------


def _dump(plan: TradePlan) -> dict:
    return plan.model_dump()


def _plan(
    plan_id: str = "AAPL-2026-07-01T10:00:00+00:00",
    symbol: str = "AAPL",
    *,
    quantity: float = 10.0,
    remaining_quantity: float | None = None,
    side: str = "LONG",
    entry_price: float = 100.0,
    hard_stop_price: float | None = 95.0,
    take_profits: list | None = None,
    filled_take_profits: list | None = None,
    trailing_stop: TrailingStop | None = None,
    profit_protection: ProfitProtection | None = None,
    exit_watch: dict | None = None,
    last_llm_review: dict | None = None,
    entry_context: dict | None = None,
) -> TradePlan:
    if remaining_quantity is None:
        remaining_quantity = quantity
    return TradePlan(
        id=plan_id,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        quantity=quantity,
        remaining_quantity=remaining_quantity,
        entry_price=entry_price,
        opened_at="2026-07-01T10:00:00+00:00",
        reference_volatility=0.02,
        hard_stop_price=hard_stop_price,
        take_profits=take_profits or [],
        trailing_stop=trailing_stop,
        max_hold_minutes=120.0,
        high_watermark=105.0,
        low_watermark=98.0,
        filled_take_profits=filled_take_profits or [],
        profit_protection=profit_protection,
        exit_watch=exit_watch,
        llm_provider="openai",
        llm_model="gpt-4o",
        llm_fallback_reason=None,
        llm_confidence=0.85,
        last_llm_review=last_llm_review,
        entry_thesis="Test thesis",
        entry_decision_id="dec-001",
        entry_context=entry_context,
    )


def _rich_plan(plan_id: str = "AAPL-001", symbol: str = "AAPL") -> TradePlan:
    """Plan avec tous les sous-objets optionnels non-None."""
    return _plan(
        plan_id=plan_id,
        symbol=symbol,
        take_profits=[
            TakeProfit(name="tp1", price=110.0, fraction=0.5, quantity=5.0, after_fill=""),
            TakeProfit(name="tp2", price=120.0, fraction=0.5, quantity=5.0, after_fill="hold"),
        ],
        trailing_stop=TrailingStop(
            enabled_after="tp1",
            trail_type="percent",
            trail_value=2.0,
            trail_floored=False,
        ),
        profit_protection=ProfitProtection(
            enabled=True,
            arm_at_r=0.5,
            trigger_on_giveback_pct=0.4,
            close_fraction=1.0 / 3.0,
            move_stop_to="breakeven",
            min_hold_minutes=10.0,
            lock_r=1.5,
            triggered=False,
        ),
        exit_watch={"type": "indicator", "on_trigger": "WAKE", "source": "exit_watch"},
        last_llm_review={"ts": "2026-07-01T10:05:00+00:00", "action": "HOLD"},
        entry_context={"price": 100.0, "session": "US"},
    )


@pytest.fixture()
def db(tmp_path: Path) -> StateDb:
    return StateDb(tmp_path / "casys.db")


@pytest.fixture()
def sqlite_store(db: StateDb, tmp_path: Path) -> SqliteTradePlanStore:
    import_trade_plans_from_json(db, tmp_path / "trade_plans.json")  # absent → vide
    return SqliteTradePlanStore(db)


# ---------------------------------------------------------------------------
# Round-trip plan_to_columns / row_to_plan
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_round_trip_minimal_plan(self, db: StateDb) -> None:
        """Plan minimal (pas de sous-objets) → dump identique après colonnes→row→plan."""
        plan = _plan()
        cols = plan_to_columns(plan, seq=1)
        # Simuler une row via query
        import_trade_plans_from_json(db, Path("/nonexistent/trade_plans.json"))  # no-op absent
        with db.transaction() as cur:
            cur.execute(
                """INSERT INTO trade_plans(
                    id, seq, symbol, side, quantity, remaining_quantity,
                    entry_price, opened_at, reference_volatility, hard_stop_price,
                    max_hold_minutes, high_watermark, low_watermark,
                    llm_provider, llm_model, llm_fallback_reason, llm_confidence,
                    entry_thesis, entry_decision_id,
                    trailing_json, profit_protection_json, take_profits_json,
                    filled_take_profits_json, exit_watch_json,
                    last_llm_review_json, entry_context_json
                ) VALUES (
                    :id, :seq, :symbol, :side, :quantity, :remaining_quantity,
                    :entry_price, :opened_at, :reference_volatility, :hard_stop_price,
                    :max_hold_minutes, :high_watermark, :low_watermark,
                    :llm_provider, :llm_model, :llm_fallback_reason, :llm_confidence,
                    :entry_thesis, :entry_decision_id,
                    :trailing_json, :profit_protection_json, :take_profits_json,
                    :filled_take_profits_json, :exit_watch_json,
                    :last_llm_review_json, :entry_context_json
                )""",
                cols,
            )
        row = db.query_one("SELECT * FROM trade_plans WHERE id=?", (plan.id,))
        assert row is not None
        retrieved = row_to_plan(row)
        assert _dump(retrieved) == _dump(plan)

    def test_round_trip_rich_plan(self, sqlite_store: SqliteTradePlanStore) -> None:
        """Plan riche (trailing, profit_protection, exit_watch, take_profits, etc.) → round-trip exact."""
        plan = _rich_plan()
        sqlite_store.upsert(plan)
        plans = sqlite_store.open_plans()
        assert len(plans) == 1
        assert _dump(plans[0]) == _dump(plan)

    def test_round_trip_filled_take_profits(self, sqlite_store: SqliteTradePlanStore) -> None:
        """filled_take_profits non vide → conservé exactement."""
        plan = _plan(
            take_profits=[
                TakeProfit(name="tp1", price=110.0, fraction=0.5, quantity=5.0, after_fill=""),
            ],
            filled_take_profits=["tp1"],
        )
        sqlite_store.upsert(plan)
        retrieved = sqlite_store.open_plans()[0]
        assert retrieved.filled_take_profits == ["tp1"]
        assert _dump(retrieved) == _dump(plan)

    def test_round_trip_none_optional_fields(self, sqlite_store: SqliteTradePlanStore) -> None:
        """Plan avec tous les champs optionnels à None → round-trip exact."""
        plan = _plan(
            hard_stop_price=None,
            trailing_stop=None,
            profit_protection=None,
            exit_watch=None,
            last_llm_review=None,
            entry_context=None,
        )
        sqlite_store.upsert(plan)
        retrieved = sqlite_store.open_plans()[0]
        assert _dump(retrieved) == _dump(plan)
        assert retrieved.trailing_stop is None
        assert retrieved.profit_protection is None
        assert retrieved.exit_watch is None


# ---------------------------------------------------------------------------
# Opérations de base
# ---------------------------------------------------------------------------


class TestSqliteTradePlanStoreOps:
    def test_open_plans_empty(self, sqlite_store: SqliteTradePlanStore) -> None:
        assert sqlite_store.open_plans() == []

    def test_upsert_and_open(self, sqlite_store: SqliteTradePlanStore) -> None:
        plan = _plan("P1", "AAPL")
        sqlite_store.upsert(plan)
        plans = sqlite_store.open_plans()
        assert len(plans) == 1
        assert plans[0].id == "P1"

    def test_upsert_replaces_and_moves_to_end(self, sqlite_store: SqliteTradePlanStore) -> None:
        """Un plan ré-upserté est mis à jour ET placé en fin de liste (ordre identique au JSON store)."""
        p1 = _plan("P1", "AAPL")
        p2 = _plan("P2", "MSFT")
        sqlite_store.upsert(p1)
        sqlite_store.upsert(p2)
        # Re-upsert P1 avec une nouvelle valeur → doit aller en fin
        p1_updated = p1.model_copy(update={"high_watermark": 999.0})
        sqlite_store.upsert(p1_updated)

        plans = sqlite_store.open_plans()
        assert len(plans) == 2
        assert plans[0].id == "P2"  # P2 reste avant
        assert plans[1].id == "P1"  # P1 déplacé en fin
        assert plans[1].high_watermark == pytest.approx(999.0)

    def test_close_removes_plan(self, sqlite_store: SqliteTradePlanStore) -> None:
        p1 = _plan("P1", "AAPL")
        p2 = _plan("P2", "MSFT")
        sqlite_store.upsert(p1)
        sqlite_store.upsert(p2)
        sqlite_store.close("P1")

        plans = sqlite_store.open_plans()
        assert len(plans) == 1
        assert plans[0].id == "P2"

    def test_close_symbol_removes_all_for_symbol(self, sqlite_store: SqliteTradePlanStore) -> None:
        p1 = _plan("AAPL-1", "AAPL")
        p2 = _plan("AAPL-2", "AAPL")  # 2nd plan même symbole
        p3 = _plan("MSFT-1", "MSFT")
        sqlite_store.upsert(p1)
        sqlite_store.upsert(p2)
        sqlite_store.upsert(p3)
        sqlite_store.close_symbol("AAPL")

        plans = sqlite_store.open_plans()
        assert len(plans) == 1
        assert plans[0].id == "MSFT-1"

    def test_clear_removes_all(self, sqlite_store: SqliteTradePlanStore) -> None:
        sqlite_store.upsert(_plan("P1", "AAPL"))
        sqlite_store.upsert(_plan("P2", "MSFT"))
        sqlite_store.clear()
        assert sqlite_store.open_plans() == []


# ---------------------------------------------------------------------------
# sync_symbol_quantity
# ---------------------------------------------------------------------------


class TestSyncSymbolQuantity:
    def test_sync_zero_closes_symbol(self, sqlite_store: SqliteTradePlanStore) -> None:
        sqlite_store.upsert(_plan("AAPL-1", "AAPL"))
        sqlite_store.sync_symbol_quantity("AAPL", 0.0)
        assert sqlite_store.open_plans() == []

    def test_sync_negative_closes_symbol(self, sqlite_store: SqliteTradePlanStore) -> None:
        sqlite_store.upsert(_plan("AAPL-1", "AAPL"))
        sqlite_store.sync_symbol_quantity("AAPL", -5.0)
        assert sqlite_store.open_plans() == []

    def test_sync_partial_rescales_remaining(self, sqlite_store: SqliteTradePlanStore) -> None:
        plan = _plan("AAPL-1", "AAPL", quantity=10.0, remaining_quantity=10.0)
        sqlite_store.upsert(plan)
        sqlite_store.sync_symbol_quantity("AAPL", 6.0)

        updated = sqlite_store.open_plans()[0]
        assert updated.remaining_quantity == pytest.approx(6.0, rel=1e-6)

    def test_sync_rescales_unfilled_tp_quantities(self, sqlite_store: SqliteTradePlanStore) -> None:
        """TP non rempli est rescalé ; TP rempli est conservé tel quel."""
        plan = _plan(
            "AAPL-1",
            "AAPL",
            quantity=10.0,
            remaining_quantity=10.0,
            take_profits=[
                TakeProfit(name="tp1", price=110.0, fraction=0.5, quantity=5.0, after_fill=""),
                TakeProfit(name="tp2", price=120.0, fraction=0.5, quantity=5.0, after_fill=""),
            ],
            filled_take_profits=["tp1"],  # tp1 rempli → conservé
        )
        sqlite_store.upsert(plan)
        sqlite_store.sync_symbol_quantity("AAPL", 5.0)  # ratio = 0.5

        updated = sqlite_store.open_plans()[0]
        assert updated.remaining_quantity == pytest.approx(5.0)
        tp1 = next(tp for tp in updated.take_profits if tp.name == "tp1")
        tp2 = next(tp for tp in updated.take_profits if tp.name == "tp2")
        # tp1 rempli → quantité inchangée
        assert tp1.quantity == pytest.approx(5.0)
        # tp2 non rempli → rescalé
        assert tp2.quantity == pytest.approx(2.5)

    def test_sync_unknown_symbol_noop(self, sqlite_store: SqliteTradePlanStore) -> None:
        """Sync sur un symbole inconnu avec remaining > 0 → no-op, liste inchangée."""
        sqlite_store.upsert(_plan("MSFT-1", "MSFT"))
        sqlite_store.sync_symbol_quantity("AAPL", 5.0)
        plans = sqlite_store.open_plans()
        assert len(plans) == 1
        assert plans[0].id == "MSFT-1"

    def test_sync_total_remaining_zero_closes(self, sqlite_store: SqliteTradePlanStore) -> None:
        """Si total_remaining == 0 côté DB, ferme le symbole même si arg > 0."""
        # Créer un plan avec remaining=0 directement
        plan = _plan("AAPL-1", "AAPL", quantity=10.0, remaining_quantity=0.0)
        sqlite_store.upsert(plan)
        sqlite_store.sync_symbol_quantity("AAPL", 5.0)
        assert sqlite_store.open_plans() == []

    def test_sync_multiple_plans_same_symbol(self, sqlite_store: SqliteTradePlanStore) -> None:
        """Plusieurs plans pour le même symbole → tous rescalés proportionnellement."""
        p1 = _plan("AAPL-1", "AAPL", quantity=6.0, remaining_quantity=6.0)
        p2 = _plan("AAPL-2", "AAPL", quantity=4.0, remaining_quantity=4.0)
        sqlite_store.upsert(p1)
        sqlite_store.upsert(p2)
        sqlite_store.sync_symbol_quantity("AAPL", 5.0)  # total=10 → ratio=0.5

        plans = sqlite_store.open_plans()
        assert len(plans) == 2
        assert plans[0].remaining_quantity == pytest.approx(3.0)
        assert plans[1].remaining_quantity == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Opérations SQLite (séquence complète)
# ---------------------------------------------------------------------------


class TestSqliteSequence:
    def _run_sequence(self, store: SqliteTradePlanStore) -> list[list[dict]]:
        """Exécute la séquence et retourne les snapshots open_plans après chaque op."""
        snapshots: list[list[dict]] = []

        def snap():
            snapshots.append([_dump(p) for p in store.open_plans()])

        p1 = _plan("AAPL-1", "AAPL", quantity=10.0)
        p2 = _plan("MSFT-1", "MSFT", quantity=8.0)
        p3 = _plan("TSLA-1", "TSLA", quantity=5.0)

        # upsert ×3
        store.upsert(p1)
        snap()
        store.upsert(p2)
        snap()
        store.upsert(p3)
        snap()

        # re-upsert p1 (modifié) → déplacé en fin
        store.upsert(p1.model_copy(update={"high_watermark": 110.0}))
        snap()

        # close plan
        store.close("MSFT-1")
        snap()

        # close_symbol (rien pour MSFT après, TSLA reste)
        store.close_symbol("TSLA")
        snap()

        # sync partiel AAPL
        store.sync_symbol_quantity("AAPL", 6.0)
        snap()

        # sync→0 AAPL
        store.sync_symbol_quantity("AAPL", 0.0)
        snap()

        # clear dans séquence parité (après ajout d'un plan)
        store.upsert(_plan("GOOG-1", "GOOG", quantity=3.0))
        snap()
        store.clear()
        snap()  # doit être []

        return snapshots

    def test_sequence_complete(
        self,
        sqlite_store: SqliteTradePlanStore,
    ) -> None:
        sqlite_snaps = self._run_sequence(sqlite_store)

        assert sqlite_snaps[-1] == []
        assert [snap[0]["id"] for snap in sqlite_snaps[:4]] == [
            "AAPL-1",
            "AAPL-1",
            "AAPL-1",
            "MSFT-1",
        ]

    def test_sync_with_take_profits(
        self,
        sqlite_store: SqliteTradePlanStore,
    ) -> None:
        """sync avec TP remplis et non remplis."""
        plan = _plan(
            "AAPL-1",
            "AAPL",
            quantity=10.0,
            remaining_quantity=10.0,
            take_profits=[
                TakeProfit(name="tp1", price=110.0, fraction=0.3, quantity=3.0, after_fill=""),
                TakeProfit(name="tp2", price=120.0, fraction=0.7, quantity=7.0, after_fill=""),
            ],
            filled_take_profits=["tp1"],
        )

        sqlite_store.upsert(plan)
        sqlite_store.sync_symbol_quantity("AAPL", 5.0)

        sqlite_plans = [_dump(p) for p in sqlite_store.open_plans()]
        assert sqlite_plans[0]["remaining_quantity"] == pytest.approx(5.0)
        assert sqlite_plans[0]["take_profits"][0]["quantity"] == pytest.approx(3.0)
        assert sqlite_plans[0]["take_profits"][1]["quantity"] == pytest.approx(3.5)


# ---------------------------------------------------------------------------
# Shadow supprimé
# ---------------------------------------------------------------------------


class TestNoShadow:
    def test_constructor_rejects_json_path(self, db: StateDb, tmp_path: Path) -> None:
        import_trade_plans_from_json(db, tmp_path / "absent.json")

        with pytest.raises(TypeError):
            SqliteTradePlanStore(db, json_path=tmp_path / "trade_plans.json")  # type: ignore[call-arg]

    def test_regenerate_shadow_api_removed(self) -> None:
        assert not hasattr(SqliteTradePlanStore, "regenerate_shadow")

    def test_no_shadow_written_after_mutations(
        self, sqlite_store: SqliteTradePlanStore, tmp_path: Path
    ) -> None:
        shadow_path = tmp_path / "trade_plans.json"

        sqlite_store.upsert(_plan("AAPL-1", "AAPL", quantity=10.0, remaining_quantity=10.0))
        sqlite_store.upsert(_plan("AAPL-2", "AAPL"))
        sqlite_store.sync_symbol_quantity("AAPL", 6.0)
        sqlite_store.close("AAPL-2")
        sqlite_store.close_symbol("AAPL")
        sqlite_store.clear()

        assert not shadow_path.exists()


# ---------------------------------------------------------------------------
# Parité non-finis (NaN / Inf)
# ---------------------------------------------------------------------------


class TestNonFiniteParity:
    """Non-finis scalaires (NaN/Inf) → None après round-trip SQLite.

    SQLite : float('nan') en colonne REAL → NULL → None (sqlite3 adapter).
    C'est une normalisation fonctionnelle, pas un round-trip byte-exact.
    """

    def test_non_finite_scalars_parity(
        self,
        sqlite_store: SqliteTradePlanStore,
    ) -> None:
        """reference_volatility=NaN et trailing_stop.trail_value=Inf → None."""

        plan = TradePlan(
            id="NAN-1",
            symbol="AAPL",
            side="LONG",
            quantity=10.0,
            remaining_quantity=10.0,
            entry_price=100.0,
            opened_at="2026-07-01T10:00:00+00:00",
            reference_volatility=float("nan"),  # NaN → SQLite NULL → None
            hard_stop_price=95.0,
            take_profits=[],
            trailing_stop=TrailingStop(
                enabled_after=None,
                trail_type="percent",
                trail_value=float("inf"),  # Inf → _trailing_from_dict → None
                trail_floored=False,
            ),
            max_hold_minutes=120.0,
            high_watermark=105.0,
            low_watermark=98.0,
            filled_take_profits=[],
            profit_protection=None,
            exit_watch=None,
            llm_provider="openai",
            llm_model="gpt-4o",
            llm_fallback_reason=None,
            llm_confidence=0.85,
            last_llm_review=None,
            entry_thesis="Test non-fini",
            entry_decision_id="dec-nan",
            entry_context=None,
        )

        sqlite_store.upsert(plan)

        sqlite_plans = sqlite_store.open_plans()

        assert len(sqlite_plans) == 1

        assert sqlite_plans[0].reference_volatility is None, "SQLite: NaN doit devenir None"
        assert sqlite_plans[0].trailing_stop is None, "SQLite: trail_value=Inf → trailing_stop None"


# ---------------------------------------------------------------------------
# Rollback transaction
# ---------------------------------------------------------------------------


class TestRollback:
    def test_sync_symbol_quantity_rollback_on_exception(
        self, sqlite_store: SqliteTradePlanStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exception mid-transaction dans sync_symbol_quantity → rollback, état inchangé."""
        from trader.state_db import trade_plan_store as tps_module

        p1 = _plan("AAPL-1", "AAPL", quantity=6.0, remaining_quantity=6.0)
        p2 = _plan("AAPL-2", "AAPL", quantity=4.0, remaining_quantity=4.0)
        sqlite_store.upsert(p1)
        sqlite_store.upsert(p2)

        original_plan_to_columns = tps_module.plan_to_columns
        call_count = [0]

        def plan_to_columns_fail_on_second(plan, seq):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise RuntimeError("injection erreur rollback test")
            return original_plan_to_columns(plan, seq)

        monkeypatch.setattr(tps_module, "plan_to_columns", plan_to_columns_fail_on_second)

        with pytest.raises(RuntimeError, match="injection erreur rollback test"):
            sqlite_store.sync_symbol_quantity("AAPL", 5.0)  # total=10 → ratio=0.5

        # Transaction rollbackée : les 2 plans sont inchangés
        plans = sqlite_store.open_plans()
        assert len(plans) == 2
        assert plans[0].remaining_quantity == pytest.approx(6.0)
        assert plans[1].remaining_quantity == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# Factory make_trade_plan_store
# ---------------------------------------------------------------------------


class TestMakeTradePlanStore:
    def test_json_backend_fast_fails(self, tmp_path: Path) -> None:
        from trader.state_db.broker_factory import make_trade_plan_store

        with pytest.raises(NotImplementedError, match="trade plan JSON backend supprimé"):
            make_trade_plan_store(state_dir=tmp_path, backend="json")

    def test_sqlite_backend_returns_sqlite_store(self, tmp_path: Path) -> None:
        from trader.state_db.broker_factory import make_trade_plan_store

        store = make_trade_plan_store(state_dir=tmp_path, backend="sqlite")
        assert isinstance(store, SqliteTradePlanStore)

    def test_sqlite_backend_case_insensitive(self, tmp_path: Path) -> None:
        from trader.state_db.broker_factory import make_trade_plan_store

        store = make_trade_plan_store(state_dir=tmp_path, backend="SQLite")
        assert isinstance(store, SqliteTradePlanStore)

    def test_unknown_backend_raises_value_error(self, tmp_path: Path) -> None:
        from trader.state_db.broker_factory import make_trade_plan_store

        with pytest.raises(ValueError, match="backend d'état inconnu"):
            make_trade_plan_store(state_dir=tmp_path, backend="bogus")

    def test_sqlite_store_functional(self, tmp_path: Path) -> None:
        from trader.state_db.broker_factory import make_trade_plan_store

        store = make_trade_plan_store(state_dir=tmp_path, backend="sqlite")
        plan = _plan("P1", "AAPL")
        store.upsert(plan)
        assert len(store.open_plans()) == 1

    def test_sqlite_imports_existing_json(self, tmp_path: Path) -> None:
        """Si trade_plans.json existe, il est importé dans SQLite au boot."""
        from trader.state_db.broker_factory import make_trade_plan_store

        # Créer un trade_plans.json pré-existant
        json_path = tmp_path / "trade_plans.json"
        json_path.write_text(
            json.dumps({"plans": [_dump(_plan("OLD-P1", "AAPL"))]}, indent=2)
        )

        store = make_trade_plan_store(state_dir=tmp_path, backend="sqlite")
        plans = store.open_plans()
        assert len(plans) == 1
        assert plans[0].id == "OLD-P1"

    def test_sqlite_does_not_regenerate_trade_plan_shadow_at_boot(self, tmp_path: Path) -> None:
        """Le boot SQLite n'écrit plus trade_plans.json depuis les tables."""
        from trader.state_db.broker_factory import make_trade_plan_store

        json_path = tmp_path / "trade_plans.json"
        json_path.write_text(
            json.dumps({"plans": [_dump(_plan("P1", "AAPL"))]}, indent=2)
        )

        make_trade_plan_store(state_dir=tmp_path, backend="sqlite")
        json_path.unlink()

        store = make_trade_plan_store(state_dir=tmp_path, backend="sqlite")

        assert not json_path.exists()
        assert [plan.id for plan in store.open_plans()] == ["P1"]
