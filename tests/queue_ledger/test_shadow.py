"""tests/queue_ledger/test_shadow.py — sonde d'orchestration shadow-queue.

TDD : les tests vérifient l'orchestration pure (enqueue/drain/identité),
jamais le LLM.

Couverture post-FIX2/FIX3 :
- Signature run(*, cycle_ts, decidable_symbols, decided_symbols, now_ms)
- Purge par cycle (vieux dead d'un cycle précédent invisible dans le rapport)
- Source indépendante (decidable ≠ decided → decided_vs_decidable renseigné)
- Placement fin-de-cycle noté (test fonctionnel hors périmètre unitaire)
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from trader.queue.shadow import ShadowQueueProbe


# ── Helpers ─────────────────────────────────────────────────────────────────


def _probe(tmp_path: Path) -> ShadowQueueProbe:
    return ShadowQueueProbe(tmp_path / "shadow_queue.db")


NOW_MS = 1_000_000
CYCLE_TS = "2026-07-03T10:00:00+00:00"
SYMBOLS = ["AAPL", "MSFT", "TSLA"]


# ── Test 1 : happy path ──────────────────────────────────────────────────────


def test_run_3_symbols_enqueued_and_drained(tmp_path):
    """3 symboles decidable == decided → enqueued=3, drained=3, identical=True."""
    probe = _probe(tmp_path)
    report = probe.run(
        cycle_ts=CYCLE_TS,
        decidable_symbols=SYMBOLS,
        decided_symbols=SYMBOLS,
        now_ms=NOW_MS,
    )

    assert report["enqueued"] == 3
    assert report["drained"] == 3
    assert report["identical"] is True
    assert report["missing"] == []
    assert report["duplicated"] == []
    assert report["dead"] == 0
    assert report["expected"] == sorted(SYMBOLS)
    assert report["drained_symbols"] == sorted(SYMBOLS)


# ── Test 2 : purge par cycle — un vieux job ne pollue plus ──────────────────


def test_purge_between_cycles_dead_job_invisible(tmp_path):
    """Un vieux job 'dead' inséré manuellement avant run() est purgé → dead=0."""
    probe = _probe(tmp_path)

    # Injecte un job dead qui simule un résidu du cycle précédent
    probe._ledger._conn.execute(
        "INSERT INTO tasks(kind, status, priority, scheduled_at, created_at, updated_at) "
        "VALUES('shadow_decide', 'dead', 0, 1000, 1000, 1000)"
    )
    probe._ledger._conn.commit()

    # Le run() doit purger ce résidu avant d'enqueuer le cycle courant
    report = probe.run(
        cycle_ts=CYCLE_TS,
        decidable_symbols=SYMBOLS,
        decided_symbols=SYMBOLS,
        now_ms=NOW_MS,
    )

    assert report["dead"] == 0, "Les vieux jobs dead ne doivent pas contaminer le rapport"
    assert report["identical"] is True


def test_purge_resets_state_between_calls(tmp_path):
    """Deux appels successifs repartent propres (purge à chaque run())."""
    probe = _probe(tmp_path)

    r1 = probe.run(
        cycle_ts=CYCLE_TS,
        decidable_symbols=SYMBOLS,
        decided_symbols=SYMBOLS,
        now_ms=NOW_MS,
    )
    assert r1["identical"] is True
    assert r1["enqueued"] == 3

    # 2e appel (cycle suivant) : purge puis re-enqueue → résultat propre
    cycle2 = "2026-07-03T11:00:00+00:00"
    r2 = probe.run(
        cycle_ts=cycle2,
        decidable_symbols=SYMBOLS,
        decided_symbols=SYMBOLS,
        now_ms=NOW_MS + 3_600_000,
    )
    assert r2["enqueued"] == 3
    assert r2["identical"] is True
    assert r2["dead"] == 0


# ── Test 3 : source indépendante — decidable ≠ decided ──────────────────────


def test_source_independante_decided_vs_decidable_diff(tmp_path):
    """decidable ≠ decided → decided_vs_decidable non vide, identical reflète l'acheminement."""
    probe = _probe(tmp_path)
    decidable = ["AAPL", "MSFT", "TSLA"]
    decided = ["AAPL", "MSFT"]  # TSLA pas dans decided

    report = probe.run(
        cycle_ts=CYCLE_TS,
        decidable_symbols=decidable,
        decided_symbols=decided,
        now_ms=NOW_MS,
    )

    # identical = routing (decidable drainé intégralement ?), pas decided
    assert report["identical"] is True  # tous les decidable ont été acheminés
    assert report["expected"] == sorted(decidable)  # source = decidable

    dvd = report["decided_vs_decidable"]
    assert "TSLA" in dvd["only_decidable"]  # decidable mais pas decided
    assert dvd["only_decided"] == []        # decided ⊆ decidable ici
    assert dvd["decided"] == sorted(decided)
    assert dvd["decidable"] == sorted(decidable)


