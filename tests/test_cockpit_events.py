"""Tests unitaires de trader.cockpit.events — logique pure, zéro I/O externe."""
from __future__ import annotations

from pathlib import Path

from trader.cockpit.events import (
    EventLine,
    format_event_line,
    classify_event,
    EventClass,
    read_new_lines,
)


# ---------------------------------------------------------------------------
# classify_event
# ---------------------------------------------------------------------------


def test_classify_cycle_started():
    ev = {"event": "cycle_started", "ts": "2026-06-10T10:00:00+00:00"}
    assert classify_event(ev) == EventClass.CYCLE


def test_classify_cycle_completed():
    ev = {"event": "cycle_completed", "decisions_done": 5}
    assert classify_event(ev) == EventClass.CYCLE


def test_classify_decision_executed():
    ev = {
        "event": "decision_recorded",
        "action": "BUY",
        "executed": True,
        "reason": "ok",
    }
    assert classify_event(ev) == EventClass.DECISION_EXECUTED


def test_classify_decision_rejected_risk():
    ev = {
        "event": "decision_recorded",
        "action": "BUY",
        "executed": False,
        "reason": "risk:order_value_exceeded",
    }
    assert classify_event(ev) == EventClass.RISK_REJECT


def test_classify_decision_hold():
    ev = {
        "event": "decision_recorded",
        "action": "HOLD",
        "executed": False,
        "reason": "hold",
    }
    assert classify_event(ev) == EventClass.HOLD


def test_classify_stale():
    ev = {
        "event": "decision_recorded",
        "action": "HOLD",
        "executed": False,
        "reason": "stale_market_data",
    }
    assert classify_event(ev) == EventClass.STALE


def test_classify_watch_triggered():
    ev = {"event": "indicator_watch_triggered", "symbol": "AAPL"}
    assert classify_event(ev) == EventClass.WATCH


def test_classify_watch_expired():
    ev = {"event": "indicator_watch_expired", "symbol": "AAPL"}
    assert classify_event(ev) == EventClass.WATCH


def test_classify_learning_consolidated():
    ev = {"event": "learning_consolidated", "triggered": True}
    assert classify_event(ev) == EventClass.LEARNING


def test_classify_stale_backoff_event():
    """L'event de type stale_backoff (nouveau daemon) doit être classé STALE."""
    ev = {"event": "stale_backoff", "symbol": "SPY", "backoff_minutes": 30}
    assert classify_event(ev) == EventClass.STALE


def test_classify_stale_market_data_event_toplevel():
    """Un event stale_market_data au top-level (pas sous decision_recorded) → STALE."""
    ev = {"event": "stale_market_data", "symbol": "QQQ"}
    assert classify_event(ev) == EventClass.STALE


def test_classify_unknown_falls_back_to_other():
    ev = {"event": "some_future_event_type"}
    assert classify_event(ev) == EventClass.OTHER


# ---------------------------------------------------------------------------
# format_event_line
# ---------------------------------------------------------------------------


def test_format_event_line_decision_executed(monkeypatch):
    monkeypatch.setenv("CASYS_DISPLAY_TZ", "UTC")
    ev = {
        "ts": "2026-06-10T10:01:02+00:00",
        "event": "decision_recorded",
        "symbol": "AAPL",
        "action": "BUY",
        "executed": True,
        "reason": "ok",
    }
    line = format_event_line(ev)
    assert isinstance(line, EventLine)
    assert "AAPL" in line.text
    assert "BUY" in line.text
    assert "2026-06-10 10:01:02 +00:00" in line.text
    assert line.markup_class == EventClass.DECISION_EXECUTED


def test_format_event_line_cycle_started():
    ev = {
        "ts": "2026-06-10T09:00:00+00:00",
        "event": "cycle_started",
        "symbols_due": ["SPY", "QQQ"],
        "dry_run": True,
    }
    line = format_event_line(ev)
    assert "cycle" in line.text.lower()
    assert line.markup_class == EventClass.CYCLE


