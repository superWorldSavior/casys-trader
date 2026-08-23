"""Tests de trader/macro_series.py (P1a spec §7).

Couverture :
  - append si period nouvelle
  - skip si period identique (dédup par period)
  - erreur d'une série n'empêche pas les autres
  - déclenchement 20 h respecté (cooldown)
  - échec réseau total → cycle intact (maybe_collect retourne sans lever)
  - maybe_collect retourne immédiatement (thread fire-and-forget)
  - pas de double départ pendant une collecte en cours
  - marqueur posé au lancement du thread (pas à la fin)
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import trader.market.macro_series as _ms
from trader.market.macro_series import (
    SERIES,
    collect_daily,
    maybe_collect,
)

UTC = timezone.utc
NOW = datetime(2026, 7, 2, 10, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixture — réinitialise le flag de collecte entre les tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_collect_flag():
    """Garantit que _collect_in_progress est False avant et après chaque test."""
    _ms._collect_in_progress = False
    yield
    _ms._collect_in_progress = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dbnomics_resp(period: str, value: float) -> dict:
    """Réponse DBnomics minimale pour une observation."""
    return {"series": {"docs": [{"period": [period], "value": [value]}]}}


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _stub_all(period: str, value: float = 3.5):
    """Stub get_json qui retourne la même réponse pour toutes les URLs."""
    def get_json(_url: str) -> dict:
        return _dbnomics_resp(period, value)
    return get_json


def _join(result: dict, timeout: float = 5.0) -> None:
    """Joint le thread de collecte si présent dans le résultat."""
    t = result.get("_thread")
    if isinstance(t, threading.Thread):
        t.join(timeout=timeout)


# ---------------------------------------------------------------------------
# collect_daily — append si period nouvelle
# ---------------------------------------------------------------------------


def test_append_nouvelle_period(tmp_path):
    """Premier appel → chaque série crée un JSONL avec 1 ligne."""
    result = collect_daily(tmp_path, NOW, get_json=_stub_all("2026-06"))

    assert result["collected"] == len(SERIES)
    assert result["skipped"] == 0
    assert result["errors"] == 0

    macro_dir = tmp_path / "macro_series"
    for s in SERIES:
        label = s["label"]
        rows = _read_jsonl(macro_dir / f"{label}.jsonl")
        assert len(rows) == 1
        row = rows[0]
        assert row["period"] == "2026-06"
        assert row["value"] == 3.5
        assert row["series_id"] == s["id"]
        assert "ts_collected" in row


def test_skip_period_identique(tmp_path):
    """Deuxième appel avec la même period et la même valeur → tout skippé."""
    collect_daily(tmp_path, NOW, get_json=_stub_all("2026-06"))
    result = collect_daily(tmp_path, NOW, get_json=_stub_all("2026-06"))

    assert result["skipped"] == len(SERIES)
    assert result["collected"] == 0
    assert result["errors"] == 0

    # Vérifier qu'aucune ligne n'a été dupliquée
    macro_dir = tmp_path / "macro_series"
    for s in SERIES:
        rows = _read_jsonl(macro_dir / f"{s['label']}.jsonl")
        assert len(rows) == 1, f"doublon inattendu pour {s['label']}"


def test_meme_period_valeur_corrigee_cree_une_vintage(tmp_path):
    """Même période, valeur différente → append une correction, pas un skip."""
    collect_daily(tmp_path, NOW, get_json=_stub_all("2026-06", 3.5))
    result = collect_daily(tmp_path, NOW, get_json=_stub_all("2026-06", 3.75))

    assert result["collected"] == len(SERIES)
    assert result["skipped"] == 0
    assert result["errors"] == 0

    macro_dir = tmp_path / "macro_series"
    for s in SERIES:
        rows = _read_jsonl(macro_dir / f"{s['label']}.jsonl")
        assert len(rows) == 2, f"correction absente pour {s['label']}"
        assert rows[0]["period"] == rows[1]["period"] == "2026-06"
        assert rows[0]["value"] == 3.5
        assert rows[1]["value"] == 3.75
        assert "unit" in rows[1]


def test_append_apres_nouvelle_period(tmp_path):
    """Nouvelles données (period différente) → ligne supplémentaire."""
    collect_daily(tmp_path, NOW, get_json=_stub_all("2026-06"))
    result = collect_daily(tmp_path, NOW, get_json=_stub_all("2026-07"))

    assert result["collected"] == len(SERIES)
    assert result["skipped"] == 0

    macro_dir = tmp_path / "macro_series"
    for s in SERIES:
        rows = _read_jsonl(macro_dir / f"{s['label']}.jsonl")
        assert len(rows) == 2
        assert rows[0]["period"] == "2026-06"
        assert rows[1]["period"] == "2026-07"


# ---------------------------------------------------------------------------
# Isolation des erreurs par série
# ---------------------------------------------------------------------------


def test_erreur_serie_nexempte_pas_les_autres(tmp_path):
    """Une série qui lève une exception ne bloque pas les suivantes."""
    call_count = [0]

    def get_json(_url: str) -> dict:
        call_count[0] += 1
        if call_count[0] == 1:
            raise ConnectionError("timeout simulé")
        return _dbnomics_resp("2026-06", 3.5)

    result = collect_daily(tmp_path, NOW, get_json=get_json)

    assert result["errors"] == 1
    assert result["collected"] == len(SERIES) - 1
    assert result["skipped"] == 0


def test_erreur_observation_absente(tmp_path):
    """Réponse vide (aucune observation) → erreur, pas de crash."""
    def get_json(_url: str) -> dict:
        return {"series": {"docs": []}}

    result = collect_daily(tmp_path, NOW, get_json=get_json)

    assert result["errors"] == len(SERIES)
    assert result["collected"] == 0


# ---------------------------------------------------------------------------
# maybe_collect — gate 20 h
# ---------------------------------------------------------------------------


def test_maybe_collect_sans_marqueur(tmp_path):
    """Pas de marqueur → déclenche, crée le marqueur, collecte les séries."""
    result = maybe_collect(tmp_path, NOW, get_json=_stub_all("2026-06"))

    assert result["triggered"] is True
    _join(result)

    marker = tmp_path / "macro_series" / ".last_collect"
    assert marker.exists()

    macro_dir = tmp_path / "macro_series"
    for s in SERIES:
        rows = _read_jsonl(macro_dir / f"{s['label']}.jsonl")
        assert len(rows) == 1, f"JSONL manquant pour {s['label']}"


def test_maybe_collect_cooldown_bloque(tmp_path):
    """Moins de 24 h depuis dernière collecte → pas de déclenchement."""
    result0 = maybe_collect(tmp_path, NOW, get_json=_stub_all("2026-06"))
    _join(result0)

    # 5 h après (< 20 h)
    now2 = datetime(2026, 7, 2, 15, 0, 0, tzinfo=UTC)
    result = maybe_collect(tmp_path, now2, get_json=_stub_all("2026-06"))

    assert result["triggered"] is False
    assert result["reason"] == "cooldown"
    assert result["elapsed_h"] < 24.0


def test_maybe_collect_21h_reste_en_cooldown_24h(tmp_path):
    """21 h < cooldown opérateur 24 h → pas de déclenchement."""
    result0 = maybe_collect(tmp_path, NOW, get_json=_stub_all("2026-06"))
    _join(result0)

    now2 = NOW + timedelta(hours=21)
    result = maybe_collect(tmp_path, now2, get_json=_stub_all("2026-07"))

    assert result["triggered"] is False
    assert result["reason"] == "cooldown"


def test_maybe_collect_declenche_apres_24h(tmp_path):
    """Plus de 24 h depuis dernière collecte → déclenchement et nouvelles données."""
    result0 = maybe_collect(tmp_path, NOW, get_json=_stub_all("2026-06"))
    _join(result0)

    now2 = NOW + timedelta(hours=25)
    result = maybe_collect(tmp_path, now2, get_json=_stub_all("2026-07"))

    assert result["triggered"] is True
    _join(result)

    macro_dir = tmp_path / "macro_series"
    for s in SERIES:
        rows = _read_jsonl(macro_dir / f"{s['label']}.jsonl")
        assert len(rows) == 2, f"deuxième ligne attendue pour {s['label']}"
        assert rows[1]["period"] == "2026-07"


def test_maybe_collect_juste_avant_seuil(tmp_path):
    """23 h 59 min → encore bloqué par le cooldown 24 h."""
    result0 = maybe_collect(tmp_path, NOW, get_json=_stub_all("2026-06"))
    _join(result0)

    now2 = NOW + timedelta(hours=23, minutes=59)
    result = maybe_collect(tmp_path, now2, get_json=_stub_all("2026-06"))

    assert result["triggered"] is False
    assert result["reason"] == "cooldown"


# ---------------------------------------------------------------------------
# Échec réseau total → cycle intact
# ---------------------------------------------------------------------------


def test_echec_reseau_total_cycle_intact(tmp_path):
    """Toutes les séries échouent → maybe_collect retourne sans lever d'exception."""
    def get_json(_url: str) -> dict:
        raise ConnectionError("réseau coupé")

    # Doit retourner sans lever
    result = maybe_collect(tmp_path, NOW, get_json=get_json)

    assert result["triggered"] is True
    _join(result)


