"""Tests TDD — compare_backends (trader/state_db/compare.py).

Couverture :
    FIX 1 — compare pur (read-only) :
        - RuntimeError si casys.db absent
        - RuntimeError si sentinels manquants
        - Compare n'importe jamais (vérifié par structure du résultat)

    FIX 2 — fidélité :
        - État identique (JSON shadow ↔ SQLite) → identical=True, exit 0
        - Divergence cash broker (JSON modifié post-import) → identical=False
        - Divergence scheduler.symbols : même symbole, heure différente → identical=False
        - Divergence trade_plans : ordre différent → identical=False
        - Divergence watches : expires_at différent côté JSON → identical=False
        - Divergence scheduler.symbols_with_wake (symbole absent côté SQLite) → identical=False
        - CLI exit code : 0 si identical, 1 sinon
        - Structure du dict retourné
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trader.state_db.connection import (
    StateDb,
    _DB_REGISTRY,
    _DB_REGISTRY_LOCK,
    close_all_state_dbs,
    open_state_db,
)
from trader.state_db.broker_factory import bootstrap_state_backend
from trader.state_db.compare import compare_backends


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clear_registry_for(paths: list[Path]) -> None:
    with _DB_REGISTRY_LOCK:
        for p in paths:
            _DB_REGISTRY.pop(str(p.resolve()), None)


def _bootstrap(tmp_path: Path, cash: float = 50_000.0) -> None:
    """Bootstrap un state_dir SQLite depuis zéro avec un broker.json initial."""
    broker_json = {
        "cash": cash,
        "positions": {},
        "fills": [],
    }
    (tmp_path / "broker.json").write_text(json.dumps(broker_json))

    db_path = tmp_path / "casys.db"
    _clear_registry_for([db_path])

    bootstrap_state_backend(
        state_dir=tmp_path,
        starting_cash=cash,
        commission_model=None,
        backend="sqlite",
    )


def _minimal_plan_dict(plan_id: str, symbol: str = "AAPL.US") -> dict:
    """Dict minimal valide pour TradePlan."""
    return {
        "id": plan_id,
        "symbol": symbol,
        "side": "LONG",
        "quantity": 10.0,
        "remaining_quantity": 10.0,
        "entry_price": 100.0,
        "opened_at": "2026-07-01T10:00:00+00:00",
        "reference_volatility": None,
        "hard_stop_price": 90.0,
        "take_profits": [],
        "trailing_stop": None,
        "max_hold_minutes": None,
        "high_watermark": None,
        "low_watermark": None,
        "filled_take_profits": [],
        "profit_protection": None,
        "exit_watch": None,
        "llm_provider": None,
        "llm_model": None,
        "llm_fallback_reason": None,
        "llm_confidence": None,
        "last_llm_review": None,
        "entry_thesis": None,
        "entry_decision_id": None,
        "entry_context": None,
    }


# ---------------------------------------------------------------------------
# FIX 1 — compare pur : guards
# ---------------------------------------------------------------------------


def test_compare_raises_if_db_absent(tmp_path: Path) -> None:
    """compare_backends lève RuntimeError si casys.db est absent."""
    # Fichiers JSON présents mais pas de casys.db
    (tmp_path / "broker.json").write_text(
        json.dumps({"cash": 100.0, "positions": {}, "fills": []})
    )
    (tmp_path / "trade_plans.json").write_text(json.dumps({"plans": []}))
    (tmp_path / "scheduler.json").write_text(
        json.dumps({"default_next_wake": None, "symbols": {}, "stale_streaks": {}, "indicator_watches": {}})
    )

    with pytest.raises(RuntimeError, match="casys.db absent"):
        compare_backends(tmp_path)


def test_compare_raises_if_sentinel_missing(tmp_path: Path) -> None:
    """compare_backends lève RuntimeError si les sentinels state_imports sont manquants."""
    db_path = tmp_path / "casys.db"
    _clear_registry_for([db_path])

    # Crée le fichier DB sans tables ni sentinels
    db = StateDb(db_path)
    db.close()
    _clear_registry_for([db_path])

    # Fichiers JSON présents
    (tmp_path / "broker.json").write_text(
        json.dumps({"cash": 100.0, "positions": {}, "fills": []})
    )
    (tmp_path / "trade_plans.json").write_text(json.dumps({"plans": []}))
    (tmp_path / "scheduler.json").write_text(
        json.dumps({"default_next_wake": None, "symbols": {}, "stale_streaks": {}, "indicator_watches": {}})
    )

    try:
        with pytest.raises(RuntimeError, match="non initialisé"):
            compare_backends(tmp_path)
    finally:
        close_all_state_dbs()
        _clear_registry_for([db_path])


# ---------------------------------------------------------------------------
# FIX 2 — État identique → identical=True
# ---------------------------------------------------------------------------


def test_compare_identical_states(tmp_path: Path) -> None:
    """JSON shadow et SQLite identiques après bootstrap → identical=True."""
    _bootstrap(tmp_path, cash=50_000.0)
    try:
        result = compare_backends(tmp_path)
    finally:
        close_all_state_dbs()
        _clear_registry_for([tmp_path / "casys.db"])

    assert result["identical"] is True, (
        f"États identiques après bootstrap : identical doit être True. result={result!r}"
    )
    assert result["broker"]["cash"]["identical"] is True
    assert result["broker"]["positions_diff"] == []
    assert result["broker"]["fills_diff"] == {}
    assert result["trade_plans"]["diff"] == []
    assert result["scheduler"]["wakes_diff"] == []
    assert result["scheduler"]["watches_diff"] == []
    assert result["scheduler"]["stale_diff"] == []


# ---------------------------------------------------------------------------
# FIX 2 — Divergence broker.cash → identical=False
# ---------------------------------------------------------------------------


def test_compare_divergent_broker_cash(tmp_path: Path) -> None:
    """Modification du broker.json post-import → cash diverge JSON vs SQLite."""
    _bootstrap(tmp_path, cash=50_000.0)

    # Modifier le shadow JSON après que SQLite a déjà été importé
    # L'import SQLite (sentinel posé) ne sera pas rejouée → divergence
    broker_json_path = tmp_path / "broker.json"
    shadow = json.loads(broker_json_path.read_text())
    shadow["cash"] = 99_999.99
    broker_json_path.write_text(json.dumps(shadow))

    try:
        result = compare_backends(tmp_path)
    finally:
        close_all_state_dbs()
        _clear_registry_for([tmp_path / "casys.db"])

    assert result["identical"] is False, (
        "Cash différent JSON vs SQLite → identical doit être False"
    )
    assert result["broker"]["cash"]["identical"] is False
    assert abs(result["broker"]["cash"]["json"] - 99_999.99) < 1e-6
    assert abs(result["broker"]["cash"]["sqlite"] - 50_000.0) < 1e-6

    # Aucune divergence sur plans ou scheduler (non modifiés)
    assert result["trade_plans"]["diff"] == []
    assert result["scheduler"]["wakes_diff"] == []


# ---------------------------------------------------------------------------
# FIX 2 — Divergence scheduler.symbols : heure différente (faux négatif corrigé)
# ---------------------------------------------------------------------------


def test_compare_wake_time_divergence(tmp_path: Path) -> None:
    """Même symbole mais heure de réveil différente → identical=False.

    Faux négatif de l'ancienne version (comparait uniquement les ensembles de
    symboles, pas les heures). La nouvelle version compare {symbol: heure}.
    """
    _bootstrap(tmp_path, cash=50_000.0)

    db_path = tmp_path / "casys.db"
    db = open_state_db(db_path)

    from trader.state_db.scheduler_store import SqliteScheduler

    sched = SqliteScheduler(db, json_path=tmp_path / "scheduler.json")
    sched.set_symbol_next_wake("AAPL.US", "2026-07-04T10:00:00+00:00")
    # SQLite = 10:00, shadow scheduler.json aussi = 10:00

    # Modifier manuellement scheduler.json pour avoir une heure différente
    sched_path = tmp_path / "scheduler.json"
    sched_data = json.loads(sched_path.read_text())
    sched_data["symbols"]["AAPL.US"] = "2026-07-04T11:00:00+00:00"  # 11:00 ≠ 10:00
    sched_path.write_text(json.dumps(sched_data))

    try:
        result = compare_backends(tmp_path)
    finally:
        close_all_state_dbs()
        _clear_registry_for([db_path])

    assert result["identical"] is False, (
        "Même symbole, heure différente → identical doit être False"
    )
    wakes_field = next(
        (d for d in result["scheduler"]["wakes_diff"] if d["field"] == "symbols_with_wake"),
        None,
    )
    assert wakes_field is not None, "wakes_diff doit contenir symbols_with_wake"
    assert "AAPL.US" in wakes_field["json"]
    assert "AAPL.US" in wakes_field["sqlite"]
    # Les heures doivent différer
    assert wakes_field["json"]["AAPL.US"] != wakes_field["sqlite"]["AAPL.US"]


# ---------------------------------------------------------------------------
# FIX 2 — Divergence trade_plans : ordre différent (faux négatif corrigé)
# ---------------------------------------------------------------------------


def test_compare_plan_order_divergence(tmp_path: Path) -> None:
    """Mêmes plans mais dans un ordre différent → identical=False.

    Faux négatif de l'ancienne version (comparait par id, ignorait l'ordre/seq).
    La nouvelle version compare la liste ordonnée position par position.
    """
    _bootstrap(tmp_path, cash=50_000.0)

    db_path = tmp_path / "casys.db"
    db = open_state_db(db_path)

    from trader.state_db.trade_plan_store import SqliteTradePlanStore
    from trader.planning.trade_plan import trade_plan_from_dict

    plans_store = SqliteTradePlanStore(db, json_path=tmp_path / "trade_plans.json")
    plan_a = trade_plan_from_dict(_minimal_plan_dict("plan-A", "AAPL.US"))
    plan_b = trade_plan_from_dict(_minimal_plan_dict("plan-B", "BN.PA"))
    plans_store.upsert(plan_a)  # seq=1
    plans_store.upsert(plan_b)  # seq=2
    # SQLite : A, B ; shadow JSON : A, B

    # Inverser l'ordre dans trade_plans.json
    plans_path = tmp_path / "trade_plans.json"
    plans_data = json.loads(plans_path.read_text())
    plans_data["plans"] = list(reversed(plans_data["plans"]))  # B, A
    plans_path.write_text(json.dumps(plans_data))

    try:
        result = compare_backends(tmp_path)
    finally:
        close_all_state_dbs()
        _clear_registry_for([db_path])

    assert result["identical"] is False, (
        "Ordre différent des plans → identical doit être False"
    )
    assert result["trade_plans"]["diff"], "trade_plans.diff doit être non vide"
    # Position 0 doit différer (JSON:B vs SQLite:A)
    first_diff = result["trade_plans"]["diff"][0]
    assert first_diff["position"] == 0
    assert first_diff["json"]["symbol"] == "BN.PA"
    assert first_diff["sqlite"]["symbol"] == "AAPL.US"


# ---------------------------------------------------------------------------
# FIX 2 — Divergence watches : expires_at différent (faux négatif corrigé)
# ---------------------------------------------------------------------------


def test_compare_watch_expires_divergence(tmp_path: Path) -> None:
    """Watch avec expires_at différent entre JSON et SQLite → identical=False.

    Faux négatif corrigé : la comparaison utilise le champ expires_at normalisé
    de la colonne DB (pas seulement watch_json), et couvre toutes les watches (y
    compris celles dont expires_at est dans le futur proche ou le passé).
    """
    _bootstrap(tmp_path, cash=50_000.0)

    db_path = tmp_path / "casys.db"
    db = open_state_db(db_path)

    from trader.state_db.scheduler_store import SqliteScheduler

    sched = SqliteScheduler(db, json_path=tmp_path / "scheduler.json")

    # Crée une watch avec expires_at dans le futur (T1)
    future1 = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    watch = {
        "id": "w-test-001",
        "symbol": "AAPL.US",
        "on_trigger": "WAKE",
        "expires_at": future1,
    }
    sched.set_symbol_indicator_watch("AAPL.US", watch)
    # SQLite colonne expires_at = canonical(T1) ; watch_json = {expires_at: T1}
    # Shadow scheduler.json synchronisé avec T1

    # Modifier scheduler.json : changer expires_at de la watch vers T2 (différent)
    future2 = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
    sched_path = tmp_path / "scheduler.json"
    sched_data = json.loads(sched_path.read_text())
    watch_data = sched_data["indicator_watches"]["w-test-001"]
    watch_data["expires_at"] = future2
    sched_data["indicator_watches"]["w-test-001"] = watch_data
    sched_path.write_text(json.dumps(sched_data))

    try:
        result = compare_backends(tmp_path)
    finally:
        close_all_state_dbs()
        _clear_registry_for([db_path])

    assert result["identical"] is False, (
        "expires_at différent dans la watch → identical doit être False"
    )
    assert result["scheduler"]["watches_diff"], "watches_diff doit être non vide"
    diff = result["scheduler"]["watches_diff"][0]
    assert diff["id"] == "w-test-001"
    assert diff["json"] is not None
    assert diff["sqlite"] is not None
    assert diff["json"]["expires_at"] != diff["sqlite"]["expires_at"]


# ---------------------------------------------------------------------------
# FIX 2 — Watch EXPIRÉE diverge entre JSON et SQLite → identical=False
# ---------------------------------------------------------------------------


def test_compare_expired_watch_divergence(tmp_path: Path) -> None:
    """Watch déjà expirée absente côté JSON mais présente côté SQLite → identical=False.

    Cas non couvert par l'ancienne version (ne comparait que les watches actives) :
    le daemon consomme aussi les watches expirées (pop_expired_indicator_watches),
    donc une divergence sur une expirée doit être détectée.
    """
    _bootstrap(tmp_path, cash=50_000.0)

    db_path = tmp_path / "casys.db"
    db = open_state_db(db_path)

    from trader.state_db.scheduler_store import SqliteScheduler

    sched = SqliteScheduler(db, json_path=tmp_path / "scheduler.json")

    # Crée une watch avec expires_at dans le PASSÉ (déjà expirée)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    watch = {
        "id": "w-expired-001",
        "symbol": "BN.PA",
        "on_trigger": "WAKE",
        "expires_at": past,
    }
    sched.set_symbol_indicator_watch("BN.PA", watch)
    # SQLite a la watch expirée ; shadow JSON aussi

    # Supprimer la watch du scheduler.json pour simuler une divergence
    sched_path = tmp_path / "scheduler.json"
    sched_data = json.loads(sched_path.read_text())
    del sched_data["indicator_watches"]["w-expired-001"]
    sched_path.write_text(json.dumps(sched_data))

    try:
        result = compare_backends(tmp_path)
    finally:
        close_all_state_dbs()
        _clear_registry_for([db_path])

    assert result["identical"] is False, (
        "Watch expirée absente du JSON mais présente dans SQLite → identical=False"
    )
    assert result["scheduler"]["watches_diff"], "watches_diff doit être non vide"
    diff = result["scheduler"]["watches_diff"][0]
    assert diff["id"] == "w-expired-001"
    assert diff["json"] is None, "Watch absente côté JSON → json=None"
    assert diff["sqlite"] is not None, "Watch présente côté SQLite"


# ---------------------------------------------------------------------------
# FIX 2 — Divergence scheduler.symbols_with_wake (symbole côté JSON uniquement)
# ---------------------------------------------------------------------------


def test_compare_divergent_scheduler_wakes(tmp_path: Path) -> None:
    """Modification de scheduler.json (symbols) post-import → wakes_diff non vide."""
    _bootstrap(tmp_path, cash=50_000.0)

    # Ajouter un symbol wake dans le shadow scheduler.json APRÈS import
    sched_json_path = tmp_path / "scheduler.json"
    sched = json.loads(sched_json_path.read_text())
    sched["symbols"]["AAPL.US"] = "2026-07-04T10:00:00+00:00"
    sched_json_path.write_text(json.dumps(sched))

    try:
        result = compare_backends(tmp_path)
    finally:
        close_all_state_dbs()
        _clear_registry_for([tmp_path / "casys.db"])

    assert result["identical"] is False, (
        "symbols_with_wake divergent → identical doit être False"
    )
    assert result["scheduler"]["wakes_diff"], (
        "wakes_diff doit être non vide quand symbols_with_wake divergent"
    )
    # Le diff doit mentionner AAPL.US (côté JSON) vs dict vide (côté SQLite)
    wakes_field = next(
        (d for d in result["scheduler"]["wakes_diff"] if d["field"] == "symbols_with_wake"),
        None,
    )
    assert wakes_field is not None, "Un wakes_diff avec field=symbols_with_wake attendu"
    assert "AAPL.US" in wakes_field["json"]


# ---------------------------------------------------------------------------
# CLI exit code 0 si identical
# ---------------------------------------------------------------------------


def test_cli_exit_0_when_identical(tmp_path: Path) -> None:
    """CLI python -m trader.state_db.compare exit 0 si identical."""
    _bootstrap(tmp_path, cash=50_000.0)
    # Libère le registre pour le sous-processus
    close_all_state_dbs()
    _clear_registry_for([tmp_path / "casys.db"])

    proc = subprocess.run(
        [sys.executable, "-m", "trader.state_db.compare", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"CLI exit code doit être 0 si identical. stderr={proc.stderr!r}"
    )
    output = json.loads(proc.stdout)
    assert output["identical"] is True


# ---------------------------------------------------------------------------
# CLI exit code 1 si divergence
# ---------------------------------------------------------------------------


def test_cli_exit_1_when_divergent(tmp_path: Path) -> None:
    """CLI python -m trader.state_db.compare exit 1 si divergence."""
    _bootstrap(tmp_path, cash=50_000.0)

    # Modifier le shadow
    broker_json_path = tmp_path / "broker.json"
    shadow = json.loads(broker_json_path.read_text())
    shadow["cash"] = 1.0
    broker_json_path.write_text(json.dumps(shadow))

    # Libère le registre pour le sous-processus
    close_all_state_dbs()
    _clear_registry_for([tmp_path / "casys.db"])

    proc = subprocess.run(
        [sys.executable, "-m", "trader.state_db.compare", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1, (
        f"CLI exit code doit être 1 si divergence. stdout={proc.stdout!r}"
    )
    output = json.loads(proc.stdout)
    assert output["identical"] is False
    assert output["broker"]["cash"]["identical"] is False


# ---------------------------------------------------------------------------
# Structure du dict retourné
# ---------------------------------------------------------------------------


def test_compare_result_structure(tmp_path: Path) -> None:
    """compare_backends retourne un dict avec la structure attendue."""
    _bootstrap(tmp_path, cash=10_000.0)
    try:
        result = compare_backends(tmp_path)
    finally:
        close_all_state_dbs()
        _clear_registry_for([tmp_path / "casys.db"])

    # Structure broker
    assert "broker" in result
    assert "cash" in result["broker"]
    assert "identical" in result["broker"]["cash"]
    assert "json" in result["broker"]["cash"]
    assert "sqlite" in result["broker"]["cash"]
    assert "positions_diff" in result["broker"]
    assert isinstance(result["broker"]["positions_diff"], list)
    assert "fills_diff" in result["broker"]

    # Structure trade_plans
    assert "trade_plans" in result
    assert "diff" in result["trade_plans"]
    assert isinstance(result["trade_plans"]["diff"], list)

    # Structure scheduler
    assert "scheduler" in result
    assert "wakes_diff" in result["scheduler"]
    assert "watches_diff" in result["scheduler"]
    assert "stale_diff" in result["scheduler"]

    # Clé identical globale
    assert "identical" in result
    assert isinstance(result["identical"], bool)
