from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts import migrate_gbp_minor_quotes as migration


def _state(tmp_path: Path, *, open_london_position: bool = False) -> tuple[Path, Path]:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "casys.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE broker_state(id INTEGER PRIMARY KEY, cash REAL NOT NULL);
        CREATE TABLE broker_positions(symbol TEXT PRIMARY KEY, quantity REAL, avg_price REAL);
        CREATE TABLE broker_fills(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            side TEXT,
            quantity REAL,
            price REAL,
            ts TEXT,
            commission REAL,
            commission_currency TEXT,
            commission_model TEXT,
            fx_rate REAL
        );
        CREATE TABLE state_imports(store TEXT PRIMARY KEY, imported_at TEXT);
        """
    )
    fills = [
        ("SPY", "BUY", 1.0, 100.0, "2026-01-01T10:00:00+00:00", 0.35, "USD", "us", 1.0),
        ("BRBY.L", "SELL", 8.0, 1153.0, "2026-01-01T11:00:00+00:00", 0.35, "USD", "legacy", 1.27),
        ("BRBY.L", "BUY", 8.0, 1155.5, "2026-01-01T12:00:00+00:00", 0.35, "USD", "legacy", 1.27),
    ]
    conn.executemany(
        "INSERT INTO broker_fills(symbol,side,quantity,price,ts,commission,"
        "commission_currency,commission_model,fx_rate) VALUES(?,?,?,?,?,?,?,?,?)",
        fills,
    )
    cash = 100_000.0 - 100.0 - 0.35 + (8.0 * 1153.0 * 1.27) - 0.35 - (8.0 * 1155.5 * 1.27) - 0.35
    conn.execute("INSERT INTO broker_state(id,cash) VALUES(1,?)", (cash,))
    conn.execute(
        "INSERT INTO broker_positions(symbol,quantity,avg_price) VALUES('BRBY.L',?,0)",
        (1.0 if open_london_position else 0.0,),
    )
    conn.commit()
    conn.close()

    performance_path = state_dir / "model_performance.jsonl"
    rows = [
        {
            "symbol": symbol,
            "action": side,
            "quantity": quantity,
            "price": price,
            "ts": ts,
            "cash": 100_000.0 - index,
            "equity": 100_000.0 - index,
        }
        for index, (symbol, side, quantity, price, ts, *_) in enumerate(fills, start=1)
    ]
    performance_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    return db_path, performance_path


def _preflight(db_path: Path, performance_path: Path) -> dict:
    return migration.inspect(
        db_path,
        performance_path,
        starting_cash=100_000.0,
        through_seq=3,
    )


def _apply_reviewed(db_path: Path, performance_path: Path, *, preflight: dict | None = None) -> dict:
    reviewed = preflight or _preflight(db_path, performance_path)
    return migration.apply(
        db_path,
        performance_path,
        starting_cash=100_000.0,
        through_seq=3,
        expected_count=reviewed["candidate_count"],
        expected_manifest_sha256=reviewed["manifest_sha256"],
    )


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_dry_run_reconciles_exact_manifest_without_mutation(tmp_path: Path) -> None:
    db_path, performance_path = _state(tmp_path)
    before_db = db_path.read_bytes()
    before_perf = performance_path.read_text(encoding="utf-8")

    report = _preflight(db_path, performance_path)

    assert report["status"] == "ready"
    assert report["candidate_count"] == 2
    assert report["candidate_sequences"] == [2, 3]
    assert len(report["manifest_sha256"]) == 64
    assert len(report["normalized_manifest_sha256"]) == 64
    assert report["manifest_sha256"] != report["normalized_manifest_sha256"]
    assert report["projection_matches"] == 2
    assert report["economic_quality_marked"] == 2
    assert report["cash_delta"] == pytest.approx(25.146)
    assert db_path.read_bytes() == before_db
    assert performance_path.read_text(encoding="utf-8") == before_perf


def test_apply_normalizes_attests_verifies_and_is_idempotent(tmp_path: Path) -> None:
    db_path, performance_path = _state(tmp_path)
    preflight = _preflight(db_path, performance_path)
    original_first_line = performance_path.read_text(encoding="utf-8").splitlines()[0]

    report = _apply_reviewed(db_path, performance_path, preflight=preflight)

    assert report["status"] == "applied"
    assert report["verification"]["status"] == "verified"
    assert Path(report["db_backup"]).exists()
    assert Path(report["performance_backup"]).exists()
    conn = sqlite3.connect(db_path)
    prices = [
        row[0]
        for row in conn.execute(
            "SELECT price FROM broker_fills WHERE symbol='BRBY.L' ORDER BY seq"
        )
    ]
    cash = conn.execute("SELECT cash FROM broker_state WHERE id=1").fetchone()[0]
    sentinels = conn.execute(
        "SELECT store, imported_at FROM state_imports "
        "WHERE store=? OR store LIKE ? ORDER BY store",
        (migration.MIGRATION_KEY, f"{migration.MIGRATION_KEY}:scope:%"),
    ).fetchall()
    conn.close()
    assert prices == pytest.approx([11.53, 11.555])
    assert cash == pytest.approx(report["cash_after"])
    assert len(sentinels) == 2
    assert {row[1] for row in sentinels} == {report["imported_at"]}
    assert any(preflight["manifest_sha256"] in row[0] for row in sentinels)

    raw_lines = performance_path.read_text(encoding="utf-8").splitlines()
    assert raw_lines[0] == original_first_line
    rows = [json.loads(line) for line in raw_lines]
    london = [row for row in rows if row["symbol"] == "BRBY.L"]
    assert [row["price"] for row in london] == pytest.approx([11.53, 11.555])
    assert all(row["price_normalization"] == migration._PRICE_NORMALIZATION for row in london)
    assert "economic_fields_quality" not in rows[0]
    assert all(
        row["economic_fields_quality"] == migration._ECONOMIC_FIELDS_QUALITY
        for row in london
    )

    second = _apply_reviewed(db_path, performance_path, preflight=preflight)
    assert second["status"] == "already_applied"
    assert second["verification"]["status"] == "verified"


def test_idempotent_verification_accepts_fresh_unmarked_post_migration_fill(
    tmp_path: Path,
) -> None:
    db_path, performance_path = _state(tmp_path)
    preflight = _preflight(db_path, performance_path)
    _apply_reviewed(db_path, performance_path, preflight=preflight)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO broker_fills(symbol,side,quantity,price,ts,commission,"
        "commission_currency,commission_model,fx_rate) "
        "VALUES('QQQ','BUY',1,50,'2026-01-02T13:00:00+00:00',0.35,'USD','us',1)"
    )
    conn.execute("UPDATE broker_state SET cash=cash-50.35 WHERE id=1")
    conn.commit()
    conn.close()
    with performance_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "symbol": "QQQ",
                    "action": "BUY",
                    "quantity": 1,
                    "price": 50,
                    "ts": "2026-01-02T13:00:00+00:00",
                    "cash": 1,
                    "equity": 1,
                }
            )
            + "\n"
        )

    second = _apply_reviewed(db_path, performance_path, preflight=preflight)

    assert second["status"] == "already_applied"
    assert second["verification"]["status"] == "verified"
    qqq = next(row for row in _jsonl(performance_path) if row["symbol"] == "QQQ")
    assert "economic_fields_quality" not in qqq


def test_apply_refuses_count_manifest_and_empty_scope(tmp_path: Path) -> None:
    db_path, performance_path = _state(tmp_path)
    preflight = _preflight(db_path, performance_path)

    with pytest.raises(migration.MigrationRefused, match="portée inattendue"):
        migration.apply(
            db_path,
            performance_path,
            starting_cash=100_000.0,
            through_seq=3,
            expected_count=3,
            expected_manifest_sha256=preflight["manifest_sha256"],
        )
    with pytest.raises(migration.MigrationRefused, match="portée inattendue"):
        migration.apply(
            db_path,
            performance_path,
            starting_cash=100_000.0,
            through_seq=3,
            expected_count=2,
            expected_manifest_sha256="0" * 64,
        )
    with pytest.raises(migration.MigrationRefused, match="through_seq doit être > 0"):
        migration.inspect(
            db_path,
            performance_path,
            starting_cash=100_000.0,
            through_seq=0,
        )
    with pytest.raises(migration.MigrationRefused, match="population Londres vide"):
        migration.inspect(
            db_path,
            performance_path,
            starting_cash=100_000.0,
            through_seq=1,
        )
    with pytest.raises(migration.MigrationRefused, match="expected_count doit être > 0"):
        migration.apply(
            db_path,
            performance_path,
            starting_cash=100_000.0,
            through_seq=3,
            expected_count=0,
            expected_manifest_sha256=preflight["manifest_sha256"],
        )
    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT COUNT(*) FROM state_imports WHERE store LIKE ?",
        (f"{migration.MIGRATION_KEY}%",),
    ).fetchone()[0] == 0
    conn.close()


def test_dry_run_refuses_london_fill_beyond_reviewed_cutoff(tmp_path: Path) -> None:
    db_path, performance_path = _state(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO broker_fills(symbol,side,quantity,price,ts,commission,"
        "commission_currency,commission_model,fx_rate) "
        "VALUES('RR.L','BUY',1,1400,'2026-01-01T13:00:00+00:00',0.35,'USD','legacy',1.27)"
    )
    conn.execute("UPDATE broker_state SET cash=cash-(1400*1.27)-0.35 WHERE id=1")
    conn.commit()
    conn.close()
    with performance_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "symbol": "RR.L",
                    "action": "BUY",
                    "quantity": 1,
                    "price": 1400,
                    "ts": "2026-01-01T13:00:00+00:00",
                    "cash": 1,
                    "equity": 1,
                }
            )
            + "\n"
        )

    with pytest.raises(migration.MigrationRefused, match="cutoff Londres incomplet"):
        _preflight(db_path, performance_path)


def test_dry_run_refuses_cutoff_beyond_current_max_fill_seq(tmp_path: Path) -> None:
    db_path, performance_path = _state(tmp_path)

    with pytest.raises(migration.MigrationRefused, match=r"MAX\(seq\)"):
        migration.inspect(
            db_path,
            performance_path,
            starting_cash=100_000.0,
            through_seq=1_000,
        )


def test_inspect_refuses_attestation_with_divergent_quality_boundary(
    tmp_path: Path,
) -> None:
    db_path, performance_path = _state(tmp_path)
    report = _apply_reviewed(db_path, performance_path)
    corrupt_scope = migration.MigrationScope(
        through_seq=report["through_seq"],
        candidate_count=report["candidate_count"],
        quality_through_seq=report["through_seq"] - 1,
        manifest_sha256=report["manifest_sha256"],
        normalized_manifest_sha256=report["normalized_manifest_sha256"],
    )
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE state_imports SET store=? WHERE store GLOB ?",
        (corrupt_scope.store_key, f"{migration._SCOPE_PREFIX}*"),
    )
    conn.commit()
    conn.close()

    with pytest.raises(migration.MigrationRefused, match="attestation de portée invalide"):
        migration.inspect(
            db_path,
            performance_path,
            starting_cash=100_000.0,
            through_seq=report["through_seq"],
        )


def test_open_london_position_refuses_even_dry_run(tmp_path: Path) -> None:
    db_path, performance_path = _state(tmp_path, open_london_position=True)

    with pytest.raises(migration.MigrationRefused, match="positions Londres"):
        _preflight(db_path, performance_path)


@pytest.mark.parametrize("source", ["status", "pid_file"])
def test_apply_refuses_live_daemon_from_both_identity_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    db_path, performance_path = _state(tmp_path)
    preflight = _preflight(db_path, performance_path)
    monkeypatch.setattr(migration, "_daemon_pid_is_live", lambda pid: pid == 4242)
    if source == "status":
        (db_path.parent / "daemon_status.json").write_text(
            json.dumps({"pid": 4242, "phase": "cycle_completed"}),
            encoding="utf-8",
        )
    else:
        (db_path.parent / "daemon.pid").write_text("4242", encoding="utf-8")

    with pytest.raises(migration.MigrationRefused, match="daemon trader vivant"):
        _apply_reviewed(db_path, performance_path, preflight=preflight)

    assert not list(db_path.parent.glob("*.bak-pre-*"))


@pytest.mark.parametrize(
    ("field", "value"),
    [("action", "BUY"), ("quantity", 999.0)],
)
def test_projection_requires_full_fill_identity(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    db_path, performance_path = _state(tmp_path)
    rows = _jsonl(performance_path)
    rows[1][field] = value
    performance_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    with pytest.raises(migration.MigrationRefused, match="aucun fill canonique"):
        _preflight(db_path, performance_path)


def test_apply_refuses_intervening_fill_that_changes_max_seq(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, performance_path = _state(tmp_path)
    reviewed = _preflight(db_path, performance_path)
    original_inspect = migration.inspect
    injected = False

    def raced_inspect(*args, **kwargs):
        nonlocal injected
        report = original_inspect(*args, **kwargs)
        if not injected:
            injected = True
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO broker_fills(symbol,side,quantity,price,ts,commission,"
                "commission_currency,commission_model,fx_rate) "
                "VALUES('QQQ','BUY',1,50,'2026-01-01T13:00:00+00:00',0.35,'USD','us',1)"
            )
            conn.execute("UPDATE broker_state SET cash=cash-50.35 WHERE id=1")
            conn.commit()
            conn.close()
            with performance_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "symbol": "QQQ",
                            "action": "BUY",
                            "quantity": 1,
                            "price": 50,
                            "ts": "2026-01-01T13:00:00+00:00",
                            "cash": 1,
                            "equity": 1,
                        }
                    )
                    + "\n"
                )
        return report

    monkeypatch.setattr(migration, "inspect", raced_inspect)
    with pytest.raises(migration.MigrationRefused, match=r"MAX\(seq\)"):
        _apply_reviewed(db_path, performance_path, preflight=reviewed)

    conn = sqlite3.connect(db_path)
    stored_cash = conn.execute("SELECT cash FROM broker_state WHERE id=1").fetchone()[0]
    london_prices = [
        row[0]
        for row in conn.execute(
            "SELECT price FROM broker_fills WHERE symbol='BRBY.L' ORDER BY seq"
        )
    ]
    sentinel_count = conn.execute(
        "SELECT COUNT(*) FROM state_imports WHERE store GLOB ?",
        (f"{migration.MIGRATION_KEY}*",),
    ).fetchone()[0]
    conn.close()
    assert stored_cash == pytest.approx(reviewed["cash_before"] - 50.35)
    assert london_prices == pytest.approx([1153.0, 1155.5])
    assert sentinel_count == 0
    qqq = next(row for row in _jsonl(performance_path) if row["symbol"] == "QQQ")
    assert "economic_fields_quality" not in qqq
    london = [row for row in _jsonl(performance_path) if row["symbol"] == "BRBY.L"]
    assert [row["price"] for row in london] == pytest.approx([1153.0, 1155.5])
    assert all("price_normalization" not in row for row in london)


def test_exception_after_json_replace_rolls_back_db_and_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, performance_path = _state(tmp_path)
    reviewed = _preflight(db_path, performance_path)
    before_perf = performance_path.read_bytes()
    conn = sqlite3.connect(db_path)
    before_db = conn.execute(
        "SELECT cash, (SELECT price FROM broker_fills WHERE seq=2) FROM broker_state WHERE id=1"
    ).fetchone()
    conn.close()

    def fail_attestation(*args, **kwargs):
        raise RuntimeError("sentinel write failed")

    monkeypatch.setattr(migration, "_insert_attestation", fail_attestation)
    with pytest.raises(RuntimeError, match="sentinel write failed"):
        _apply_reviewed(db_path, performance_path, preflight=reviewed)

    conn = sqlite3.connect(db_path)
    after_db = conn.execute(
        "SELECT cash, (SELECT price FROM broker_fills WHERE seq=2) FROM broker_state WHERE id=1"
    ).fetchone()
    sentinel_count = conn.execute(
        "SELECT COUNT(*) FROM state_imports WHERE store LIKE ?",
        (f"{migration.MIGRATION_KEY}%",),
    ).fetchone()[0]
    conn.close()
    assert after_db == before_db
    assert sentinel_count == 0
    assert performance_path.read_bytes() == before_perf


def test_recovery_after_projection_replace_before_db_commit(tmp_path: Path) -> None:
    db_path, performance_path = _state(tmp_path)
    reviewed = _preflight(db_path, performance_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    prepared = migration._prepare_migration(
        conn,
        performance_path,
        starting_cash=100_000.0,
        through_seq=3,
    )
    conn.close()
    performance_path.write_text(
        "\n".join(prepared.rewrite.lines) + "\n",
        encoding="utf-8",
    )

    report = _apply_reviewed(db_path, performance_path, preflight=reviewed)

    assert report["status"] == "applied"
    assert report["verification"]["status"] == "verified"


def test_verifier_refuses_post_commit_projection_divergence(tmp_path: Path) -> None:
    db_path, performance_path = _state(tmp_path)
    report = _apply_reviewed(db_path, performance_path)
    rows = _jsonl(performance_path)
    rows[1]["price"] += 1.0
    performance_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    scope = migration.MigrationScope(
        through_seq=report["through_seq"],
        candidate_count=report["candidate_count"],
        quality_through_seq=report["quality_through_seq"],
        manifest_sha256=report["manifest_sha256"],
        normalized_manifest_sha256=report["normalized_manifest_sha256"],
    )

    with pytest.raises(migration.MigrationRefused, match="fill canonique absent"):
        migration.verify_applied(
            db_path,
            performance_path,
            starting_cash=100_000.0,
            expected_scope=scope,
        )
