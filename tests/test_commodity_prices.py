"""Tests de trader/infrastructure/market_sources/commodity_prices.py.

Couverture :
  - fetch_last_close : extraction correcte depuis réponse Yahoo v8 simulée
  - fetch_last_close : robustesse (close null, liste vide, structure absente)
  - collect_daily : append si period nouvelle (dédup par period)
  - collect_daily : skip si period identique
  - collect_daily : erreur sur une commodité n'empêche pas les autres
  - maybe_collect : cooldown 20 h respecté
  - maybe_collect : thread fire-and-forget (retour immédiat)
  - maybe_collect : marqueur posé au lancement du thread
  - maybe_collect : pas de double départ pendant collecte en cours

Aucun appel réseau réel — tout le transport est mocké.
Aucune écriture dans state/ — uniquement tmp_path.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import trader.infrastructure.market_sources.commodity_prices as _cp
from trader.infrastructure.market_sources.commodity_prices import (
    COMMODITIES,
    collect_daily,
    maybe_collect,
    _fetch_last_close,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixture — réinitialise le flag de collecte entre les tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_collect_flag():
    """Garantit que _collect_in_progress est False avant/après chaque test."""
    _cp._collect_in_progress = False
    yield
    _cp._collect_in_progress = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _yahoo_resp(period_date: str, close: float) -> str:
    """Réponse Yahoo v8 minimale pour une seule barre quotidienne."""
    from datetime import date
    import calendar
    # Convertit la date en timestamp Unix (UTC midnight)
    d = date.fromisoformat(period_date)
    ts = int(calendar.timegm(d.timetuple()))
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [ts],
                    "meta": {"exchangeTimezoneName": "UTC"},
                    "indicators": {
                        "quote": [{"close": [close], "open": [close], "high": [close], "low": [close], "volume": [0]}]
                    },
                }
            ],
            "error": None,
        }
    }
    return json.dumps(payload)


def _stub_http(period: str, close: float = 91.5):
    """Transport mocké qui retourne la même réponse pour toutes les URLs."""
    def http_get(_url: str) -> str:
        return _yahoo_resp(period, close)
    return http_get


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _join(result: dict, timeout: float = 5.0) -> None:
    t = result.get("_thread")
    if isinstance(t, threading.Thread):
        t.join(timeout=timeout)


# ---------------------------------------------------------------------------
# _fetch_last_close — extraction depuis réponse Yahoo simulée
# ---------------------------------------------------------------------------


def test_fetch_last_close_nominal():
    """Barre valide → retourne (period, close)."""
    obs = _fetch_last_close("BZ=F", http_get=_stub_http("2026-08-18", 91.22))
    assert obs is not None
    period, value = obs
    assert period == "2026-08-18"
    assert abs(value - 91.22) < 0.01


def test_fetch_last_close_close_null():
    """Close null → None (aucun crash)."""
    def http_get(_url: str) -> str:
        from datetime import date
        import calendar
        ts = int(calendar.timegm(date.fromisoformat("2026-08-18").timetuple()))
        return json.dumps({"chart": {"result": [{"timestamp": [ts], "meta": {}, "indicators": {"quote": [{"close": [None]}]}}], "error": None}})

    obs = _fetch_last_close("BZ=F", http_get=http_get)
    assert obs is None


def test_fetch_last_close_empty_timestamps():
    """Timestamps vide → None."""
    def http_get(_url: str) -> str:
        return json.dumps({"chart": {"result": [{"timestamp": [], "meta": {}, "indicators": {"quote": [{"close": []}]}}], "error": None}})

    obs = _fetch_last_close("GC=F", http_get=http_get)
    assert obs is None


def test_fetch_last_close_chart_error():
    """chart.error non-null → None (aucun crash)."""
    def http_get(_url: str) -> str:
        return json.dumps({"chart": {"result": None, "error": {"code": "Not Found"}}})

    obs = _fetch_last_close("BZ=F", http_get=http_get)
    assert obs is None


def test_fetch_last_close_transport_error():
    """Erreur réseau → None (aucun crash)."""
    def http_get(_url: str) -> str:
        raise ConnectionError("réseau coupé")

    obs = _fetch_last_close("BZ=F", http_get=http_get)
    assert obs is None


def test_fetch_last_close_skips_null_and_returns_last_valid():
    """Plusieurs barres dont la dernière est null → prend le dernier close valide."""
    from datetime import date
    import calendar

    ts1 = int(calendar.timegm(date.fromisoformat("2026-08-17").timetuple()))
    ts2 = int(calendar.timegm(date.fromisoformat("2026-08-18").timetuple()))

    def http_get(_url: str) -> str:
        return json.dumps({
            "chart": {
                "result": [{
                    "timestamp": [ts1, ts2],
                    "meta": {},
                    "indicators": {"quote": [{"close": [90.0, None]}]},
                }],
                "error": None,
            }
        })

    obs = _fetch_last_close("BZ=F", http_get=http_get)
    assert obs is not None
    period, value = obs
    assert period == "2026-08-17"
    assert abs(value - 90.0) < 0.01


# ---------------------------------------------------------------------------
# collect_daily — append + dédup
# ---------------------------------------------------------------------------


def test_collect_daily_append_nouvelles_periods(tmp_path):
    """Premier appel → chaque commodité crée un JSONL avec 1 ligne."""
    result = collect_daily(tmp_path, NOW, http_get=_stub_http("2026-08-18", 91.22))

    assert result["collected"] == len(COMMODITIES)
    assert result["skipped"] == 0
    assert result["errors"] == 0

    macro_dir = tmp_path / "macro_series"
    for c in COMMODITIES:
        rows = _read_jsonl(macro_dir / f"{c['label']}.jsonl")
        assert len(rows) == 1
        row = rows[0]
        assert row["period"] == "2026-08-18"
        assert row["series_id"] == c["series_id"]
        assert "ts_collected" in row
        assert "value" in row


def test_collect_daily_skip_period_identique(tmp_path):
    """Deuxième appel avec la même period et la même valeur → tout skippé."""
    collect_daily(tmp_path, NOW, http_get=_stub_http("2026-08-18"))
    result = collect_daily(tmp_path, NOW, http_get=_stub_http("2026-08-18"))

    assert result["skipped"] == len(COMMODITIES)
    assert result["collected"] == 0
    assert result["errors"] == 0

    macro_dir = tmp_path / "macro_series"
    for c in COMMODITIES:
        rows = _read_jsonl(macro_dir / f"{c['label']}.jsonl")
        assert len(rows) == 1, f"doublon inattendu pour {c['label']}"


def test_collect_daily_meme_period_valeur_corrigee_cree_une_vintage(tmp_path):
    """Même période, close différent → append une correction de vintage."""
    collect_daily(tmp_path, NOW, http_get=_stub_http("2026-08-18", 91.22))
    result = collect_daily(tmp_path, NOW, http_get=_stub_http("2026-08-18", 92.5))

    assert result["collected"] == len(COMMODITIES)
    assert result["skipped"] == 0
    assert result["errors"] == 0

    macro_dir = tmp_path / "macro_series"
    for c in COMMODITIES:
        rows = _read_jsonl(macro_dir / f"{c['label']}.jsonl")
        assert len(rows) == 2, f"correction absente pour {c['label']}"
        assert rows[0]["period"] == rows[1]["period"] == "2026-08-18"
        assert abs(rows[0]["value"] - 91.22) < 0.01
        assert abs(rows[1]["value"] - 92.5) < 0.01
        assert "unit" in rows[1]


def test_collect_daily_append_nouvelle_period(tmp_path):
    """Period différente le lendemain → ligne supplémentaire."""
    collect_daily(tmp_path, NOW, http_get=_stub_http("2026-08-18"))
    result = collect_daily(tmp_path, NOW, http_get=_stub_http("2026-08-19", 92.0))

    assert result["collected"] == len(COMMODITIES)
    assert result["skipped"] == 0

    macro_dir = tmp_path / "macro_series"
    for c in COMMODITIES:
        rows = _read_jsonl(macro_dir / f"{c['label']}.jsonl")
        assert len(rows) == 2
        assert rows[0]["period"] == "2026-08-18"
        assert rows[1]["period"] == "2026-08-19"


def test_collect_daily_erreur_une_commodite_nexempte_pas_les_autres(tmp_path):
    """Erreur sur la première commodité → les suivantes sont quand même collectées."""
    call_count = [0]

    def http_get(_url: str) -> str:
        call_count[0] += 1
        if call_count[0] == 1:
            raise ConnectionError("timeout simulé")
        return _yahoo_resp("2026-08-18", 4450.0)

    result = collect_daily(tmp_path, NOW, http_get=http_get)

    assert result["errors"] == 1
    assert result["collected"] == len(COMMODITIES) - 1
    assert result["skipped"] == 0


def test_collect_daily_erreur_observation_absente(tmp_path):
    """Réponse vide (pas de result) → erreur, pas de crash."""
    def http_get(_url: str) -> str:
        return json.dumps({"chart": {"result": [], "error": None}})

    result = collect_daily(tmp_path, NOW, http_get=http_get)

    assert result["errors"] == len(COMMODITIES)
    assert result["collected"] == 0


# ---------------------------------------------------------------------------
# maybe_collect — cooldown
# ---------------------------------------------------------------------------


def test_maybe_collect_sans_marqueur(tmp_path):
    """Pas de marqueur → déclenche, crée le marqueur, collecte les données."""
    result = maybe_collect(tmp_path, NOW, http_get=_stub_http("2026-08-18"))

    assert result["triggered"] is True
    _join(result)

    marker = tmp_path / "macro_series" / "commodity_prices.last_collect"
    assert marker.exists()

    macro_dir = tmp_path / "macro_series"
    for c in COMMODITIES:
        rows = _read_jsonl(macro_dir / f"{c['label']}.jsonl")
        assert len(rows) == 1, f"JSONL manquant pour {c['label']}"


def test_maybe_collect_cooldown_bloque(tmp_path):
    """Moins de 24 h depuis dernière collecte → pas de déclenchement."""
    result0 = maybe_collect(tmp_path, NOW, http_get=_stub_http("2026-08-18"))
    _join(result0)

    now2 = NOW + timedelta(hours=5)
    result = maybe_collect(tmp_path, now2, http_get=_stub_http("2026-08-18"))

    assert result["triggered"] is False
    assert result["reason"] == "cooldown"
    assert result["elapsed_h"] < 24.0


def test_maybe_collect_21h_reste_en_cooldown_24h(tmp_path):
    """21 h < cooldown opérateur 24 h → pas de déclenchement."""
    result0 = maybe_collect(tmp_path, NOW, http_get=_stub_http("2026-08-18"))
    _join(result0)

    now2 = NOW + timedelta(hours=21)
    result = maybe_collect(tmp_path, now2, http_get=_stub_http("2026-08-19", 92.0))

    assert result["triggered"] is False
    assert result["reason"] == "cooldown"


def test_maybe_collect_declenche_apres_24h(tmp_path):
    """Plus de 24 h depuis dernière collecte → déclenchement."""
    result0 = maybe_collect(tmp_path, NOW, http_get=_stub_http("2026-08-18"))
    _join(result0)

    now2 = NOW + timedelta(hours=25)
    result = maybe_collect(tmp_path, now2, http_get=_stub_http("2026-08-19", 92.0))

    assert result["triggered"] is True
    _join(result)

    macro_dir = tmp_path / "macro_series"
    for c in COMMODITIES:
        rows = _read_jsonl(macro_dir / f"{c['label']}.jsonl")
        assert len(rows) == 2, f"deuxième ligne attendue pour {c['label']}"
        assert rows[1]["period"] == "2026-08-19"


# ---------------------------------------------------------------------------
# maybe_collect — comportement asynchrone
# ---------------------------------------------------------------------------


def test_maybe_collect_retourne_immediatement(tmp_path):
    """maybe_collect rend la main immédiatement même si http_get est lent."""
    started = threading.Event()
    can_finish = threading.Event()

    def slow_http(_url: str) -> str:
        started.set()
        can_finish.wait(timeout=5.0)
        return _yahoo_resp("2026-08-18", 91.22)

    t0 = time.monotonic()
    result = maybe_collect(tmp_path, NOW, http_get=slow_http)
    elapsed = time.monotonic() - t0

    assert result["triggered"] is True
    assert elapsed < 0.1, f"maybe_collect a bloqué {elapsed:.3f}s (attendu < 0.1s)"

    can_finish.set()
    _join(result)


def test_no_double_demarrage_en_cours(tmp_path):
    """Pendant une collecte en cours, un second appel retourne in_progress."""
    started = threading.Event()
    can_finish = threading.Event()

    def slow_http(_url: str) -> str:
        started.set()
        can_finish.wait(timeout=5.0)
        return _yahoo_resp("2026-08-18", 91.22)

    result1 = maybe_collect(tmp_path, NOW, http_get=slow_http)
    assert result1["triggered"] is True
    assert started.wait(timeout=2.0), "thread de collecte non démarré"

    now2 = NOW + timedelta(hours=25)
    result2 = maybe_collect(tmp_path, now2, http_get=slow_http)

    assert result2["triggered"] is False
    assert result2["reason"] == "in_progress"

    can_finish.set()
    _join(result1)


def test_marqueur_pose_apres_collecte_reussie(tmp_path):
    """Le marqueur est écrit APRÈS la collecte réussie, pas avant le lancement du thread."""
    started = threading.Event()
    can_finish = threading.Event()

    def slow_http(_url: str) -> str:
        started.set()
        can_finish.wait(timeout=5.0)
        return _yahoo_resp("2026-08-18", 91.22)

    result = maybe_collect(tmp_path, NOW, http_get=slow_http)
    assert result["triggered"] is True
    assert started.wait(timeout=2.0), "thread non démarré"

    marker = tmp_path / "macro_series" / "commodity_prices.last_collect"
    # Pendant la collecte (thread encore en attente) : pas encore de marqueur
    assert not marker.exists(), "marqueur ne doit pas exister tant que la collecte est en cours"

    # Laisser le thread finir
    can_finish.set()
    _join(result)

    # Après collecte réussie : marqueur présent
    assert marker.exists(), "marqueur doit exister après une collecte réussie"

    # Un nouvel appel 5 min plus tard est bloqué par le cooldown
    now2 = NOW + timedelta(minutes=5)
    result2 = maybe_collect(tmp_path, now2, http_get=_stub_http("2026-08-18"))
    assert result2["triggered"] is False
    assert result2["reason"] == "cooldown"


def test_echec_reseau_total_cycle_intact(tmp_path):
    """Toutes les commodités échouent → maybe_collect retourne sans lever."""
    def bad_http(_url: str) -> str:
        raise ConnectionError("réseau coupé")

    result = maybe_collect(tmp_path, NOW, http_get=bad_http)
    assert result["triggered"] is True
    _join(result)


def test_echec_reseau_pas_de_marqueur_retry_possible(tmp_path):
    """En cas d'erreur totale, le marqueur N'est PAS écrit → retry au cycle suivant.

    Correctif du finding : l'ancien comportement (marqueur posé avant le thread)
    bloquait tout retry ~20 h même après un échec Yahoo.
    """
    def bad_http(_url: str) -> str:
        raise ConnectionError("réseau coupé")

    result0 = maybe_collect(tmp_path, NOW, http_get=bad_http)
    _join(result0)

    marker = tmp_path / "macro_series" / "commodity_prices.last_collect"
    assert not marker.exists(), "pas de marqueur après échec total → retry possible"

    # Un nouvel appel doit se déclencher immédiatement (pas de cooldown sans marqueur)
    now2 = NOW + timedelta(hours=1)
    result2 = maybe_collect(tmp_path, now2, http_get=bad_http)
    assert result2["triggered"] is True, "doit se déclencher à nouveau (pas de marqueur)"
    _join(result2)
