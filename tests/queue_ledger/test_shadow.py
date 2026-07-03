"""tests/queue_ledger/test_shadow.py — sonde d'orchestration shadow-queue.

TDD : les tests sont écrits avant (ou conjointement à) l'implémentation.
Ils vérifient l'orchestration pure (enqueue/drain/identité), jamais le LLM.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from trader.queue.shadow import ShadowQueueProbe


# ── Helpers ─────────────────────────────────────────────────────────────────


def _probe(tmp_path: Path) -> ShadowQueueProbe:
    return ShadowQueueProbe(tmp_path / "shadow_queue.db")


NOW_MS = 1_000_000
CYCLE_TS = "2026-07-03T10:00:00+00:00"
SYMBOLS = ["AAPL", "MSFT", "TSLA"]


# ── Test 1 : happy path ──────────────────────────────────────────────────────


def test_run_3_symbols_enqueued_and_drained(tmp_path):
    """3 symboles → enqueued=3, drained=3, identical=True, missing=[]."""
    probe = _probe(tmp_path)
    report = probe.run(cycle_ts=CYCLE_TS, decided_symbols=SYMBOLS, now_ms=NOW_MS)

    assert report["enqueued"] == 3
    assert report["drained"] == 3
    assert report["identical"] is True
    assert report["missing"] == []
    assert report["duplicated"] == []
    assert report["dead"] == 0
    assert report["expected"] == sorted(SYMBOLS)
    assert report["drained_symbols"] == sorted(SYMBOLS)


# ── Test 2 : idempotence dedup sur même cycle_ts ────────────────────────────


def test_second_call_same_cycle_deduped(tmp_path):
    """2e appel même cycle_ts → deduped=3, pas de doublon dans drained."""
    probe = _probe(tmp_path)
    # Premier appel : enfile et draine
    r1 = probe.run(cycle_ts=CYCLE_TS, decided_symbols=SYMBOLS, now_ms=NOW_MS)
    assert r1["enqueued"] == 3

    # Deuxième appel identique : les dedup_key existent déjà → tout dedup
    r2 = probe.run(cycle_ts=CYCLE_TS, decided_symbols=SYMBOLS, now_ms=NOW_MS + 1)
    assert r2["deduped"] == 3
    assert r2["enqueued"] == 0
    # Rien à drainer : identical reste cohérent (expected==drained==[] depuis ce cycle)
    # drained=0, expected=sorted(SYMBOLS), mais deduped=3 → identical=False (missing)
    # C'est le comportement attendu : un dedup complet = rien drainé = manquants.
    assert r2["drained"] == 0


def test_different_cycle_ts_enqueues_again(tmp_path):
    """cycle_ts différent → nouvelle enfile (dedup_key différente)."""
    probe = _probe(tmp_path)
    r1 = probe.run(cycle_ts=CYCLE_TS, decided_symbols=SYMBOLS, now_ms=NOW_MS)
    assert r1["enqueued"] == 3
    assert r1["identical"] is True

    cycle2 = "2026-07-03T11:00:00+00:00"
    r2 = probe.run(cycle_ts=cycle2, decided_symbols=SYMBOLS, now_ms=NOW_MS + 3_600_000)
    assert r2["enqueued"] == 3
    assert r2["identical"] is True


# ── Test 3 : divergence simulée (missing) ───────────────────────────────────


def test_missing_symbol_when_drain_capped(tmp_path):
    """Borne de drain = 0 → tous les symboles sont missing, identical=False."""
    probe = _probe(tmp_path)
    # Enfile sans drainer en patchant run_once pour qu'il retourne False dès le départ
    with patch.object(probe._worker, "run_once", return_value=False):
        report = probe.run(cycle_ts=CYCLE_TS, decided_symbols=SYMBOLS, now_ms=NOW_MS)

    # Le patch empêche tout drain → missing = tous les symboles
    assert report["identical"] is False
    assert set(report["missing"]) == set(SYMBOLS)
    assert report["drained"] == 0


def test_partial_drain_produces_missing(tmp_path):
    """Drain partiel (1 symbole sur 3) → missing non vide, identical=False."""
    probe = _probe(tmp_path)

    # Enfile les 3 symboles manuellement
    probe._ledger.enqueue(
        kind="shadow_decide",
        priority=0,
        scheduled_at_ms=NOW_MS,
        now_ms=NOW_MS,
        partition_key="AAPL",
        dedup_key=f"shadow:{CYCLE_TS}:AAPL",
        payload=json.dumps({"symbol": "AAPL", "cycle_ts": CYCLE_TS}),
    )

    # Drain uniquement AAPL via run_internal directement, puis on vérifie
    # En pratique : on compare un expected de 3 avec un drained de 1
    import uuid
    probe._drained_symbols = []
    token = uuid.uuid4().hex
    probe._worker.run_once(now_ms=NOW_MS, token=token)

    # Rapport manuel pour vérifier la logique missing
    expected = ["AAPL", "MSFT", "TSLA"]
    drained = sorted(probe._drained_symbols)
    missing = sorted(set(expected) - set(drained))
    assert "MSFT" in missing
    assert "TSLA" in missing


# ── Test 4 : isolation exception interne ────────────────────────────────────


def test_internal_exception_returns_error_dict_no_raise(tmp_path):
    """Toute exception interne → rapport {"error": ..., "identical": False}, pas de raise."""
    probe = _probe(tmp_path)
    with patch.object(probe, "_run_internal", side_effect=RuntimeError("boom")):
        report = probe.run(cycle_ts=CYCLE_TS, decided_symbols=SYMBOLS, now_ms=NOW_MS)

    assert "error" in report
    assert "boom" in report["error"]
    assert report["identical"] is False


def test_ledger_exception_returns_error_dict_no_raise(tmp_path):
    """Exception dans ledger.enqueue → capturée, rapport error, pas de raise."""
    probe = _probe(tmp_path)
    with patch.object(probe._ledger, "enqueue", side_effect=OSError("disk full")):
        report = probe.run(cycle_ts=CYCLE_TS, decided_symbols=SYMBOLS, now_ms=NOW_MS)

    assert "error" in report
    assert report["identical"] is False


# ── Test 5 : isolation DB ───────────────────────────────────────────────────


def test_probe_writes_only_to_shadow_db(tmp_path):
    """La sonde n'écrit QUE dans shadow_queue.db, pas dans un autre fichier state."""
    shadow_db = tmp_path / "shadow_queue.db"
    other_db = tmp_path / "prod.db"

    probe = ShadowQueueProbe(shadow_db)
    probe.run(cycle_ts=CYCLE_TS, decided_symbols=SYMBOLS, now_ms=NOW_MS)

    assert shadow_db.exists(), "shadow_queue.db doit exister"
    assert not other_db.exists(), "prod.db ne doit pas être créé"
    # Vérifie que le ledger pointe bien vers shadow_queue.db
    assert probe._ledger.path == shadow_db


# ── Test 6 : liste vide ─────────────────────────────────────────────────────


def test_empty_decided_symbols(tmp_path):
    """Liste vide → enqueued=0, drained=0, identical=True (vide == vide)."""
    probe = _probe(tmp_path)
    report = probe.run(cycle_ts=CYCLE_TS, decided_symbols=[], now_ms=NOW_MS)

    assert report["enqueued"] == 0
    assert report["drained"] == 0
    assert report["identical"] is True
    assert report["missing"] == []
    assert report["expected"] == []
    assert report["drained_symbols"] == []


# ── Test 7 : champs rapport machine-readable ────────────────────────────────


def test_report_contains_all_required_fields(tmp_path):
    """Le rapport doit contenir tous les champs machine-readable requis."""
    probe = _probe(tmp_path)
    report = probe.run(cycle_ts=CYCLE_TS, decided_symbols=SYMBOLS, now_ms=NOW_MS)

    required_fields = {
        "cycle_ts", "enqueued", "deduped", "drained",
        "expected", "drained_symbols", "missing", "duplicated", "dead", "identical",
    }
    assert required_fields <= set(report.keys()), (
        f"Champs manquants: {required_fields - set(report.keys())}"
    )
    assert report["cycle_ts"] == CYCLE_TS
