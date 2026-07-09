"""State adapters for decision dashboards."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb

from trader.infrastructure.state_db.portfolio_dashboard_state import symbol_family_map

_DECISION_CHART_SQL = """
SELECT
    symbol,
    action,
    cast(cycle_ts AS DATE)::VARCHAR AS jour
FROM read_json_auto(?, format='newline_delimited', union_by_name=true,
                    maximum_object_size=33554432)
"""

_DECISION_DASHBOARD_SQL = """
SELECT
    cycle_ts,
    cast(cycle_ts AS DATE)::VARCHAR                                   AS jour,
    symbol, action, intent,
    qty, confidence, price, executed, model_called,
    decision_reason_code, decision_source, llm_model,
    news.news_count                                                  AS news_count,
    news.earnings_in_h                                               AS earnings_in_h,
    news.news_coverage                                               AS news_coverage,
    coalesce(market_snapshot.stale_market_data.stale_reason, 'frais') AS data_state,
    portfolio_snapshot.equity                                        AS equity,
    portfolio_snapshot.total_return_pct                              AS total_return_pct
FROM read_json_auto(?, format='newline_delimited', union_by_name=true,
                    maximum_object_size=33554432)
"""


def load_decision_chart_rows(state_dir: Path) -> list[dict[str, object]]:
    """Load compact decision rows for static chart rendering."""
    journal_path = state_dir / "decisions.jsonl"
    if not journal_path.is_file():
        raise FileNotFoundError(f"journal introuvable : {journal_path}")

    sym2family = symbol_family_map()
    con = duckdb.connect()
    try:
        cur = con.execute(_DECISION_CHART_SQL, [journal_path.as_posix()])
        cols = [desc[0] for desc in cur.description]
        rows: list[dict[str, object]] = []
        for values in cur.fetchall():
            row = dict(zip(cols, values))
            row["family"] = sym2family.get(str(row.get("symbol") or ""), "?")
            rows.append(row)
        return rows
    finally:
        con.close()


def load_decision_dashboard_rows(state_dir: Path) -> list[dict[str, object]]:
    """Load flattened decision rows for the interactive Perspective dashboard."""
    journal_path = state_dir / "decisions.jsonl"
    if not journal_path.is_file():
        raise FileNotFoundError(f"journal introuvable : {journal_path}")

    sym2family = symbol_family_map()
    con = duckdb.connect()
    try:
        cur = con.execute(_DECISION_DASHBOARD_SQL, [journal_path.as_posix()])
        cols = [desc[0] for desc in cur.description]
        rows: list[dict[str, object]] = []
        for values in cur.fetchall():
            row = dict(zip(cols, values))
            row["family"] = sym2family.get(str(row.get("symbol") or ""), "?inconnu?")
            for key, value in list(row.items()):
                if isinstance(value, (dt.date, dt.datetime)):
                    row[key] = value.isoformat()
            rows.append(row)
        return rows
    finally:
        con.close()
