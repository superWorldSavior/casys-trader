"""Tests de la rotation mensuelle gzip des ledgers JSONL."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

from trader.infrastructure.files.ledger_rotation import read_rows_with_archive, rotate_monthly


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW_JULY = datetime(2026, 7, 2, 10, 0, 0, tzinfo=timezone.utc)
_NOW_AUG = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _read_gz(path: Path) -> list[dict]:
    rows = []
    with gzip.open(path, "rt", encoding="utf-8") as gz:
        for line in gz:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _make_row(ts: str, symbol: str, ts_key: str = "cycle_ts") -> dict:
    return {ts_key: ts, "symbol": symbol, "action": "HOLD"}


# ---------------------------------------------------------------------------
# rotate_monthly — partition multi-mois
# ---------------------------------------------------------------------------


def test_rotate_partition_mois_passes_et_mois_courant(tmp_path: Path) -> None:
    """Les mois passés vont en archive ; le mois courant reste dans le vif."""
    ledger = tmp_path / "decisions.jsonl"
    archive_dir = tmp_path / "archive"

    rows = [
        _make_row("2026-05-01T10:00:00+00:00", "SPY"),
        _make_row("2026-06-15T12:00:00+00:00", "QQQ"),
        _make_row("2026-07-01T08:00:00+00:00", "AAPL"),  # mois courant (juillet)
    ]
    _write_jsonl(ledger, rows)

    result = rotate_monthly(ledger, archive_dir, now=_NOW_JULY, ts_key="cycle_ts")

    assert result["archived"] == 2
    assert result["kept"] == 1

    # Fichier vif = juillet uniquement
    live = _read_jsonl(ledger)
    assert len(live) == 1
    assert live[0]["symbol"] == "AAPL"

    # Archives : 2026-05 et 2026-06
    assert (archive_dir / "decisions-2026-05.jsonl.gz").exists()
    assert (archive_dir / "decisions-2026-06.jsonl.gz").exists()

    may_rows = _read_gz(archive_dir / "decisions-2026-05.jsonl.gz")
    assert len(may_rows) == 1
    assert may_rows[0]["symbol"] == "SPY"

    june_rows = _read_gz(archive_dir / "decisions-2026-06.jsonl.gz")
    assert len(june_rows) == 1
    assert june_rows[0]["symbol"] == "QQQ"


# ---------------------------------------------------------------------------
# rotate_monthly — append sur archive existante (2 rotations successives)
# ---------------------------------------------------------------------------


def test_rotate_append_sur_archive_existante_relisible(tmp_path: Path) -> None:
    """Deux rotations successives sur le même mois → gzip relisible intégralement."""
    ledger = tmp_path / "decisions.jsonl"
    archive_dir = tmp_path / "archive"

    # 1re rotation : 2 lignes de juin
    _write_jsonl(ledger, [
        _make_row("2026-06-10T10:00:00+00:00", "SPY"),
        _make_row("2026-06-20T15:00:00+00:00", "QQQ"),
        _make_row("2026-07-01T08:00:00+00:00", "AAPL"),
    ])
    rotate_monthly(ledger, archive_dir, now=_NOW_JULY, ts_key="cycle_ts")

    # Simuler un nouveau batch de juin apparu après la 1re rotation
    # (scénario edge-case : on repart d'un fichier avec une ligne de juin)
    _write_jsonl(ledger, [
        _make_row("2026-06-25T09:00:00+00:00", "MSFT"),
        _make_row("2026-07-02T11:00:00+00:00", "AAPL"),
    ])
    rotate_monthly(ledger, archive_dir, now=_NOW_JULY, ts_key="cycle_ts")

    # L'archive juin doit contenir les 3 lignes de juin (2 + 1)
    june_archive = archive_dir / "decisions-2026-06.jsonl.gz"
    assert june_archive.exists()
    june_rows = _read_gz(june_archive)
    assert len(june_rows) == 3
    symbols = {r["symbol"] for r in june_rows}
    assert symbols == {"SPY", "QQQ", "MSFT"}

    # Fichier vif = la seule ligne de juillet du 2e write (le 1er write a été écrasé)
    live = _read_jsonl(ledger)
    assert len(live) == 1
    assert live[0]["symbol"] == "AAPL"


# ---------------------------------------------------------------------------
# rotate_monthly — lignes sans ts conservées dans le vif
# ---------------------------------------------------------------------------


def test_rotate_lignes_sans_ts_restent_dans_vif(tmp_path: Path) -> None:
    """Les lignes sans champ ts valide ne sont pas archivées."""
    ledger = tmp_path / "decisions.jsonl"
    archive_dir = tmp_path / "archive"

    rows_raw = [
        json.dumps({"cycle_ts": "2026-05-10T00:00:00+00:00", "symbol": "SPY"}),
        json.dumps({"symbol": "ORPHAN_NO_TS"}),              # pas de ts
        json.dumps({"cycle_ts": "", "symbol": "EMPTY_TS"}),  # ts vide
        json.dumps({"cycle_ts": "not-a-date", "symbol": "BAD_TS"}),  # ts invalide
    ]
    ledger.write_text("\n".join(rows_raw) + "\n", encoding="utf-8")

    result = rotate_monthly(ledger, archive_dir, now=_NOW_JULY, ts_key="cycle_ts")

    assert result["archived"] == 1  # seulement la ligne de mai
    assert result["kept"] == 3      # les 3 lignes sans ts valide

    live = _read_jsonl(ledger)
    symbols = {r["symbol"] for r in live}
    assert symbols == {"ORPHAN_NO_TS", "EMPTY_TS", "BAD_TS"}


# ---------------------------------------------------------------------------
# rotate_monthly — fichier vif = mois courant uniquement
# ---------------------------------------------------------------------------


def test_rotate_fichier_vif_contient_seulement_mois_courant(tmp_path: Path) -> None:
    """Après rotation, le fichier vif ne contient que les lignes du mois courant."""
    ledger = tmp_path / "decisions.jsonl"
    archive_dir = tmp_path / "archive"

    rows = [
        _make_row("2026-03-01T00:00:00+00:00", "A"),
        _make_row("2026-04-01T00:00:00+00:00", "B"),
        _make_row("2026-05-01T00:00:00+00:00", "C"),
        _make_row("2026-06-01T00:00:00+00:00", "D"),
        _make_row("2026-07-01T00:00:00+00:00", "E"),  # juillet = mois courant
        _make_row("2026-07-02T00:00:00+00:00", "F"),
    ]
    _write_jsonl(ledger, rows)

    result = rotate_monthly(ledger, archive_dir, now=_NOW_JULY, ts_key="cycle_ts")

    assert result["archived"] == 4
    assert result["kept"] == 2

    live = _read_jsonl(ledger)
    assert {r["symbol"] for r in live} == {"E", "F"}
    assert all(r["cycle_ts"].startswith("2026-07") for r in live)


# ---------------------------------------------------------------------------
# rotate_monthly — atomicité : le tmp est remplacé
# ---------------------------------------------------------------------------


def test_rotate_atomicite_pas_de_tmp_orphelin(tmp_path: Path) -> None:
    """Après rotation réussie, le fichier .tmp ne subsiste pas."""
    ledger = tmp_path / "decisions.jsonl"
    archive_dir = tmp_path / "archive"

    _write_jsonl(ledger, [_make_row("2026-06-01T00:00:00+00:00", "SPY"),
                          _make_row("2026-07-01T00:00:00+00:00", "QQQ")])
    rotate_monthly(ledger, archive_dir, now=_NOW_JULY, ts_key="cycle_ts")

    assert not (tmp_path / "decisions.tmp").exists()


# ---------------------------------------------------------------------------
# rotate_monthly — fichier absent : pas d'erreur
# ---------------------------------------------------------------------------


def test_rotate_fichier_absent_renvoie_zero(tmp_path: Path) -> None:
    result = rotate_monthly(
        tmp_path / "missing.jsonl",
        tmp_path / "archive",
        now=_NOW_JULY,
        ts_key="cycle_ts",
    )
    assert result == {"archived": 0, "kept": 0, "files": []}


# ---------------------------------------------------------------------------
# rotate_monthly — clé ts_key "ts" (events.jsonl)
# ---------------------------------------------------------------------------


def test_rotate_events_avec_ts_key_ts(tmp_path: Path) -> None:
    """La rotation fonctionne aussi avec ts_key='ts' (events.jsonl)."""
    ledger = tmp_path / "events.jsonl"
    archive_dir = tmp_path / "archive"

    rows = [
        {"ts": "2026-06-01T10:00:00+00:00", "event": "foo"},
        {"ts": "2026-07-01T10:00:00+00:00", "event": "bar"},
    ]
    _write_jsonl(ledger, rows)

    result = rotate_monthly(ledger, archive_dir, now=_NOW_JULY, ts_key="ts")

    assert result["archived"] == 1
    assert result["kept"] == 1
    assert (archive_dir / "events-2026-06.jsonl.gz").exists()


# ---------------------------------------------------------------------------
# read_rows_with_archive — chaîne archives + vif
# ---------------------------------------------------------------------------


def test_read_rows_with_archive_chaine_archives_puis_vif(tmp_path: Path) -> None:
    """read_rows_with_archive retourne les archives (chrono) puis le fichier vif."""
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    ledger = tmp_path / "decisions.jsonl"

    # Écrire 2 archives gzip
    for month, symbol in [("2026-05", "SPY"), ("2026-06", "QQQ")]:
        archive_path = archive_dir / f"decisions-{month}.jsonl.gz"
        with gzip.open(archive_path, "wt", encoding="utf-8") as gz:
            gz.write(json.dumps({"cycle_ts": f"{month}-01T00:00:00+00:00", "symbol": symbol}) + "\n")

    # Écrire le fichier vif (mois courant)
    _write_jsonl(ledger, [{"cycle_ts": "2026-07-01T00:00:00+00:00", "symbol": "AAPL"}])

    rows = list(read_rows_with_archive(ledger, archive_dir))

    assert len(rows) == 3
    assert rows[0]["symbol"] == "SPY"   # 2026-05 (archive)
    assert rows[1]["symbol"] == "QQQ"   # 2026-06 (archive)
    assert rows[2]["symbol"] == "AAPL"  # 2026-07 (vif)


def test_read_rows_with_archive_sans_archives(tmp_path: Path) -> None:
    """Sans archive_dir existant, seul le fichier vif est retourné."""
    ledger = tmp_path / "decisions.jsonl"
    _write_jsonl(ledger, [{"cycle_ts": "2026-07-01T00:00:00+00:00", "symbol": "SPY"}])

    rows = list(read_rows_with_archive(ledger, tmp_path / "archive"))  # archive_dir absent

    assert len(rows) == 1
    assert rows[0]["symbol"] == "SPY"


def test_read_rows_with_archive_ordre_chronologique_archives(tmp_path: Path) -> None:
    """Les archives sont lues dans l'ordre alphabétique (= chrono YYYY-MM)."""
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    ledger = tmp_path / "decisions.jsonl"
    ledger.write_text("", encoding="utf-8")

    months = ["2026-03", "2026-01", "2026-05", "2026-02", "2026-04"]
    for month in months:
        archive_path = archive_dir / f"decisions-{month}.jsonl.gz"
        with gzip.open(archive_path, "wt", encoding="utf-8") as gz:
            gz.write(json.dumps({"cycle_ts": f"{month}-01T00:00:00+00:00", "month": month}) + "\n")

    rows = list(read_rows_with_archive(ledger, archive_dir))

    assert len(rows) == 5
    assert [r["month"] for r in rows] == sorted(months)


