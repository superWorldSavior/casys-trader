"""Tests de trader/macro_series.py (P1a spec §7).

Couverture :
  - append si period nouvelle
  - skip si period identique (dédup par period)
  - erreur d'une série n'empêche pas les autres
  - déclenchement 20 h respecté (cooldown)
  - échec réseau total → cycle intact (maybe_collect retourne sans lever)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from trader.macro_series import (
    SERIES,
    collect_daily,
    maybe_collect,
)

UTC = timezone.utc
NOW = datetime(2026, 7, 2, 10, 0, 0, tzinfo=UTC)


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
    """Deuxième appel avec la même period → tout skippé, aucun doublon."""
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
    """Pas de marqueur → déclenche, crée le marqueur."""
    result = maybe_collect(tmp_path, NOW, get_json=_stub_all("2026-06"))

    assert result["triggered"] is True
    assert result["collected"] == len(SERIES)

    marker = tmp_path / "macro_series" / ".last_collect"
    assert marker.exists()


def test_maybe_collect_cooldown_bloque(tmp_path):
    """Moins de 20 h depuis dernière collecte → pas de déclenchement."""
    maybe_collect(tmp_path, NOW, get_json=_stub_all("2026-06"))

    # 5 h après (< 20 h)
    now2 = datetime(2026, 7, 2, 15, 0, 0, tzinfo=UTC)
    result = maybe_collect(tmp_path, now2, get_json=_stub_all("2026-06"))

    assert result["triggered"] is False
    assert result["reason"] == "cooldown"
    assert result["elapsed_h"] < 20.0


def test_maybe_collect_declenche_apres_20h(tmp_path):
    """Plus de 20 h depuis dernière collecte → déclenchement."""
    maybe_collect(tmp_path, NOW, get_json=_stub_all("2026-06"))

    # 21 h après
    now2 = datetime(2026, 7, 3, 7, 0, 0, tzinfo=UTC)
    result = maybe_collect(tmp_path, now2, get_json=_stub_all("2026-07"))

    assert result["triggered"] is True
    assert result["collected"] == len(SERIES)


def test_maybe_collect_juste_avant_seuil(tmp_path):
    """19 h 59 min → encore bloqué."""
    maybe_collect(tmp_path, NOW, get_json=_stub_all("2026-06"))

    from datetime import timedelta
    now2 = NOW + timedelta(hours=19, minutes=59)
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
    assert result["collected"] == 0
    # Soit errors > 0 via collect_daily, soit error= dans le catch externe
    assert result.get("errors", 0) > 0 or "error" in result


def test_echec_reseau_marqueur_quand_meme_ecrit(tmp_path):
    """Même en cas d'erreur totale, le marqueur est écrit (évite retry frénétique)."""
    def get_json(_url: str) -> dict:
        raise ConnectionError("réseau coupé")

    maybe_collect(tmp_path, NOW, get_json=get_json)

    marker = tmp_path / "macro_series" / ".last_collect"
    assert marker.exists()

    # Cooldown respecté après l'échec
    now2 = datetime(2026, 7, 2, 11, 0, 0, tzinfo=UTC)
    result2 = maybe_collect(tmp_path, now2, get_json=get_json)
    assert result2["triggered"] is False
    assert result2["reason"] == "cooldown"