def test_format_event_line_convertit_l_utc_en_timezone_affichage(monkeypatch):
    monkeypatch.setenv("CASYS_DISPLAY_TZ", "Asia/Taipei")
    ev = {
        "ts": "2026-07-06T04:53:13+00:00",
        "event": "cycle_started",
        "symbols_due": ["2379.TW", "2633.TW"],
    }

    line = format_event_line(ev)

    assert line.text.startswith("2026-07-06 12:53:13 +08:00")


def test_format_event_line_risk_reject():
    ev = {
        "ts": "2026-06-10T10:02:00+00:00",
        "event": "decision_recorded",
        "symbol": "NZDUSD=X",
        "action": "BUY",
        "executed": False,
        "reason": "risk:order_value_exceeded",
    }
    line = format_event_line(ev)
    assert "NZDUSD=X" in line.text
    assert line.markup_class == EventClass.RISK_REJECT


def test_format_event_line_watch():
    ev = {
        "ts": "2026-06-10T10:05:00+00:00",
        "event": "indicator_watch_triggered",
        "symbol": "NG=F",
        "on_trigger": "WAKE",
    }
    line = format_event_line(ev)
    assert "NG=F" in line.text
    assert line.markup_class == EventClass.WATCH


def test_format_event_line_watch_expired(monkeypatch):
    monkeypatch.setenv("CASYS_DISPLAY_TZ", "Asia/Taipei")
    ev = {
        "ts": "2026-07-06T04:53:13+00:00",
        "event": "indicator_watch_expired",
        "symbol": "2379.TW",
        "watch_id": "2379.TW:old",
        "on_trigger": "WAKE",
    }

    line = format_event_line(ev)

    assert line.text == "2026-07-06 12:53:13 +08:00 ⚑ watch expired — 2379.TW (WAKE)"
    assert line.markup_class == EventClass.WATCH


def test_format_event_line_unknown_json_survives():
    """Une ligne JSON valide mais avec un event inconnu ne lève pas."""
    ev = {"ts": "2026-06-10T10:06:00+00:00", "event": "mystery_event", "foo": "bar"}
    line = format_event_line(ev)
    assert isinstance(line, EventLine)


# ---------------------------------------------------------------------------
# read_new_lines — tail incrémental
# ---------------------------------------------------------------------------


def test_read_new_lines_fichier_absent_retourne_vide():
    """Fichier absent → liste vide, offset 0, pas de crash."""
    lines, new_offset = read_new_lines(Path("/tmp/fichier_inexistant_cockpit.jsonl"), offset=0)
    assert lines == []
    assert new_offset == 0


def test_read_new_lines_lit_tout_au_premier_appel(tmp_path):
    f = tmp_path / "events.jsonl"
    f.write_text(
        '{"event":"cycle_started","ts":"2026-06-10T01:00:00+00:00"}\n'
        '{"event":"cycle_completed","ts":"2026-06-10T01:00:05+00:00"}\n',
        encoding="utf-8",
    )
    lines, offset = read_new_lines(f, offset=0)
    assert len(lines) == 2
    assert offset == f.stat().st_size


def test_read_new_lines_incremental_ne_rend_que_les_nouvelles(tmp_path):
    f = tmp_path / "events.jsonl"
    f.write_text(
        '{"event":"cycle_started","ts":"2026-06-10T01:00:00+00:00"}\n',
        encoding="utf-8",
    )
    lines1, off1 = read_new_lines(f, offset=0)
    assert len(lines1) == 1

    # Ajout d'une nouvelle ligne
    with f.open("a", encoding="utf-8") as fh:
        fh.write('{"event":"cycle_completed","ts":"2026-06-10T01:00:05+00:00"}\n')

    lines2, off2 = read_new_lines(f, offset=off1)
    assert len(lines2) == 1
    assert off2 > off1