def test_read_rows_with_archive_archive_gzip_concatenes_relisibles(tmp_path: Path) -> None:
    """Un fichier gzip avec membres concaténés (résultat de 2 rotations) est relisible."""
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    ledger = tmp_path / "decisions.jsonl"
    ledger.write_text("", encoding="utf-8")

    archive_path = archive_dir / "decisions-2026-06.jsonl.gz"
    # Simuler 2 appends (2 membres gzip concaténés)
    with gzip.open(archive_path, "ab") as gz:
        gz.write(json.dumps({"cycle_ts": "2026-06-10T00:00:00+00:00", "symbol": "SPY"}).encode() + b"\n")
    with gzip.open(archive_path, "ab") as gz:
        gz.write(json.dumps({"cycle_ts": "2026-06-20T00:00:00+00:00", "symbol": "QQQ"}).encode() + b"\n")

    rows = list(read_rows_with_archive(ledger, archive_dir))

    assert len(rows) == 2
    assert {r["symbol"] for r in rows} == {"SPY", "QQQ"}


# ---------------------------------------------------------------------------
# Intégration rotate_monthly → read_rows_with_archive
# ---------------------------------------------------------------------------


def test_rotation_puis_lecture_integrale(tmp_path: Path) -> None:
    """Après rotation, read_rows_with_archive restitue toutes les lignes originales."""
    ledger = tmp_path / "decisions.jsonl"
    archive_dir = tmp_path / "archive"

    original_rows = [
        _make_row("2026-05-01T10:00:00+00:00", "A"),
        _make_row("2026-06-15T12:00:00+00:00", "B"),
        _make_row("2026-07-01T08:00:00+00:00", "C"),
        _make_row("2026-07-02T09:00:00+00:00", "D"),
    ]
    _write_jsonl(ledger, original_rows)

    rotate_monthly(ledger, archive_dir, now=_NOW_JULY, ts_key="cycle_ts")

    all_rows = list(read_rows_with_archive(ledger, archive_dir))
    assert len(all_rows) == 4
    symbols = [r["symbol"] for r in all_rows]
    assert set(symbols) == {"A", "B", "C", "D"}
    # Archives d'abord (chrono), puis vif
    assert symbols.index("A") < symbols.index("C")
    assert symbols.index("B") < symbols.index("C")