def test_source_independante_decided_extra_symbol(tmp_path):
    """Symbol dans decided mais pas dans decidable → only_decided non vide."""
    probe = _probe(tmp_path)
    decidable = ["AAPL", "MSFT"]
    decided = ["AAPL", "MSFT", "EXTRA"]  # EXTRA armé, pas passé au LLM

    report = probe.run(
        cycle_ts=CYCLE_TS,
        decidable_symbols=decidable,
        decided_symbols=decided,
        now_ms=NOW_MS,
    )

    dvd = report["decided_vs_decidable"]
    assert "EXTRA" in dvd["only_decided"]
    assert dvd["only_decidable"] == []
    assert report["identical"] is True  # routing sur decidable OK


def test_source_independante_identical_when_same_sets(tmp_path):
    """decidable == decided → decided_vs_decidable vide (both diff lists empty)."""
    probe = _probe(tmp_path)
    report = probe.run(
        cycle_ts=CYCLE_TS,
        decidable_symbols=SYMBOLS,
        decided_symbols=SYMBOLS,
        now_ms=NOW_MS,
    )
    dvd = report["decided_vs_decidable"]
    assert dvd["only_decided"] == []
    assert dvd["only_decidable"] == []


# ── Test 4 : divergence simulée (missing dans l'acheminement) ───────────────


def test_missing_symbol_when_drain_capped(tmp_path):
    """Borne de drain = 0 → tous les symboles sont missing, identical=False."""
    probe = _probe(tmp_path)
    with patch.object(probe._worker, "run_once", return_value=False):
        report = probe.run(
            cycle_ts=CYCLE_TS,
            decidable_symbols=SYMBOLS,
            decided_symbols=SYMBOLS,
            now_ms=NOW_MS,
        )

    assert report["identical"] is False
    assert set(report["missing"]) == set(SYMBOLS)
    assert report["drained"] == 0


def test_partial_drain_produces_missing(tmp_path):
    """Drain partiel (1 symbole sur 3) → missing non vide, identical=False."""
    probe = _probe(tmp_path)

    # Enfile uniquement AAPL manuellement
    probe._ledger.enqueue(
        kind="shadow_decide",
        priority=0,
        scheduled_at_ms=NOW_MS,
        now_ms=NOW_MS,
        partition_key="AAPL",
        dedup_key=f"shadow:{CYCLE_TS}:AAPL",
        payload=json.dumps({"symbol": "AAPL", "cycle_ts": CYCLE_TS}),
    )

    import uuid
    probe._drained_symbols = []
    token = uuid.uuid4().hex
    probe._worker.run_once(now_ms=NOW_MS, token=token)

    expected = ["AAPL", "MSFT", "TSLA"]
    drained = sorted(probe._drained_symbols)
    missing = sorted(set(expected) - set(drained))
    assert "MSFT" in missing
    assert "TSLA" in missing


# ── Test 5 : isolation exception interne ────────────────────────────────────


