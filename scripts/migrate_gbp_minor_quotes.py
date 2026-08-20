"""Normalize legacy London fills recorded in pence as if they were GBP.

The runtime now converts Yahoo ``GBp``/``GBX`` OHLC values to GBP at the
market-source boundary.  Older paper fills predate that boundary and therefore
store prices 100 times too large.  This migration repairs the canonical SQLite
ledger and its ``model_performance.jsonl`` projection together.

Safety contract:

- dry-run by default;
- ``--apply`` requires an exact SHA-256 manifest, fill count and sequence cutoff;
- refuses ``--apply`` while a live ``trader.daemon`` is identified from either
  canonical PID source;
- refuses to run while a London position is open;
- verifies the existing cash ledger before changing it;
- creates SQLite and JSONL backups before mutation;
- records an ISO-dated sentinel and a scope attestation in the same SQLite
  transaction as the fills;
- never guesses from a live symbol suffix: the cutoff freezes the reviewed
  legacy population before the normalized runtime is restarted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trader.domain.market import fx
from trader.runtime import pid_file as runtime_pid_file

MIGRATION_KEY = "gbp_minor_quotes_v1"
_CASH_TOLERANCE = 1e-6
_SCOPE_PREFIX = f"{MIGRATION_KEY}:scope:v1:"
_PRICE_NORMALIZATION = {
    "migration": MIGRATION_KEY,
    "scale": 0.01,
    "source_unit": "GBp_or_GBX",
    "target_unit": "GBP",
}
_ECONOMIC_FIELDS_QUALITY = {
    "status": "stale",
    "reason": "legacy_gbp_minor_quote_accounting_not_reconstructed",
    "migration": MIGRATION_KEY,
    "fields": ["cash", "equity"],
}


class MigrationRefused(RuntimeError):
    """Raised when the reviewed legacy population cannot be proven safely."""


@dataclass(frozen=True)
class MigrationScope:
    through_seq: int
    candidate_count: int
    quality_through_seq: int
    manifest_sha256: str
    normalized_manifest_sha256: str

    @property
    def store_key(self) -> str:
        return (
            f"{_SCOPE_PREFIX}{self.through_seq}:{self.candidate_count}:"
            f"{self.quality_through_seq}:"
            f"{self.manifest_sha256}:{self.normalized_manifest_sha256}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "through_seq": self.through_seq,
            "candidate_count": self.candidate_count,
            "quality_through_seq": self.quality_through_seq,
            "manifest_sha256": self.manifest_sha256,
            "normalized_manifest_sha256": self.normalized_manifest_sha256,
        }


@dataclass(frozen=True)
class MigrationAttestation:
    scope: MigrationScope
    imported_at: str


@dataclass(frozen=True)
class PerformanceRewrite:
    lines: list[str]
    target_matches: int
    quality_marked: int
    source_sha256: str


@dataclass(frozen=True)
class PreparedMigration:
    rows: list[sqlite3.Row]
    candidates: list[sqlite3.Row]
    scope: MigrationScope
    cash_before: float
    cash_after: float
    rewrite: PerformanceRewrite

    def report(self) -> dict[str, Any]:
        candidate_sequences = [int(row["seq"]) for row in self.candidates]
        return {
            "status": "ready",
            "migration": MIGRATION_KEY,
            **self.scope.as_dict(),
            "candidate_sequences": candidate_sequences,
            "projection_matches": self.rewrite.target_matches,
            "projection_missing": [],
            "economic_quality_marked": self.rewrite.quality_marked,
            "cash_before": self.cash_before,
            "cash_after": self.cash_after,
            "cash_delta": self.cash_after - self.cash_before,
        }


def _is_london_symbol(symbol: object) -> bool:
    return str(symbol or "").strip().upper().endswith(".L")


def _daemon_pid_is_live(pid: int) -> bool:
    """Return True only for a live process identified as ``trader.daemon``."""
    return runtime_pid_file._is_daemon_pid(pid)  # noqa: SLF001 - canonical identity check


def _read_pid(value: object, *, source: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        pid = int(value)
    except (TypeError, ValueError) as exc:
        raise MigrationRefused(f"{source}: PID illisible") from exc
    if pid <= 0:
        raise MigrationRefused(f"{source}: PID invalide {pid}")
    return pid


def _assert_daemon_stopped(state_dir: Path) -> None:
    """Refuse apply when either canonical daemon locator names a live daemon.

    Both files are inspected because a manual launch can leave only the status
    file, while a normal supervised launch owns ``daemon.pid``.  Stale numeric
    PIDs are harmless only after command-line identity has been checked.
    """

    candidates: dict[str, int] = {}
    status_path = state_dir / "daemon_status.json"
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise MigrationRefused(
                f"{status_path}: status illisible, arrêt du daemon non prouvé"
            ) from exc
        if not isinstance(status, dict):
            raise MigrationRefused(
                f"{status_path}: status invalide, arrêt du daemon non prouvé"
            )
        pid = _read_pid(status.get("pid"), source=str(status_path))
        if pid is not None:
            candidates["daemon_status.json"] = pid

    pid_path = state_dir / "daemon.pid"
    if pid_path.exists():
        try:
            raw_pid = pid_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise MigrationRefused(
                f"{pid_path}: lecture impossible, arrêt du daemon non prouvé"
            ) from exc
        pid = _read_pid(raw_pid, source=str(pid_path))
        if pid is not None:
            candidates["daemon.pid"] = pid

    live = sorted(
        {(source, pid) for source, pid in candidates.items() if _daemon_pid_is_live(pid)}
    )
    if live:
        details = ", ".join(f"{source}=PID {pid}" for source, pid in live)
        raise MigrationRefused(f"daemon trader vivant: {details}")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _float_hex(value: object, *, context: str) -> str:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise MigrationRefused(f"{context}: nombre invalide {value!r}") from exc
    if not math.isfinite(parsed):
        raise MigrationRefused(f"{context}: nombre non fini")
    return parsed.hex()


def _optional_float_hex(value: object, *, context: str) -> str | None:
    if value is None:
        return None
    return _float_hex(value, context=context)


def _manifest_sha256(
    candidates: list[sqlite3.Row],
    *,
    through_seq: int,
    price_scale: float,
) -> str:
    rows: list[dict[str, Any]] = []
    for row in sorted(candidates, key=lambda candidate: int(candidate["seq"])):
        seq = int(row["seq"])
        price = float(row["price"]) * price_scale
        rows.append(
            {
                "seq": seq,
                "symbol": str(row["symbol"]),
                "side": str(row["side"]),
                "quantity": _float_hex(row["quantity"], context=f"fill seq={seq} quantity"),
                "price": _float_hex(price, context=f"fill seq={seq} price"),
                "ts": str(row["ts"]),
                "commission": _optional_float_hex(
                    row["commission"],
                    context=f"fill seq={seq} commission",
                ),
                "commission_currency": str(row["commission_currency"]),
                "commission_model": str(row["commission_model"]),
                "fx_rate": _float_hex(row["fx_rate"], context=f"fill seq={seq} fx_rate"),
            }
        )
    payload = json.dumps(
        {
            "schema": "gbp_minor_quotes_manifest_v1",
            "migration": MIGRATION_KEY,
            "through_seq": through_seq,
            "candidates": rows,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _validate_sha256(value: str, *, name: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise MigrationRefused(f"{name}: SHA-256 invalide")
    return normalized


def _signed_quantity(row: sqlite3.Row) -> float:
    side = str(row["side"] or "").upper()
    quantity = float(row["quantity"])
    if side == "BUY":
        return quantity
    if side == "SELL":
        return -quantity
    raise MigrationRefused(f"fill seq={row['seq']}: side invalide {side!r}")


def _commission_usd(row: sqlite3.Row) -> float:
    amount = float(row["commission"] or 0.0)
    currency = str(row["commission_currency"] or "USD").upper()
    if currency == fx.BASE_CCY:
        return amount
    symbol_currency = fx.currency_for(str(row["symbol"]))
    if currency != symbol_currency:
        raise MigrationRefused(
            f"fill seq={row['seq']}: commission {currency} sans taux propre "
            f"(devise symbole={symbol_currency})"
        )
    return amount * float(row["fx_rate"])


def _cash_from_fills(
    rows: list[sqlite3.Row],
    *,
    starting_cash: float,
    normalized_sequences: set[int],
) -> float:
    cash = float(starting_cash)
    for row in rows:
        price = float(row["price"])
        rate = float(row["fx_rate"])
        quantity = float(row["quantity"])
        if not all(math.isfinite(value) for value in (price, rate, quantity)):
            raise MigrationRefused(f"fill seq={row['seq']}: valeur non finie")
        if price <= 0.0 or rate <= 0.0 or quantity < 0.0:
            raise MigrationRefused(f"fill seq={row['seq']}: prix/taux/quantité invalide")
        if int(row["seq"]) in normalized_sequences:
            price *= 0.01
        cash -= _signed_quantity(row) * price * rate
        cash -= _commission_usd(row)
    return cash


def _fill_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT seq, symbol, side, quantity, price, ts, commission, "
        "commission_currency, commission_model, fx_rate "
        "FROM broker_fills ORDER BY seq"
    ).fetchall()


def _candidate_rows(
    rows: list[sqlite3.Row],
    *,
    through_seq: int,
) -> list[sqlite3.Row]:
    if through_seq <= 0:
        raise MigrationRefused(f"through_seq doit être > 0 (reçu {through_seq})")
    candidates = [
        row
        for row in rows
        if int(row["seq"]) <= through_seq and _is_london_symbol(row["symbol"])
    ]
    if not candidates:
        raise MigrationRefused("population Londres vide: aucun sentinel ne sera écrit")
    return candidates


def _parse_scope_store(store: str) -> MigrationScope:
    if not store.startswith(_SCOPE_PREFIX):
        raise MigrationRefused(f"attestation de portée invalide: {store!r}")
    fields = store[len(_SCOPE_PREFIX):].split(":")
    if len(fields) != 5:
        raise MigrationRefused(f"attestation de portée illisible: {store!r}")
    through_raw, count_raw, quality_through_raw, manifest_raw, normalized_raw = fields
    try:
        through_seq = int(through_raw)
        candidate_count = int(count_raw)
        quality_through_seq = int(quality_through_raw)
    except ValueError as exc:
        raise MigrationRefused(f"attestation de portée illisible: {store!r}") from exc
    if (
        through_seq <= 0
        or candidate_count <= 0
        or quality_through_seq <= 0
        or quality_through_seq != through_seq
    ):
        raise MigrationRefused(f"attestation de portée invalide: {store!r}")
    return MigrationScope(
        through_seq=through_seq,
        candidate_count=candidate_count,
        quality_through_seq=quality_through_seq,
        manifest_sha256=_validate_sha256(manifest_raw, name="manifest atteste"),
        normalized_manifest_sha256=_validate_sha256(
            normalized_raw,
            name="manifest normalise atteste",
        ),
    )


def _migration_attestation(
    conn: sqlite3.Connection,
) -> MigrationAttestation | None:
    primary = conn.execute(
        "SELECT imported_at FROM state_imports WHERE store=?",
        (MIGRATION_KEY,),
    ).fetchone()
    scope_rows = conn.execute(
        "SELECT store, imported_at FROM state_imports WHERE store GLOB ?",
        (f"{_SCOPE_PREFIX}*",),
    ).fetchall()
    if primary is None and not scope_rows:
        return None
    if primary is None or len(scope_rows) != 1:
        raise MigrationRefused(
            "sentinel migration partiel ou ambigu: attestation de portée absente"
        )
    imported_at = str(primary["imported_at"] or "")
    scope_imported_at = str(scope_rows[0]["imported_at"] or "")
    if not imported_at or scope_imported_at != imported_at:
        raise MigrationRefused("timestamps des sentinels migration divergents")
    try:
        parsed = datetime.fromisoformat(imported_at)
    except ValueError as exc:
        raise MigrationRefused("timestamp du sentinel migration illisible") from exc
    if parsed.tzinfo is None:
        raise MigrationRefused("timestamp du sentinel migration sans fuseau")
    return MigrationAttestation(
        scope=_parse_scope_store(str(scope_rows[0]["store"])),
        imported_at=imported_at,
    )


def _attestation_report(attestation: MigrationAttestation) -> dict[str, Any]:
    return {
        "status": "already_applied",
        "migration": MIGRATION_KEY,
        **attestation.scope.as_dict(),
        "imported_at": attestation.imported_at,
    }


def _assert_expected_scope(
    scope: MigrationScope,
    *,
    through_seq: int,
    expected_count: int,
    expected_manifest_sha256: str,
) -> None:
    if scope.quality_through_seq != scope.through_seq:
        raise MigrationRefused(
            "portée invalide: quality_through_seq doit égaler through_seq"
        )
    expected_manifest = _validate_sha256(
        expected_manifest_sha256,
        name="expected_manifest_sha256",
    )
    if expected_count <= 0:
        raise MigrationRefused(
            f"expected_count doit être > 0 (reçu {expected_count})"
        )
    expected = {
        "through_seq": through_seq,
        "candidate_count": expected_count,
        "manifest_sha256": expected_manifest,
    }
    actual = {
        "through_seq": scope.through_seq,
        "candidate_count": scope.candidate_count,
        "manifest_sha256": scope.manifest_sha256,
    }
    if actual != expected:
        raise MigrationRefused(
            "portée inattendue: "
            f"expected={json.dumps(expected, sort_keys=True)} "
            f"actual={json.dumps(actual, sort_keys=True)}"
        )


def _insert_attestation(
    conn: sqlite3.Connection,
    *,
    scope: MigrationScope,
    imported_at: str,
) -> None:
    conn.executemany(
        "INSERT INTO state_imports(store, imported_at) VALUES(?, ?)",
        [
            (MIGRATION_KEY, imported_at),
            (scope.store_key, imported_at),
        ],
    )


def _open_london_positions(conn: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT symbol FROM broker_positions "
            "WHERE UPPER(symbol) LIKE '%.L' AND ABS(quantity) > 1e-8 "
            "ORDER BY symbol"
        ).fetchall()
    ]


ProjectionIdentity = tuple[str, str, str, str, str]


def _projection_identity_from_values(
    *,
    symbol: object,
    side: object,
    quantity: object,
    price: object,
    ts: object,
    context: str,
) -> ProjectionIdentity:
    normalized_side = str(side or "").upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise MigrationRefused(f"{context}: side/action invalide {normalized_side!r}")
    normalized_symbol = str(symbol or "")
    normalized_ts = str(ts or "")
    if not normalized_symbol or not normalized_ts:
        raise MigrationRefused(f"{context}: symbol/ts absent")
    return (
        normalized_symbol,
        normalized_side,
        _float_hex(quantity, context=f"{context} quantity"),
        _float_hex(price, context=f"{context} price"),
        normalized_ts,
    )


def _db_projection_identity(
    row: sqlite3.Row,
    *,
    price_scale: float = 1.0,
) -> ProjectionIdentity:
    seq = int(row["seq"])
    return _projection_identity_from_values(
        symbol=row["symbol"],
        side=row["side"],
        quantity=row["quantity"],
        price=float(row["price"]) * price_scale,
        ts=row["ts"],
        context=f"fill seq={seq}",
    )


def _json_projection_identity(row: dict[str, Any], *, line_number: int) -> ProjectionIdentity:
    return _projection_identity_from_values(
        symbol=row.get("symbol"),
        side=row.get("side") or row.get("action"),
        quantity=row.get("quantity"),
        price=row.get("price"),
        ts=row.get("ts"),
        context=f"projection ligne={line_number}",
    )


def _identity_index(
    rows: list[sqlite3.Row],
    *,
    price_scale: float = 1.0,
) -> dict[ProjectionIdentity, sqlite3.Row]:
    index: dict[ProjectionIdentity, sqlite3.Row] = {}
    for row in rows:
        identity = _db_projection_identity(row, price_scale=price_scale)
        if identity in index:
            raise MigrationRefused(
                "fills canoniques avec identité projection dupliquée: "
                f"seq={index[identity]['seq']} et seq={row['seq']}"
            )
        index[identity] = row
    return index


def _read_performance_source(path: Path) -> tuple[list[str], str]:
    if not path.exists():
        raise MigrationRefused(f"projection absente: {path}")
    try:
        payload = path.read_bytes()
        text = payload.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MigrationRefused(f"projection illisible: {path}") from exc
    return text.splitlines(), _sha256_bytes(payload)


def _performance_rewrite(
    path: Path,
    *,
    rows: list[sqlite3.Row],
    candidates: list[sqlite3.Row],
) -> PerformanceRewrite:
    source_lines, source_sha256 = _read_performance_source(path)
    all_raw = _identity_index(rows)
    target_normalized = _identity_index(candidates, price_scale=0.01)
    target_sequences = {int(row["seq"]) for row in candidates}
    affected_from_seq = min(target_sequences)

    output: list[str] = []
    matched_sequences: set[int] = set()
    matched_targets: set[int] = set()
    quality_marked = 0
    for line_number, raw_line in enumerate(source_lines, start=1):
        if not raw_line.strip():
            output.append(raw_line)
            continue
        try:
            parsed = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise MigrationRefused(
                f"projection ligne={line_number}: JSON invalide"
            ) from exc
        if not isinstance(parsed, dict):
            raise MigrationRefused(
                f"projection ligne={line_number}: objet JSON attendu"
            )
        row: dict[str, Any] = parsed
        identity = _json_projection_identity(row, line_number=line_number)
        source = all_raw.get(identity)
        target_state = "raw"
        if source is None:
            source = target_normalized.get(identity)
            target_state = "normalized"
        if source is None:
            raise MigrationRefused(
                f"projection ligne={line_number}: aucun fill canonique de même identité"
            )
        seq = int(source["seq"])
        if seq in matched_sequences:
            raise MigrationRefused(f"projection dupliquée pour fill seq={seq}")
        matched_sequences.add(seq)

        if seq in target_sequences:
            marker = row.get("price_normalization")
            if target_state == "normalized":
                if marker != _PRICE_NORMALIZATION:
                    raise MigrationRefused(
                        f"projection normalisée sans marker exact pour fill seq={seq}"
                    )
            else:
                if marker is not None:
                    raise MigrationRefused(
                        f"projection brute avec marker préexistant pour fill seq={seq}"
                    )
                row["price"] = float(source["price"]) * 0.01
                row["price_normalization"] = dict(_PRICE_NORMALIZATION)
            matched_targets.add(seq)

        if seq >= affected_from_seq:
            existing_quality = row.get("economic_fields_quality")
            if existing_quality not in (None, _ECONOMIC_FIELDS_QUALITY):
                raise MigrationRefused(
                    f"qualité économique préexistante divergente pour fill seq={seq}"
                )
            row["economic_fields_quality"] = {
                **_ECONOMIC_FIELDS_QUALITY,
                "fields": list(_ECONOMIC_FIELDS_QUALITY["fields"]),
            }
            quality_marked += 1

        if seq in target_sequences or seq >= affected_from_seq:
            output.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        else:
            output.append(raw_line)

    missing_fills = sorted({int(row["seq"]) for row in rows} - matched_sequences)
    if missing_fills:
        preview = ", ".join(str(seq) for seq in missing_fills[:20])
        raise MigrationRefused(f"projection incomplète: fills sans ligne {preview}")
    missing_targets = sorted(target_sequences - matched_targets)
    if missing_targets:
        raise MigrationRefused(
            "projection Londres incomplète: "
            + ", ".join(str(seq) for seq in missing_targets)
        )
    return PerformanceRewrite(
        lines=output,
        target_matches=len(matched_targets),
        quality_marked=quality_marked,
        source_sha256=source_sha256,
    )


def _prepare_migration(
    conn: sqlite3.Connection,
    performance_path: Path,
    *,
    starting_cash: float,
    through_seq: int,
) -> PreparedMigration:
    open_positions = _open_london_positions(conn)
    if open_positions:
        raise MigrationRefused(
            "positions Londres encore ouvertes: " + ", ".join(open_positions)
        )
    rows = _fill_rows(conn)
    candidates = _candidate_rows(rows, through_seq=through_seq)
    later_london = [
        int(row["seq"])
        for row in rows
        if int(row["seq"]) > through_seq and _is_london_symbol(row["symbol"])
    ]
    if later_london:
        raise MigrationRefused(
            "cutoff Londres incomplet: fills .L au-delà de through_seq: "
            + ", ".join(str(seq) for seq in later_london[:20])
        )
    max_fill_seq = max(int(row["seq"]) for row in rows)
    if through_seq != max_fill_seq:
        raise MigrationRefused(
            "through_seq doit être exactement le MAX(seq) courant: "
            f"through_seq={through_seq} max_seq={max_fill_seq}"
        )
    candidate_sequences = {int(row["seq"]) for row in candidates}
    stored_row = conn.execute("SELECT cash FROM broker_state WHERE id=1").fetchone()
    if stored_row is None:
        raise MigrationRefused("broker_state absent")
    stored_cash = float(stored_row["cash"])
    recomputed_before = _cash_from_fills(
        rows,
        starting_cash=starting_cash,
        normalized_sequences=set(),
    )
    if not math.isclose(
        stored_cash,
        recomputed_before,
        rel_tol=0.0,
        abs_tol=_CASH_TOLERANCE,
    ):
        raise MigrationRefused(
            "cash existant non réconcilié: "
            f"stored={stored_cash:.12f} recomputed={recomputed_before:.12f}"
        )
    cash_after = _cash_from_fills(
        rows,
        starting_cash=starting_cash,
        normalized_sequences=candidate_sequences,
    )
    scope = MigrationScope(
        through_seq=through_seq,
        candidate_count=len(candidates),
        quality_through_seq=max_fill_seq,
        manifest_sha256=_manifest_sha256(
            candidates,
            through_seq=through_seq,
            price_scale=1.0,
        ),
        normalized_manifest_sha256=_manifest_sha256(
            candidates,
            through_seq=through_seq,
            price_scale=0.01,
        ),
    )
    rewrite = _performance_rewrite(
        performance_path,
        rows=rows,
        candidates=candidates,
    )
    return PreparedMigration(
        rows=rows,
        candidates=candidates,
        scope=scope,
        cash_before=stored_cash,
        cash_after=cash_after,
        rewrite=rewrite,
    )


def inspect(
    db_path: Path,
    performance_path: Path,
    *,
    starting_cash: float,
    through_seq: int,
) -> dict[str, Any]:
    if through_seq <= 0:
        raise MigrationRefused(f"through_seq doit être > 0 (reçu {through_seq})")
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        attestation = _migration_attestation(conn)
        if attestation is not None:
            return _attestation_report(attestation)
        return _prepare_migration(
            conn,
            performance_path,
            starting_cash=starting_cash,
            through_seq=through_seq,
        ).report()
    finally:
        conn.close()


def _backup_sqlite(db_path: Path, backup_path: Path) -> None:
    source = sqlite3.connect(str(db_path))
    destination = sqlite3.connect(str(backup_path))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    os.chmod(backup_path, 0o600)


def _write_temp_lines(path: Path, lines: list[str]) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write("\n".join(lines))
            handle.write("\n" if lines else "")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, path.stat().st_mode & 0o777)
        return temp_path
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _performance_verify(
    path: Path,
    *,
    rows: list[sqlite3.Row],
    candidates: list[sqlite3.Row],
    quality_through_seq: int,
) -> dict[str, int]:
    source_lines, _ = _read_performance_source(path)
    all_current = _identity_index(rows)
    target_sequences = {int(row["seq"]) for row in candidates}
    affected_from_seq = min(target_sequences)
    matched_sequences: set[int] = set()
    verified_targets = 0
    quality_marked = 0

    for line_number, raw_line in enumerate(source_lines, start=1):
        if not raw_line.strip():
            continue
        try:
            parsed = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise MigrationRefused(
                f"vérification projection ligne={line_number}: JSON invalide"
            ) from exc
        if not isinstance(parsed, dict):
            raise MigrationRefused(
                f"vérification projection ligne={line_number}: objet attendu"
            )
        identity = _json_projection_identity(parsed, line_number=line_number)
        source = all_current.get(identity)
        if source is None:
            raise MigrationRefused(
                f"vérification projection ligne={line_number}: fill canonique absent"
            )
        seq = int(source["seq"])
        if seq in matched_sequences:
            raise MigrationRefused(
                f"vérification projection dupliquée pour fill seq={seq}"
            )
        matched_sequences.add(seq)
        if seq in target_sequences:
            if parsed.get("price_normalization") != _PRICE_NORMALIZATION:
                raise MigrationRefused(
                    f"vérification marker prix divergent pour fill seq={seq}"
                )
            verified_targets += 1
        if affected_from_seq <= seq <= quality_through_seq:
            if parsed.get("economic_fields_quality") != _ECONOMIC_FIELDS_QUALITY:
                raise MigrationRefused(
                    f"vérification qualité économique absente pour fill seq={seq}"
                )
            quality_marked += 1

    expected_historical_sequences = {
        int(row["seq"])
        for row in rows
        if int(row["seq"]) <= quality_through_seq
    }
    missing = sorted(expected_historical_sequences - matched_sequences)
    if missing:
        raise MigrationRefused(
            "vérification projection incomplète: fills manquants "
            + ", ".join(str(seq) for seq in missing[:20])
        )
    if verified_targets != len(candidates):
        raise MigrationRefused(
            "vérification projection Londres incomplète: "
            f"expected={len(candidates)} actual={verified_targets}"
        )
    return {
        "projection_matches": verified_targets,
        "economic_quality_marked": quality_marked,
    }


def verify_applied(
    db_path: Path,
    performance_path: Path,
    *,
    starting_cash: float,
    expected_scope: MigrationScope | None = None,
) -> dict[str, Any]:
    """Re-read every durable effect; a sentinel alone is never verification."""

    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        attestation = _migration_attestation(conn)
        if attestation is None:
            raise MigrationRefused("vérification: sentinel migration absent")
        if expected_scope is not None and attestation.scope != expected_scope:
            raise MigrationRefused("vérification: portée attestée divergente")
        rows = _fill_rows(conn)
        candidates = _candidate_rows(
            rows,
            through_seq=attestation.scope.through_seq,
        )
        if len(candidates) != attestation.scope.candidate_count:
            raise MigrationRefused(
                "vérification population: "
                f"attested={attestation.scope.candidate_count} actual={len(candidates)}"
            )
        if max(int(row["seq"]) for row in candidates) > attestation.scope.quality_through_seq:
            raise MigrationRefused(
                "vérification: borne de qualité antérieure aux fills migrés"
            )
        normalized_manifest = _manifest_sha256(
            candidates,
            through_seq=attestation.scope.through_seq,
            price_scale=1.0,
        )
        if normalized_manifest != attestation.scope.normalized_manifest_sha256:
            raise MigrationRefused(
                "vérification: manifest des fills normalisés divergent"
            )
        stored_row = conn.execute("SELECT cash FROM broker_state WHERE id=1").fetchone()
        if stored_row is None:
            raise MigrationRefused("vérification: broker_state absent")
        stored_cash = float(stored_row["cash"])
        recomputed_cash = _cash_from_fills(
            rows,
            starting_cash=starting_cash,
            normalized_sequences=set(),
        )
        if not math.isclose(
            stored_cash,
            recomputed_cash,
            rel_tol=0.0,
            abs_tol=_CASH_TOLERANCE,
        ):
            raise MigrationRefused(
                "vérification cash divergente: "
                f"stored={stored_cash:.12f} recomputed={recomputed_cash:.12f}"
            )
        projection = _performance_verify(
            performance_path,
            rows=rows,
            candidates=candidates,
            quality_through_seq=attestation.scope.quality_through_seq,
        )
        return {
            "status": "verified",
            "migration": MIGRATION_KEY,
            **attestation.scope.as_dict(),
            "imported_at": attestation.imported_at,
            "cash": stored_cash,
            **projection,
        }
    finally:
        conn.close()


def apply(
    db_path: Path,
    performance_path: Path,
    *,
    starting_cash: float,
    through_seq: int,
    expected_count: int,
    expected_manifest_sha256: str,
) -> dict[str, Any]:
    if through_seq <= 0:
        raise MigrationRefused(f"through_seq doit être > 0 (reçu {through_seq})")
    if expected_count <= 0:
        raise MigrationRefused(
            f"expected_count doit être > 0 (reçu {expected_count})"
        )
    expected_manifest_sha256 = _validate_sha256(
        expected_manifest_sha256,
        name="expected_manifest_sha256",
    )
    state_dir = db_path.parent
    _assert_daemon_stopped(state_dir)

    preflight = inspect(
        db_path,
        performance_path,
        starting_cash=starting_cash,
        through_seq=through_seq,
    )
    preflight_scope = MigrationScope(
        through_seq=int(preflight["through_seq"]),
        candidate_count=int(preflight["candidate_count"]),
        quality_through_seq=int(preflight["quality_through_seq"]),
        manifest_sha256=str(preflight["manifest_sha256"]),
        normalized_manifest_sha256=str(preflight["normalized_manifest_sha256"]),
    )
    _assert_expected_scope(
        preflight_scope,
        through_seq=through_seq,
        expected_count=expected_count,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    if preflight["status"] == "already_applied":
        verified = verify_applied(
            db_path,
            performance_path,
            starting_cash=starting_cash,
            expected_scope=preflight_scope,
        )
        return {**preflight, "verification": verified}

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    db_backup = db_path.with_name(f"{db_path.name}.bak-pre-{MIGRATION_KEY}-{timestamp}")
    perf_backup = performance_path.with_name(
        f"{performance_path.name}.bak-pre-{MIGRATION_KEY}-{timestamp}"
    )
    if db_backup.exists() or perf_backup.exists():
        raise MigrationRefused("chemin de backup déjà existant")
    _backup_sqlite(db_path, db_backup)
    shutil.copy2(performance_path, perf_backup)
    os.chmod(perf_backup, 0o600)

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    temp_path: Path | None = None
    performance_replaced = False
    prepared: PreparedMigration | None = None
    imported_at: str | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        _assert_daemon_stopped(state_dir)
        if _migration_attestation(conn) is not None:
            raise MigrationRefused("migration appliquée concurremment")
        prepared = _prepare_migration(
            conn,
            performance_path,
            starting_cash=starting_cash,
            through_seq=through_seq,
        )
        _assert_expected_scope(
            prepared.scope,
            through_seq=through_seq,
            expected_count=expected_count,
            expected_manifest_sha256=expected_manifest_sha256,
        )
        temp_path = _write_temp_lines(performance_path, prepared.rewrite.lines)
        if _file_sha256(performance_path) != prepared.rewrite.source_sha256:
            raise MigrationRefused(
                "projection modifiée pendant la migration; aucun remplacement effectué"
            )

        conn.executemany(
            "UPDATE broker_fills SET price = ? WHERE seq = ?",
            [
                (float(row["price"]) * 0.01, int(row["seq"]))
                for row in prepared.candidates
            ],
        )
        cash_update = conn.execute(
            "UPDATE broker_state SET cash=? WHERE id=1",
            (prepared.cash_after,),
        )
        if cash_update.rowcount != 1:
            raise MigrationRefused("broker_state disparu pendant la migration")
        os.replace(temp_path, performance_path)
        performance_replaced = True
        temp_path = None
        _fsync_directory(performance_path.parent)
        imported_at = datetime.now(timezone.utc).isoformat()
        _insert_attestation(
            conn,
            scope=prepared.scope,
            imported_at=imported_at,
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        if performance_replaced:
            shutil.copy2(perf_backup, performance_path)
            _fsync_directory(performance_path.parent)
        raise
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        conn.close()

    if prepared is None or imported_at is None:
        raise MigrationRefused("migration terminée sans attestation en mémoire")
    verified = verify_applied(
        db_path,
        performance_path,
        starting_cash=starting_cash,
        expected_scope=prepared.scope,
    )
    return {
        **prepared.report(),
        "status": "applied",
        "imported_at": imported_at,
        "db_backup": str(db_backup),
        "performance_backup": str(perf_backup),
        "verification": verified,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path("state"))
    parser.add_argument("--starting-cash", type=float, default=100_000.0)
    parser.add_argument("--through-seq", type=int, required=True)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    db_path = args.state / "casys.db"
    performance_path = args.state / "model_performance.jsonl"
    try:
        if args.apply:
            if args.expected_count is None or args.expected_manifest_sha256 is None:
                parser.error(
                    "--apply exige --expected-count et --expected-manifest-sha256"
                )
            report = apply(
                db_path,
                performance_path,
                starting_cash=args.starting_cash,
                through_seq=args.through_seq,
                expected_count=args.expected_count,
                expected_manifest_sha256=args.expected_manifest_sha256,
            )
        else:
            report = inspect(
                db_path,
                performance_path,
                starting_cash=args.starting_cash,
                through_seq=args.through_seq,
            )
    except MigrationRefused as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, indent=2))
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