def test_read_new_lines_fichier_tronque_resynchro(tmp_path):
    """Si le fichier est plus court que l'offset (rotation), recommence depuis 0."""
    f = tmp_path / "events.jsonl"
    f.write_text(
        '{"event":"cycle_started","ts":"2026-06-10T01:00:00+00:00"}\n'
        '{"event":"cycle_completed","ts":"2026-06-10T01:00:05+00:00"}\n',
        encoding="utf-8",
    )
    _, big_offset = read_new_lines(f, offset=0)
    assert big_offset > 0

    # Simule une troncature : on réécrit un fichier plus court
    f.write_text(
        '{"event":"cycle_started","ts":"2026-06-10T02:00:00+00:00"}\n',
        encoding="utf-8",
    )
    lines, new_offset = read_new_lines(f, offset=big_offset)
    # Doit avoir re-lu depuis le début
    assert len(lines) == 1
    assert new_offset == f.stat().st_size


def test_read_new_lines_ligne_json_cassee_est_ignoree(tmp_path):
    """Une ligne JSON invalide ne plante pas et est simplement ignorée."""
    f = tmp_path / "events.jsonl"
    f.write_text(
        '{"event":"cycle_started","ts":"2026-06-10T01:00:00+00:00"}\n'
        'JSON INVALIDE ICI !!!\n'
        '{"event":"cycle_completed","ts":"2026-06-10T01:00:05+00:00"}\n',
        encoding="utf-8",
    )
    lines, _ = read_new_lines(f, offset=0)
    # Seules les 2 lignes valides sont retournées
    assert len(lines) == 2


def test_read_new_lines_fichier_vide_retourne_vide(tmp_path):
    f = tmp_path / "events.jsonl"
    f.write_text("", encoding="utf-8")
    lines, offset = read_new_lines(f, offset=0)
    assert lines == []
    assert offset == 0


def test_read_new_lines_gros_fichier_initial_borne_aux_n_dernieres_lignes(tmp_path):
    """Au premier appel (offset=0), seules les _TAIL_LINES_INIT dernières lignes sont émises."""
    from trader.cockpit.events import _TAIL_LINES_INIT

    f = tmp_path / "events.jsonl"
    n_total = _TAIL_LINES_INIT + 50  # dépasse la borne
    lines_content = "".join(
        f'{{"event":"cycle_started","ts":"2026-06-10T{i:05d}+00:00"}}\n'
        for i in range(n_total)
    )
    f.write_text(lines_content, encoding="utf-8")

    lines, off = read_new_lines(f, offset=0)
    # Borne respectée
    assert len(lines) == _TAIL_LINES_INIT
    # Ce sont bien les dernières (les plus récentes)
    assert lines[-1]["ts"].startswith("2026-06-10")
    # Offset avancé jusqu'au dernier \\n consommé
    assert off > 0
    assert off <= f.stat().st_size


def test_read_new_lines_ligne_partielle_non_consommee(tmp_path):
    """Une demi-ligne JSON sans \\n final ne doit pas être consommée.

    Comportement attendu :
    - tick 1 : demi-ligne écrite → rien émis, offset pointe juste avant le fragment
    - tick 2 : ligne complétée + \\n → event émis entier
    """
    f = tmp_path / "events.jsonl"
    # Tick 1 : écriture sans \\n final
    fragment = '{"event":"cycle_started","ts":"2026-06-10T01:00:00+00:00"}'
    f.write_text(fragment, encoding="utf-8")
    lines1, off1 = read_new_lines(f, offset=0)
    # Rien émis car pas de \\n final
    assert lines1 == []
    # L'offset doit être 0 (fragment non consommé)
    assert off1 == 0

    # Tick 2 : complétion de la ligne avec \\n
    with f.open("a", encoding="utf-8") as fh:
        fh.write("\n")
    lines2, off2 = read_new_lines(f, offset=off1)
    # L'event entier doit ressortir
    assert len(lines2) == 1
    assert lines2[0]["event"] == "cycle_started"
    assert off2 == f.stat().st_size


