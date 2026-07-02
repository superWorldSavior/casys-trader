"""Tests du module macro_calendar (spec §2, P1a).

Couverture :
- load_calendar : fusion JSON + constantes
- load_calendar : priorité JSON (event+at identiques = une seule entrée)
- load_calendar : JSON corrompu → constantes seules, jamais d'exception
- load_calendar : JSON absent → constantes seules
- macro_next : in_h calculé correctement
- macro_next : événements passés exclus
- macro_next : limite respectée
- macro_next : liste vide si tous passés
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from trader.macro_calendar import (
    DEFAULT_CALENDAR,
    FOMC_2026,
    load_calendar,
    macro_next,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_calendar(path, entries: list[dict]) -> None:
    path.write_text(json.dumps(entries), encoding="utf-8")


# ---------------------------------------------------------------------------
# load_calendar — fusion JSON + constantes
# ---------------------------------------------------------------------------


def test_fusion_json_et_constantes(tmp_path):
    """Le JSON fournit des événements supplémentaires ; les constantes complètent."""
    extra = {"event": "CPI_US", "at": "2026-08-12T12:30:00Z"}
    _write_calendar(tmp_path / "macro_calendar.json", [extra])

    cal = load_calendar(tmp_path / "macro_calendar.json")

    # L'extra JSON est présent
    assert any(e["event"] == "CPI_US" for e in cal)
    # Les constantes FOMC sont aussi présentes
    fomc_ats = {e["at"] for e in FOMC_2026}
    calendar_ats = {e["at"] for e in cal if e["event"] == "FOMC"}
    assert fomc_ats <= calendar_ats


def test_fusion_json_vient_en_premier(tmp_path):
    """Les entrées JSON précèdent les constantes dans la liste fusionnée."""
    extra = {"event": "CPI_US", "at": "2026-08-12T12:30:00Z"}
    _write_calendar(tmp_path / "macro_calendar.json", [extra])

    cal = load_calendar(tmp_path / "macro_calendar.json")
    assert cal[0]["event"] == "CPI_US"


# ---------------------------------------------------------------------------
# load_calendar — priorité JSON (dédup event+at)
# ---------------------------------------------------------------------------


def test_priorite_json_event_at_identiques(tmp_path):
    """Un (event, at) identique dans le JSON ET dans les constantes → une seule entrée."""
    # Prend le premier FOMC des constantes
    fomc_entry = dict(FOMC_2026[0])
    _write_calendar(tmp_path / "macro_calendar.json", [fomc_entry])

    cal = load_calendar(tmp_path / "macro_calendar.json")

    # Doit n'apparaître qu'une seule fois
    matching = [e for e in cal if e["event"] == fomc_entry["event"] and e["at"] == fomc_entry["at"]]
    assert len(matching) == 1, f"attendu 1, obtenu {len(matching)} : {matching}"


def test_priorite_json_toutes_constantes_presentes_une_fois(tmp_path):
    """Quand le JSON reprend toutes les constantes, aucune constante n'est dupliquée."""
    all_fomc = list(FOMC_2026)
    _write_calendar(tmp_path / "macro_calendar.json", all_fomc)

    cal = load_calendar(tmp_path / "macro_calendar.json")
    # Toutes les constantes → le calendrier ne contient pas de doublons
    seen: set[tuple[str, str]] = set()
    for e in cal:
        key = (e["event"], e["at"])
        assert key not in seen, f"doublon détecté : {key}"
        seen.add(key)


# ---------------------------------------------------------------------------
# load_calendar — JSON absent / corrompu → constantes seules
# ---------------------------------------------------------------------------


def test_json_absent_retourne_constantes(tmp_path):
    """JSON absent → constantes seules, sans exception."""
    cal = load_calendar(tmp_path / "macro_calendar_nonexistent.json")
    assert len(cal) == len(DEFAULT_CALENDAR)
    assert all(e in cal for e in DEFAULT_CALENDAR)


def test_json_corrompu_retourne_constantes(tmp_path):
    """JSON corrompu → constantes seules, sans exception."""
    bad = tmp_path / "macro_calendar.json"
    bad.write_text("{invalid json{{{", encoding="utf-8")

    cal = load_calendar(bad)
    assert len(cal) == len(DEFAULT_CALENDAR)


def test_json_vide_retourne_constantes(tmp_path):
    """JSON liste vide → constantes seules."""
    _write_calendar(tmp_path / "macro_calendar.json", [])

    cal = load_calendar(tmp_path / "macro_calendar.json")
    assert len(cal) == len(DEFAULT_CALENDAR)


def test_json_entrees_sans_event_ignorees(tmp_path):
    """Entrées JSON sans champ 'event' ou 'at' sont ignorées silencieusement."""
    entries = [
        {"at": "2026-08-12T12:30:00Z"},  # pas d'event
        {"event": "CPI_US"},              # pas d'at
        {"event": "NFP_US", "at": "2026-08-07T12:30:00Z"},  # valide
    ]
    _write_calendar(tmp_path / "macro_calendar.json", entries)

    cal = load_calendar(tmp_path / "macro_calendar.json")
    # Seule NFP_US valide est présente + les constantes
    assert any(e["event"] == "NFP_US" for e in cal)
    # Les entrées invalides ne génèrent pas d'entrées parasites
    events = [e["event"] for e in cal]
    assert "" not in events