def test_internal_exception_returns_error_dict_no_raise(tmp_path):
    """Toute exception interne → rapport {"error": ..., "identical": False}, pas de raise."""
    probe = _probe(tmp_path)
    with patch.object(probe, "_run_internal", side_effect=RuntimeError("boom")):
        report = probe.run(
            cycle_ts=CYCLE_TS,
            decidable_symbols=SYMBOLS,
            decided_symbols=SYMBOLS,
            now_ms=NOW_MS,
        )

    assert "error" in report
    assert "boom" in report["error"]
    assert report["identical"] is False


def test_ledger_exception_returns_error_dict_no_raise(tmp_path):
    """Exception dans ledger.enqueue → capturée, rapport error, pas de raise."""
    probe = _probe(tmp_path)
    with patch.object(probe._ledger, "enqueue", side_effect=OSError("disk full")):
        report = probe.run(
            cycle_ts=CYCLE_TS,
            decidable_symbols=SYMBOLS,
            decided_symbols=SYMBOLS,
            now_ms=NOW_MS,
        )

    assert "error" in report
    assert report["identical"] is False


# ── Test 6 : isolation DB ───────────────────────────────────────────────────


def test_probe_writes_only_to_shadow_db(tmp_path):
    """La sonde n'écrit QUE dans shadow_queue.db, pas dans un autre fichier state."""
    shadow_db = tmp_path / "shadow_queue.db"
    other_db = tmp_path / "prod.db"

    probe = ShadowQueueProbe(shadow_db)
    probe.run(
        cycle_ts=CYCLE_TS,
        decidable_symbols=SYMBOLS,
        decided_symbols=SYMBOLS,
        now_ms=NOW_MS,
    )

    assert shadow_db.exists(), "shadow_queue.db doit exister"
    assert not other_db.exists(), "prod.db ne doit pas être créé"
    assert probe._ledger.path == shadow_db


# ── Test 7 : liste vide ─────────────────────────────────────────────────────


def test_empty_decided_symbols(tmp_path):
    """Liste vide → enqueued=0, drained=0, identical=True (vide == vide)."""
    probe = _probe(tmp_path)
    report = probe.run(
        cycle_ts=CYCLE_TS,
        decidable_symbols=[],
        decided_symbols=[],
        now_ms=NOW_MS,
    )

    assert report["enqueued"] == 0
    assert report["drained"] == 0
    assert report["identical"] is True
    assert report["missing"] == []
    assert report["expected"] == []
    assert report["drained_symbols"] == []


# ── Test 8 : champs rapport machine-readable ────────────────────────────────


def test_report_contains_all_required_fields(tmp_path):
    """Le rapport doit contenir tous les champs machine-readable requis."""
    probe = _probe(tmp_path)
    report = probe.run(
        cycle_ts=CYCLE_TS,
        decidable_symbols=SYMBOLS,
        decided_symbols=SYMBOLS,
        now_ms=NOW_MS,
    )

    required_fields = {
        "cycle_ts", "enqueued", "deduped", "drained",
        "expected", "drained_symbols", "missing", "duplicated", "dead", "identical",
        "decided_vs_decidable",
    }
    assert required_fields <= set(report.keys()), (
        f"Champs manquants: {required_fields - set(report.keys())}"
    )
    assert report["cycle_ts"] == CYCLE_TS
    # decided_vs_decidable contient les 4 clés attendues
    dvd = report["decided_vs_decidable"]
    assert {"decided", "decidable", "only_decided", "only_decidable"} <= set(dvd.keys())


# ── Note : placement fin-de-cycle (FIX 1) ───────────────────────────────────
# Le branchement daemon.py est couvert par
# tests/test_daemon_learnings.py::test_run_cycle_transmet_les_flags_de_finalisation_cycle.
# La sonde shadow reste déclenchée après les décisions via
# runtime.cycle_finalization.finalize_cycle, pas entre _batch_decide et
# l'exécution des ordres.