def test_echec_reseau_marqueur_quand_meme_ecrit(tmp_path):
    """Même en cas d'erreur totale, le marqueur est écrit (évite retry frénétique)."""
    def get_json(_url: str) -> dict:
        raise ConnectionError("réseau coupé")

    result0 = maybe_collect(tmp_path, NOW, get_json=get_json)
    _join(result0)

    marker = tmp_path / "macro_series" / ".last_collect"
    assert marker.exists()

    # Cooldown respecté après l'échec
    now2 = datetime(2026, 7, 2, 11, 0, 0, tzinfo=UTC)
    result2 = maybe_collect(tmp_path, now2, get_json=get_json)
    assert result2["triggered"] is False
    assert result2["reason"] == "cooldown"


# ---------------------------------------------------------------------------
# maybe_collect — comportement asynchrone
# ---------------------------------------------------------------------------


def test_maybe_collect_retourne_immediatement(tmp_path):
    """maybe_collect rend la main immédiatement, même si get_json est lente."""
    started = threading.Event()
    can_finish = threading.Event()

    def slow_get_json(_url: str) -> dict:
        started.set()
        can_finish.wait(timeout=5.0)
        return _dbnomics_resp("2026-06", 3.5)

    t0 = time.monotonic()
    result = maybe_collect(tmp_path, NOW, get_json=slow_get_json)
    elapsed = time.monotonic() - t0

    assert result["triggered"] is True
    # Le cycle ne doit pas avoir attendu la fin de la collecte
    assert elapsed < 0.1, f"maybe_collect a bloqué {elapsed:.3f}s (attendu < 0.1s)"

    # Laisser le thread finir proprement
    can_finish.set()
    _join(result)