# ---------------------------------------------------------------------------
# macro_next — in_h correct
# ---------------------------------------------------------------------------


def test_in_h_correct():
    """macro_next calcule in_h arrondi à 1 décimale."""
    now = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    # Événement dans 6h exactement
    cal = [{"event": "FOMC", "at": "2026-07-29T18:00:00Z"}]

    result = macro_next(now, cal)

    assert len(result) == 1
    assert result[0]["in_h"] == 6.0


def test_in_h_arrondi_1_decimale():
    """in_h est arrondi à 1 décimale."""
    now = datetime(2026, 7, 29, 11, 30, tzinfo=UTC)  # 6h30 avant 18:00
    cal = [{"event": "FOMC", "at": "2026-07-29T18:00:00Z"}]

    result = macro_next(now, cal)

    assert result[0]["in_h"] == 6.5


def test_in_h_sans_tzinfo_traite_utc():
    """now sans tzinfo est traité comme UTC."""
    now_naive = datetime(2026, 7, 29, 12, 0)  # pas de tzinfo
    cal = [{"event": "FOMC", "at": "2026-07-29T18:00:00Z"}]

    result = macro_next(now_naive, cal)

    assert result[0]["in_h"] == 6.0


# ---------------------------------------------------------------------------
# macro_next — événements passés exclus
# ---------------------------------------------------------------------------


def test_passes_exclus():
    """Les événements dont at <= now sont exclus."""
    now = datetime(2026, 7, 29, 19, 0, tzinfo=UTC)
    cal = [
        {"event": "FOMC", "at": "2026-07-29T18:00:00Z"},   # passé
        {"event": "FOMC", "at": "2026-09-16T18:00:00Z"},   # futur
    ]

    result = macro_next(now, cal)

    assert len(result) == 1
    assert result[0]["at"] == "2026-09-16T18:00:00Z"


def test_evenement_exactement_maintenant_exclu():
    """Un événement à exactly now (at == now) est exclu (condition stricte >)."""
    now = datetime(2026, 7, 29, 18, 0, tzinfo=UTC)
    cal = [{"event": "FOMC", "at": "2026-07-29T18:00:00Z"}]

    result = macro_next(now, cal)

    assert result == []


def test_liste_vide_si_tous_passes():
    """Retourne [] si tous les événements sont passés."""
    now = datetime(2027, 1, 1, 0, 0, tzinfo=UTC)
    cal = list(FOMC_2026)

    result = macro_next(now, cal)

    assert result == []


# ---------------------------------------------------------------------------
# macro_next — limite et tri
# ---------------------------------------------------------------------------


def test_limite_respectee():
    """macro_next retourne au plus `limit` événements."""
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    cal = list(FOMC_2026)

    result = macro_next(now, cal, limit=2)

    assert len(result) == 2


def test_tri_chronologique():
    """Les événements sont retournés dans l'ordre chronologique."""
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    # Ordre inversé dans le calendrier pour tester le tri
    cal = [
        {"event": "FOMC", "at": "2026-09-16T18:00:00Z"},
        {"event": "FOMC", "at": "2026-03-18T18:00:00Z"},
        {"event": "FOMC", "at": "2026-07-29T18:00:00Z"},
    ]

    result = macro_next(now, cal, limit=3)

    ats = [e["at"] for e in result]
    assert ats == sorted(ats)


def test_structure_des_entrees():
    """Chaque entrée contient les clés event, at, in_h."""
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    cal = [{"event": "FOMC", "at": "2026-07-29T18:00:00Z"}]

    result = macro_next(now, cal, limit=1)

    assert len(result) == 1
    entry = result[0]
    assert set(entry.keys()) >= {"event", "at", "in_h"}
    assert entry["event"] == "FOMC"
    assert entry["at"] == "2026-07-29T18:00:00Z"
    assert isinstance(entry["in_h"], float)


def test_entrees_non_parsables_ignorees():
    """Entrées avec 'at' non parsable sont ignorées silencieusement."""
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    cal = [
        {"event": "FOMC", "at": "not-a-date"},
        {"event": "FOMC", "at": "2026-07-29T18:00:00Z"},
    ]

    result = macro_next(now, cal, limit=3)

    assert len(result) == 1
    assert result[0]["at"] == "2026-07-29T18:00:00Z"


def test_calendrier_vide():
    """Calendrier vide → liste vide, sans exception."""
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    result = macro_next(now, [])
    assert result == []


# ---------------------------------------------------------------------------
# FOMC_2026 — sanité des constantes
# ---------------------------------------------------------------------------


def test_fomc_2026_huit_reunions():
    """FOMC_2026 contient exactement 8 réunions."""
    assert len(FOMC_2026) == 8


def test_fomc_2026_toutes_a_18h_utc():
    """Toutes les réunions FOMC 2026 sont à 18:00Z."""
    for entry in FOMC_2026:
        dt = datetime.fromisoformat(entry["at"].replace("Z", "+00:00"))
        assert dt.hour == 18
        assert dt.minute == 0
        assert dt.second == 0


def test_fomc_2026_ordre_chronologique():
    """Les constantes FOMC_2026 sont dans l'ordre chronologique."""
    dts = [datetime.fromisoformat(e["at"].replace("Z", "+00:00")) for e in FOMC_2026]
    assert dts == sorted(dts)
