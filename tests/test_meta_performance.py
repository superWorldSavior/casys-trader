import json
import os
from pathlib import Path

from trader.reporting.audit import decision_quality as decision_audit
from trader.reporting.read_models import meta_performance


def _row(symbol: str, action: str, reason_code: str) -> dict:
    row = {
        "decision_id": f"2026-06-08T10:00:00+00:00|0|{symbol}",
        "cycle_ts": "2026-06-08T10:00:00+00:00",
        "symbol": symbol,
        "action": action,
        "price": 100.0,
        "decision_reason_code": reason_code,
    }
    if action == "HOLD" and reason_code == "WAITING_PULLBACK":
        row["opportunity_side"] = "long"
    return row


def test_compute_meta_performance_resume_les_hold_missed_par_reason(tmp_path) -> None:
    rows = [
        _row("PULLBACK", "HOLD", "WAITING_PULLBACK"),
        _row("NOEDGE", "HOLD", "NO_EDGE"),
        _row("ENTRY", "BUY", "ENTRY_SIGNAL"),
    ]
    prices = {
        "PULLBACK": [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 102.0},
        ],
        "NOEDGE": [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 100.1},
        ],
        "ENTRY": [
            {"ts": "2026-06-08T10:00:00+00:00", "close": 100.0},
            {"ts": "2026-06-08T11:00:00+00:00", "close": 99.0},
        ],
    }
    audit = decision_audit.audit_rows(rows, prices, horizons=["1h"], threshold_pct=0.5)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "decision_audit.json").write_text(json.dumps(audit), encoding="utf-8")

    payload = meta_performance.compute_meta_performance(state_dir, horizons=("1h",))

    assert payload["available"] is True
    assert payload["horizons"]["1h"]["global"]["missed_known_pct"] == 33.33
    hold_rows = {row["reason_code"]: row for row in payload["horizons"]["1h"]["hold_quality_by_reason"]}
    assert hold_rows["WAITING_PULLBACK"]["missed_known_pct"] == 100.0
    assert hold_rows["NO_EDGE"]["good_known_pct"] == 100.0
    assert hold_rows["WAITING_PULLBACK"]["coverage_pct"] == 100.0
    trade_rows = payload["horizons"]["1h"]["trade_quality_by_reason"]
    assert trade_rows == [
        {
            "action": "BUY",
            "reason_code": "ENTRY_SIGNAL",
            "known": 1,
            "good": 0,
            "bad": 1,
            "neutral": 0,
            "good_known_pct": 0.0,
            "bad_known_pct": 100.0,
            "neutral_known_pct": 0.0,
        }
    ]


def test_compute_meta_performance_absent_si_pas_daudit(tmp_path) -> None:
    payload = meta_performance.compute_meta_performance(tmp_path)

    assert payload == {"available": False, "reason": "decision_audit_missing"}


def test_compute_meta_performance_signale_audit_json_invalide(tmp_path) -> None:
    (tmp_path / "decision_audit.json").write_text("{not-json", encoding="utf-8")

    payload = meta_performance.compute_meta_performance(tmp_path)

    assert payload == {"available": False, "reason": "decision_audit_invalid_json"}


def test_compute_meta_performance_signale_audit_illisible(monkeypatch, tmp_path) -> None:
    audit_path = tmp_path / "decision_audit.json"
    audit_path.write_text("{}", encoding="utf-8")

    def unreadable(self: Path, *args, **kwargs):
        if self == audit_path:
            raise OSError("permission denied")
        return original_read_text(self, *args, **kwargs)

    original_read_text = Path.read_text
    monkeypatch.setattr(Path, "read_text", unreadable)

    payload = meta_performance.compute_meta_performance(tmp_path)

    assert payload == {"available": False, "reason": "decision_audit_unreadable"}


def test_compute_meta_performance_signale_schema_audit_invalide(tmp_path) -> None:
    (tmp_path / "decision_audit.json").write_text("[]", encoding="utf-8")

    payload = meta_performance.compute_meta_performance(tmp_path)

    assert payload == {"available": False, "reason": "decision_audit_invalid_schema"}