# ---------------------------------------------------------------------------
# rotate_monthly — crash-safe : pas de doublon si archive déjà écrite (Finding 1a)
# ---------------------------------------------------------------------------


def test_rotate_crash_safe_pas_de_doublon_si_archive_deja_ecrite(tmp_path: Path) -> None:
    """Simule un crash entre écriture archive et replace du vif.

    Scénario :
      - L'archive mai est déjà écrite (crash après étape archive, avant replace vif).
      - Le vif contient encore les lignes de mai ET juillet.
      - rotate_monthly relancé (= daemon suivant) → fusionne + dédup → 0 doublon.
    """
    ledger = tmp_path / "decisions.jsonl"
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()

    may_row = _make_row("2026-05-10T00:00:00+00:00", "SPY")
    july_row = _make_row("2026-07-01T00:00:00+00:00", "AAPL")
    # Vif « avant crash » : mai + juillet
    _write_jsonl(ledger, [may_row, july_row])

    # Archive mai déjà écrite avant le crash
    archive_may = archive_dir / "decisions-2026-05.jsonl.gz"
    with gzip.open(archive_may, "wt", encoding="utf-8") as gz:
        gz.write(json.dumps(may_row) + "\n")

    # Relance (simulation du redémarrage post-crash)
    rotate_monthly(ledger, archive_dir, now=_NOW_JULY, ts_key="cycle_ts")

    # Aucun doublon dans l'archive mai
    may_rows = _read_gz(archive_may)
    assert len(may_rows) == 1, f"Doublon détecté : {len(may_rows)} lignes, attendu 1"
    assert may_rows[0]["symbol"] == "SPY"

    # Le vif = juillet uniquement
    live = _read_jsonl(ledger)
    assert len(live) == 1
    assert live[0]["symbol"] == "AAPL"


