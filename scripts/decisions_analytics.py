#!/usr/bin/env python3
"""Analyse du journal de décisions via DuckDB — SANS migration.

Le journal `state/decisions.jsonl` reste la source append-only ; DuckDB le
requête directement (`read_json_auto`). Aucun schéma à maintenir, aucun serveur.

Usage :
    uv run python scripts/decisions_analytics.py overview
    uv run python scripts/decisions_analytics.py models
    uv run python scripts/decisions_analytics.py synthetic
    uv run python scripts/decisions_analytics.py activity
    uv run python scripts/decisions_analytics.py quality
    uv run python scripts/decisions_analytics.py sql "SELECT ... FROM d ..."

`d` est la vue exposée sur le journal (une ligne = une décision). Colonnes
imbriquées accessibles en notation struct : `market_snapshot.stale_market_data`,
`portfolio_snapshot.equity`, `code_version.git_commit_short`, etc.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

_DEFAULT_JOURNAL = Path("state/decisions.jsonl")


def _connect(journal: Path) -> duckdb.DuckDBPyConnection:
    """Vue `d` sur le JSONL. `union_by_name` tolère un schéma qui a évolué."""
    if not journal.is_file():
        sys.exit(f"journal introuvable : {journal} (lancer depuis la racine du repo)")
    con = duckdb.connect()
    con.execute(
        f"""
        CREATE VIEW d AS
        SELECT * FROM read_json_auto(
            '{journal.as_posix()}',
            format='newline_delimited',
            union_by_name=true,
            maximum_object_size=33554432
        )
        """
    )
    return con


def _show(con: duckdb.DuckDBPyConnection, title: str, query: str) -> None:
    print(f"\n=== {title} ===")
    con.sql(query).show(max_rows=40)


def overview(con: duckdb.DuckDBPyConnection) -> None:
    _show(con, "Volume & période", """
        SELECT count(*) AS decisions,
               count(DISTINCT symbol) AS symboles,
               min(cycle_ts)::DATE AS depuis,
               max(cycle_ts)::DATE AS jusqu_a,
               round(100.0*avg(CASE WHEN model_called THEN 1 ELSE 0 END),1) AS pct_llm,
               round(100.0*avg(CASE WHEN executed THEN 1 ELSE 0 END),1) AS pct_execute
        FROM d
    """)
    _show(con, "Par action", """
        SELECT action, count(*) AS n,
               round(100.0*count(*)/sum(count(*)) OVER (),1) AS pct,
               round(avg(confidence),2) AS conf_moy
        FROM d GROUP BY action ORDER BY n DESC
    """)


def models(con: duckdb.DuckDBPyConnection) -> None:
    """Comparaison par modèle LLM — répond à « iso vs gpt-5.5 »."""
    _show(con, "Par modèle (décisions où le LLM a été appelé)", """
        SELECT coalesce(llm_model, llm_provider, '(infra/non-LLM)') AS modele,
               count(*) AS n,
               round(avg(confidence),2) AS conf_moy,
               round(100.0*avg(CASE WHEN action ILIKE '%hold%' THEN 1 ELSE 0 END),1) AS pct_hold,
               round(100.0*avg(CASE WHEN executed THEN 1 ELSE 0 END),1) AS pct_execute
        FROM d GROUP BY 1 ORDER BY n DESC
    """)


def synthetic(con: duckdb.DuckDBPyConnection) -> None:
    """HOLD LLM authentiques vs HOLD synthétiques infra (piège Taïwan 06-17)."""
    _show(con, "HOLD : décidés par le LLM vs générés par l'infra", """
        SELECT model_called AS llm_appele,
               decision_reason_code,
               count(*) AS n
        FROM d
        WHERE action ILIKE '%hold%'
        GROUP BY 1,2 ORDER BY n DESC LIMIT 20
    """)
    _show(con, "Décisions par état de fraîcheur des données", """
        SELECT coalesce(market_snapshot.stale_market_data.stale_reason, '(frais)') AS etat,
               count(*) AS n,
               round(100.0*avg(CASE WHEN model_called THEN 1 ELSE 0 END),1) AS pct_llm,
               round(100.0*avg(CASE WHEN action ILIKE '%hold%' THEN 1 ELSE 0 END),1) AS pct_hold
        FROM d GROUP BY 1 ORDER BY n DESC
    """)


def activity(con: duckdb.DuckDBPyConnection) -> None:
    _show(con, "Décisions par jour (14 derniers)", """
        SELECT cycle_ts::DATE AS jour, count(*) AS n,
               sum(CASE WHEN model_called THEN 1 ELSE 0 END) AS llm,
               sum(CASE WHEN executed THEN 1 ELSE 0 END) AS executes
        FROM d GROUP BY 1 ORDER BY 1 DESC LIMIT 14
    """)
    _show(con, "Top symboles décidés", """
        SELECT symbol, count(*) AS n,
               sum(CASE WHEN executed THEN 1 ELSE 0 END) AS executes,
               round(avg(confidence),2) AS conf_moy
        FROM d GROUP BY 1 ORDER BY n DESC LIMIT 15
    """)


def quality(con: duckdb.DuckDBPyConnection) -> None:
    _show(con, "Distribution de confiance", """
        SELECT CASE
                 WHEN confidence < 0.3 THEN '1. <0.3'
                 WHEN confidence < 0.5 THEN '2. 0.3-0.5'
                 WHEN confidence < 0.7 THEN '3. 0.5-0.7'
                 ELSE '4. >=0.7' END AS tranche,
               count(*) AS n,
               round(100.0*avg(CASE WHEN executed THEN 1 ELSE 0 END),1) AS pct_execute
        FROM d WHERE model_called GROUP BY 1 ORDER BY 1
    """)
    _show(con, "Sur quel commit tournait le daemon (code_version)", """
        SELECT code_version.git_commit_short AS commit, count(*) AS n,
               strftime(min(cycle_ts), '%Y-%m-%d %H:%M') AS depuis
        FROM d GROUP BY 1 ORDER BY n DESC LIMIT 10
    """)


_COMMANDS = {
    "overview": overview, "models": models, "synthetic": synthetic,
    "activity": activity, "quality": quality,
}


def main(argv: list[str]) -> None:
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        print("commandes:", ", ".join(_COMMANDS), ", sql, all")
        return
    journal = Path(argv[1]) if len(argv) > 1 and argv[0] == "sql" and False else _DEFAULT_JOURNAL
    con = _connect(journal)
    cmd = argv[0]
    if cmd == "sql":
        if len(argv) < 2:
            sys.exit('usage: sql "SELECT ... FROM d ..."')
        con.sql(argv[1]).show(max_rows=100)
    elif cmd == "all":
        for fn in _COMMANDS.values():
            fn(con)
    elif cmd in _COMMANDS:
        _COMMANDS[cmd](con)
    else:
        sys.exit(f"commande inconnue : {cmd} (voir --help)")


if __name__ == "__main__":
    main(sys.argv[1:])