def test_compute_meta_performance_migre_les_anciens_audits_directionless(tmp_path) -> None:
    audit = {
        "threshold_pct": 0.5,
        "horizons": ["1h"],
        "rows": [
            {
                "decision_id": "2026-06-08T10:00:00+00:00|0|SPY",
                "cycle_ts": "2026-06-08T10:00:00+00:00",
                "symbol": "SPY",
                "action": "HOLD",
                "rationale": "attendre pullback propre",
                "audits": {
                    "1h": {
                        "entry_price": 100.0,
                        "future_price": 102.0,
                        "future_return_pct": 2.0,
                        "verdict": "missed",
                    }
                },
            }
        ],
    }
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "decision_audit.json").write_text(json.dumps(audit), encoding="utf-8")

    payload = meta_performance.compute_meta_performance(state_dir, horizons=("1h",))

    assert payload["horizons"]["1h"]["hold_quality_by_reason"] == [
        {
            "reason_code": "WAITING_PULLBACK",
            "total": 1,
            "known": 0,
            "unknown": 1,
            "good": 0,
            "missed": 0,
            "coverage_pct": 0.0,
            "good_known_pct": None,
            "missed_known_pct": None,
        }
    ]


# ---------------------------------------------------------------------------
# Cache mtime de _read_audit (P0 data-lifecycle §3.2)
# ---------------------------------------------------------------------------


def _minimal_audit(tmp_path: Path) -> Path:
    """Écrit un decision_audit.json minimal valide et retourne son chemin."""
    audit_path = tmp_path / "decision_audit.json"
    audit_path.write_text(
        json.dumps({"horizons": [], "metrics": {}, "rows": []}), encoding="utf-8"
    )
    return audit_path


def test_mtime_cache_deux_appels_sans_modif_une_seule_lecture(monkeypatch, tmp_path) -> None:
    """Deux appels consécutifs sans modification du fichier → read_text appelée
    une seule fois (le 2e appel utilise le cache mtime)."""
    audit_path = _minimal_audit(tmp_path)
    # Vider le cache du module entre tests (clé unique par tmp_path de toute façon,
    # mais on s'assure qu'aucune entrée résiduelle de cette clé n'existe).
    meta_performance._AUDIT_CACHE.pop(str(audit_path), None)

    read_count = 0
    original_read_text = Path.read_text

    def counting_read_text(self: Path, *args, **kwargs):
        nonlocal read_count
        if self == audit_path:
            read_count += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    meta_performance.compute_meta_performance(tmp_path)
    meta_performance.compute_meta_performance(tmp_path)

    assert read_count == 1, f"read_text appelée {read_count} fois (attendu 1)"


def test_mtime_cache_relecture_si_mtime_change(monkeypatch, tmp_path) -> None:
    """Si le fichier est modifié (mtime change), le cache est invalidé et le
    fichier est relu."""
    audit_path = _minimal_audit(tmp_path)
    meta_performance._AUDIT_CACHE.pop(str(audit_path), None)

    read_count = 0
    original_read_text = Path.read_text

    def counting_read_text(self: Path, *args, **kwargs):
        nonlocal read_count
        if self == audit_path:
            read_count += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    meta_performance.compute_meta_performance(tmp_path)  # lecture 1

    # Forcer un mtime différent en récrivant le fichier.
    # mtime forcé (review P0) : sleep(10ms) ne suffit pas sur les FS à
    # résolution 1-2 s (ext3, FAT, certains NFS) — utime est déterministe.
    stat = audit_path.stat()
    os.utime(audit_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    audit_path.write_text(
        json.dumps({"horizons": ["1h"], "metrics": {}, "rows": []}), encoding="utf-8"
    )

    meta_performance.compute_meta_performance(tmp_path)  # lecture 2

    assert read_count == 2, f"read_text appelée {read_count} fois (attendu 2)"