def test_no_double_demarrage_en_cours(tmp_path):
    """Pendant une collecte en cours, un second appel retourne in_progress."""
    started = threading.Event()
    can_finish = threading.Event()

    def slow_get_json(_url: str) -> dict:
        started.set()
        can_finish.wait(timeout=5.0)
        return _dbnomics_resp("2026-06", 3.5)

    result1 = maybe_collect(tmp_path, NOW, get_json=slow_get_json)
    assert result1["triggered"] is True

    # Attendre que le thread ait réellement démarré
    assert started.wait(timeout=2.0), "thread de collecte non démarré"

    # Second appel pendant la collecte : cooldown 24 h dépassé mais thread en cours
    now2 = NOW + timedelta(hours=25)
    result2 = maybe_collect(tmp_path, now2, get_json=slow_get_json)

    assert result2["triggered"] is False
    assert result2["reason"] == "in_progress"

    # Laisser le premier thread finir
    can_finish.set()
    _join(result1)


def test_marqueur_pose_au_lancement_pas_a_la_fin(tmp_path):
    """Le marqueur existe avant la fin de la collecte — protège le cycle suivant."""
    started = threading.Event()
    can_finish = threading.Event()

    def slow_get_json(_url: str) -> dict:
        started.set()
        can_finish.wait(timeout=5.0)
        return _dbnomics_resp("2026-06", 3.5)

    result = maybe_collect(tmp_path, NOW, get_json=slow_get_json)
    assert result["triggered"] is True

    # Attendre le démarrage effectif du thread
    assert started.wait(timeout=2.0), "thread de collecte non démarré"

    # Le thread tourne encore, mais le marqueur doit déjà être posé
    marker = tmp_path / "macro_series" / ".last_collect"
    assert marker.exists(), "marqueur absent alors que le thread tourne encore"

    # Un appel 5 min plus tard est bloqué par le cooldown (marqueur présent)
    now2 = NOW + timedelta(minutes=5)
    result2 = maybe_collect(tmp_path, now2, get_json=_stub_all("2026-06"))
    assert result2["triggered"] is False
    assert result2["reason"] == "cooldown"

    # Laisser le thread finir proprement
    can_finish.set()
    _join(result)


def test_extract_last_observation_rejette_scalaire_non_liste():
    """Robustesse : si DBnomics passait period/value en scalaire, on avale
    proprement au lieu de prendre le dernier caractère (finding Codex 02/07)."""
    from trader.market.macro_series import _extract_last_observation

    # Cas nominal (listes) : OK.
    ok = {"series": {"docs": [{"period": ["2026-06"], "value": [3.63]}]}}
    assert _extract_last_observation(ok) == ("2026-06", 3.63)

    # period scalaire string → None (pas "6" = dernier caractère).
    scalar = {"series": {"docs": [{"period": "2026-06", "value": [3.63]}]}}
    assert _extract_last_observation(scalar) is None

    # value scalaire → None.
    scalar_v = {"series": {"docs": [{"period": ["2026-06"], "value": 3.63}]}}
    assert _extract_last_observation(scalar_v) is None
