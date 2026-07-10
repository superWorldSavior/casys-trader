"""State adapters for portfolio dashboards."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import duckdb

from trader.domain.portfolio.allocation import PositionLike
from trader.domain.semantic import catalog
from trader.domain.contracts import Fill
from trader.infrastructure.state_db.broker_store import SqliteBroker
from trader.infrastructure.state_db.connection import open_state_db

_EQUITY_SQL = """
SELECT
    cycle_ts::VARCHAR AS ts,
    cast(cycle_ts AS DATE)::VARCHAR AS jour,
    cast(portfolio_snapshot.equity AS DOUBLE) AS equity,
    cast(portfolio_snapshot.total_return_pct AS DOUBLE) AS total_return_pct
FROM read_json_auto(?, format='newline_delimited', union_by_name=true,
                    maximum_object_size=33554432)
WHERE portfolio_snapshot.equity IS NOT NULL
ORDER BY cycle_ts
"""


def symbol_family_map() -> dict[str, str]:
    """Return the semantic-catalog symbol -> family mapping."""
    families = getattr(catalog, "FAMILIES", {}) or {}
    return {symbol: family for family, symbols in families.items() for symbol in symbols}


def load_open_positions(state_dir: Path) -> list[PositionLike]:
    """Load current open broker positions from the canonical SQLite state."""
    db_path = state_dir / "casys.db"
    if not db_path.is_file():
        raise FileNotFoundError(f"base introuvable : {db_path}")

    broker = SqliteBroker(open_state_db(db_path))
    return list(broker.positions().values())


def load_fills(state_dir: Path) -> tuple[list[Fill], str]:
    """Load real broker fills from SQLite, with broker.json as historical fallback."""
    db_path = state_dir / "casys.db"
    broker_json_path = state_dir / "broker.json"
    if db_path.is_file():
        rows = SqliteBroker(open_state_db(db_path)).fills()
        source = f"{db_path}:broker_fills"
    elif broker_json_path.is_file():
        raw = json.loads(broker_json_path.read_text(encoding="utf-8"))
        rows = raw.get("fills", [])
        source = f"{broker_json_path}:fills"
    else:
        raise FileNotFoundError(f"aucune source de fills trouvee dans {state_dir}")

    fills = [Fill(**row) for row in rows]
    if not fills:
        raise ValueError(f"aucun fill dans {source}")
    return fills, source


def load_equity_rows(state_dir: Path) -> list[dict[str, object]]:
    """Load portfolio equity snapshots from decisions.jsonl."""
    decisions_path = state_dir / "decisions.jsonl"
    if not decisions_path.is_file():
        return []

    con = duckdb.connect()
    try:
        cur = con.execute(_EQUITY_SQL, [decisions_path.as_posix()])
        cols = [desc[0] for desc in cur.description]
        rows: list[dict[str, object]] = []
        for values in cur.fetchall():
            row = dict(zip(cols, values))
            for key, value in list(row.items()):
                if isinstance(value, (dt.date, dt.datetime)):
                    row[key] = value.isoformat()
            rows.append(row)
        return rows
    finally:
        con.close()