# ---------------------------------------------------------------------------
# rotate_monthly — archive corrompue → rotation abortée, vif intact (Finding 1b)
# ---------------------------------------------------------------------------


def test_rotate_archive_corrompue_abort_vif_intact_et_log_error(tmp_path: Path) -> None:
    """Archive existante corrompue (tronquée) → rotation de ce mois abortée.

    - Aucune ligne n'est perdue (les lignes du mois aborté restent dans le vif).
    - log.error émis avec chemin de l'archive.
    """
    import logging as _logging

    ledger = tmp_path / "decisions.jsonl"
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()

    may_row = _make_row("2026-05-10T00:00:00+00:00", "SPY")
    july_row = _make_row("2026-07-01T00:00:00+00:00", "AAPL")
    _write_jsonl(ledger, [may_row, july_row])

    # Archive corrompue pour mai (tronquée au milieu)
    archive_may = archive_dir / "decisions-2026-05.jsonl.gz"
    with gzip.open(archive_may, "wt", encoding="utf-8") as gz:
        gz.write(json.dumps(_make_row("2026-05-01T00:00:00+00:00", "EXISTING")) + "\n" * 300)
    raw = archive_may.read_bytes()
    archive_may.write_bytes(raw[: len(raw) // 2])

    records: list[_logging.LogRecord] = []
    handler = _logging.Handler()
    handler.emit = records.append  # type: ignore[assignment]
    module_logger = _logging.getLogger("trader.infrastructure.files.ledger_rotation")
    module_logger.addHandler(handler)
    try:
        result = rotate_monthly(ledger, archive_dir, now=_NOW_JULY, ts_key="cycle_ts")
    finally:
        module_logger.removeHandler(handler)

    # Aucune ligne archivée (abort)
    assert result["archived"] == 0

    # Les deux lignes (mai + juillet) sont dans le vif — rien de perdu
    live = _read_jsonl(ledger)
    live_symbols = {r["symbol"] for r in live}
    assert "SPY" in live_symbols   # mai : resté dans le vif (abort)
    assert "AAPL" in live_symbols  # juillet : toujours dans le vif

    # log.error avec mention de l'archive corrompue
    assert any("corrompue" in rec.getMessage() for rec in records), (
        "Un log.error avec 'corrompue' doit être émis"
    )


# ---------------------------------------------------------------------------
# rotate_monthly — re-rotation sur archive saine existante → fusion sans doublon (Finding 1c)
# ---------------------------------------------------------------------------


def test_rotate_fusion_sans_doublon_sur_archive_saine(tmp_path: Path) -> None:
    """Deux rotations distinctes sur le même mois, archive saine → fusion, 0 doublon."""
    ledger = tmp_path / "decisions.jsonl"
    archive_dir = tmp_path / "archive"

    # 1re rotation : 1 ligne de mai
    _write_jsonl(ledger, [
        _make_row("2026-05-01T00:00:00+00:00", "SPY"),
        _make_row("2026-07-01T00:00:00+00:00", "AAPL"),
    ])
    rotate_monthly(ledger, archive_dir, now=_NOW_JULY, ts_key="cycle_ts")

    # 2e rotation : nouvelle ligne de mai (arrivée après coup)
    _write_jsonl(ledger, [
        _make_row("2026-05-15T00:00:00+00:00", "QQQ"),
        _make_row("2026-07-02T00:00:00+00:00", "AAPL"),
    ])
    rotate_monthly(ledger, archive_dir, now=_NOW_JULY, ts_key="cycle_ts")

    # Archive mai = SPY + QQQ, sans doublon
    may_rows = _read_gz(archive_dir / "decisions-2026-05.jsonl.gz")
    assert len(may_rows) == 2
    assert {r["symbol"] for r in may_rows} == {"SPY", "QQQ"}


# ---------------------------------------------------------------------------


def test_archive_tronquee_est_sautee_sans_crash(tmp_path) -> None:
    """Un membre gzip tronqué par un crash pendant la rotation (zlib.error, pas
    BadGzipFile) ne doit pas casser toute l'analyse — skip + warning (review D-c)."""
    import gzip as _gzip
    import logging as _logging

    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    # Archive valide pour un mois...
    with _gzip.open(archive_dir / "decisions-2026-05.jsonl.gz", "wt", encoding="utf-8") as gz:
        gz.write('{"cycle_ts": "2026-05-15T00:00:00+00:00", "symbol": "OK5"}\n')
    # ...et une archive TRONQUÉE au milieu d'un bloc DEFLATE pour un autre mois
    with _gzip.open(archive_dir / "decisions-2026-06.jsonl.gz", "wt", encoding="utf-8") as gz:
        gz.write('{"cycle_ts": "2026-06-15T00:00:00+00:00", "symbol": "CORRUPT"}\n' * 200)
    raw = (archive_dir / "decisions-2026-06.jsonl.gz").read_bytes()
    (archive_dir / "decisions-2026-06.jsonl.gz").write_bytes(raw[: len(raw) // 2])

    vif = tmp_path / "decisions.jsonl"
    vif.write_text('{"cycle_ts": "2026-07-01T00:00:00+00:00", "symbol": "VIF"}\n', encoding="utf-8")

    # Handler direct sur le logger du module : indépendant de la config logging
    # globale (un test voisin peut couper la propagation vers root/caplog).
    records: list[_logging.LogRecord] = []
    handler = _logging.Handler()
    handler.emit = records.append  # type: ignore[assignment]
    module_logger = _logging.getLogger("trader.infrastructure.files.ledger_rotation")
    module_logger.addHandler(handler)
    try:
        rows = list(read_rows_with_archive(vif, archive_dir))
    finally:
        module_logger.removeHandler(handler)

    symbols = [r.get("symbol") for r in rows]
    assert "OK5" in symbols and "VIF" in symbols  # le valide et le vif passent
    assert any("illisible" in rec.getMessage() for rec in records)