def test_read_new_lines_plusieurs_lignes_dont_partielle(tmp_path):
    """Lignes complètes + fragment final : seules les complètes sont émises."""
    f = tmp_path / "events.jsonl"
    f.write_text(
        '{"event":"cycle_started","ts":"2026-06-10T01:00:00+00:00"}\n'
        '{"event":"cycle_completed"',  # pas de \\n
        encoding="utf-8",
    )
    lines, off = read_new_lines(f, offset=0)
    # Seule la première ligne complète est émise
    assert len(lines) == 1
    assert lines[0]["event"] == "cycle_started"
    # L'offset s'arrête après le dernier \\n, pas à EOF
    expected_consumed = len('{"event":"cycle_started","ts":"2026-06-10T01:00:00+00:00"}\n'.encode())
    assert off == expected_consumed


def test_read_new_lines_gros_fichier_lit_la_queue(tmp_path):
    """Fichier > 1 MiB : le premier appel retourne les DERNIÈRES lignes, pas les premières."""
    from trader.cockpit.events import _READ_CHUNK_SIZE, _TAIL_LINES_INIT

    f = tmp_path / "events.jsonl"
    # ~72 octets/ligne × 16 000 ≈ 1.15 MiB
    n_lines = 16_000
    content = "".join(
        f'{{"event":"cycle_started","ts":"2026-06-10T01:00:00+00:00","seq":{i}}}\n'
        for i in range(n_lines)
    )
    f.write_text(content, encoding="utf-8")
    size = f.stat().st_size
    assert size > _READ_CHUNK_SIZE, "le fichier doit dépasser 1 MiB pour valider le tail"

    result, new_offset = read_new_lines(f, offset=0)

    assert len(result) == _TAIL_LINES_INIT
    # Dernière ligne = fin du fichier
    assert result[-1]["seq"] == n_lines - 1
    # Première ligne retournée ≠ début du fichier
    assert result[0]["seq"] != 0
    # Offset avancé jusqu'à la fin du fichier
    assert new_offset == size


def test_read_new_lines_gros_fichier_incremental_apres_init(tmp_path):
    """Après le premier appel sur un gros fichier, le poll incrémental retourne exactement la nouvelle ligne."""
    from trader.cockpit.events import _READ_CHUNK_SIZE

    f = tmp_path / "events.jsonl"
    n_lines = 16_000
    content = "".join(
        f'{{"event":"cycle_started","ts":"2026-06-10T01:00:00+00:00","seq":{i}}}\n'
        for i in range(n_lines)
    )
    f.write_text(content, encoding="utf-8")
    assert f.stat().st_size > _READ_CHUNK_SIZE

    _, offset = read_new_lines(f, offset=0)
    assert offset == f.stat().st_size

    with f.open("a", encoding="utf-8") as fh:
        fh.write('{"event":"cycle_completed","seq":99999}\n')

    result2, offset2 = read_new_lines(f, offset=offset)
    assert len(result2) == 1
    assert result2[0]["seq"] == 99999
    assert offset2 == f.stat().st_size


def test_armed_plan_cancelled_formate_dedie() -> None:
    # review Codex (D7 étage B) : une annulation de plan armé est un signal
    # opérateur — pas un event générique OTHER.
    event = {
        "ts": "2026-06-11T12:04:31+00:00",
        "event": "armed_plan_cancelled",
        "symbol": "SPY",
        "plan_id": "SPY:abc123",
        "reason": "armed_plan_cancelled:stop_incoherent",
    }

    line = format_event_line(event)

    assert "armed order cancelled" in line.text
    assert "SPY" in line.text
    assert "stop_incoherent" in line.text
    assert classify_event(event) == EventClass.WATCH  # même famille que les triggers
