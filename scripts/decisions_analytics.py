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
    uv run python scripts/decisions_analytics.py memory [state_dir]
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

_DEFAULT_STATE_DIR = Path("state")


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _show_rows(title: str, rows: list[dict], *, empty: str | None = None) -> None:
    """Même habillage `=== titre ===` + tableau DuckDB que les autres commandes."""
    print(f"\n=== {title} ===")
    if empty:
        print(empty)
        return
    if not rows:
        print("(vide)")
        return
    columns = list(rows[0].keys())
    quoted = ", ".join(f'"{name}"' for name in columns)
    values = ", ".join(
        "(" + ", ".join(_sql_literal(row.get(name)) for name in columns) + ")"
        for row in rows
    )
    duckdb.sql(f"SELECT * FROM (VALUES {values}) AS t({quoted})").show(
        max_rows=40, max_width=120
    )


def _fmt_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} Mo"
    if n >= 1024:
        return f"{n / 1024:.1f} ko"
    return f"{n} o"


def _fmt_pct(value: float | None) -> object:
    return None if value is None else round(value, 1)


def _fmt_rate(value: float | None) -> object:
    return None if value is None else round(value, 3)


def memory(state_dir: Path = _DEFAULT_STATE_DIR) -> None:
    """Répond à « est-ce que la mémoire aide ? » — read-model, zéro écriture."""
    from trader.reporting.read_models.memory import compute_memory_report

    report = compute_memory_report(Path(state_dir))
    store = report.store
    if not store.available:
        _show_rows("Store", [], empty=store.missing_reason)
    else:
        _show_rows(
            "Store",
            [
                {
                    "notes": store.n_notes,
                    "recalls": store.n_recalls,
                    "regles_actives": store.n_active_rules,
                    "pct_embedding": _fmt_pct(store.pct_with_embedding),
                    "taille": _fmt_bytes(store.file_size_bytes),
                    "depuis": store.period_from,
                    "jusqu_a": store.period_to,
                }
            ],
        )
        _show_rows(
            "Store — par source",
            [{"source": name, "n": n} for name, n in store.by_source],
        )
        _show_rows(
            "Store — par verdict",
            [{"verdict": name, "n": n} for name, n in store.by_verdict],
        )

    coverage = report.coverage
    if not coverage.available:
        _show_rows("Recall coverage", [], empty=coverage.missing_reason)
    else:
        _show_rows(
            "Recall coverage (LLM authentiques, hors HOLD infra)",
            [
                {
                    "decisions_llm": coverage.n_authentic_llm,
                    "avec_recall": coverage.n_with_recall,
                    "pct": _fmt_pct(coverage.pct),
                }
            ],
        )

    utility = report.utility
    if not utility.available:
        _show_rows("Recall utility", [], empty=utility.missing_reason)
    else:
        _show_rows(
            "Recall utility (reward évalué) — LIFT = utile − base WIN/(WIN+LOSS)",
            [
                {
                    "evalues": utility.n_evaluated,
                    "reward_plus": utility.n_plus,
                    "reward_zero": utility.n_zero,
                    "reward_moins": utility.n_minus,
                    "taux_utile": _fmt_rate(utility.useful_rate),
                    "base_rate": _fmt_rate(utility.base_rate),
                    "lift": _fmt_rate(utility.lift),
                }
            ],
        )

    def _note_rows(rows) -> list[dict]:
        return [
            {
                "symbol": row.symbol,
                "family": row.family,
                "outcome_score": _fmt_rate(row.outcome_score),
                "q_value": _fmt_rate(row.q_value),
                "q_updates": row.q_updates,
                "note": row.excerpt,
            }
            for row in rows
        ]

    _show_rows("Notes top outcome_score", _note_rows(report.ranks.top_outcome))
    _show_rows("Notes flop outcome_score", _note_rows(report.ranks.flop_outcome))
    _show_rows("Notes top q_value", _note_rows(report.ranks.top_q))
    _show_rows("Notes flop q_value", _note_rows(report.ranks.flop_q))

    _show_rows(
        "Règles globales actives",
        [
            {
                "rule_id": row.rule_id,
                "q_value": _fmt_rate(row.q_value),
                "q_updates": row.q_updates,
                "utilite": row.utility,
            }
            for row in report.rules
        ],
    )

    situation = report.situation
    if not situation.available:
        _show_rows("Situation", [], empty=situation.missing_reason)
    else:
        _show_rows(
            "Situation",
            [
                {
                    "notes": situation.n_notes,
                    "evaluees": situation.n_evaluated,
                }
            ],
        )
        _show_rows(
            "Situation — par verdict",
            [{"verdict": name, "n": n} for name, n in situation.by_verdict],
        )

    health = report.health
    if not health.available:
        _show_rows("Santé sync", [], empty=health.missing_reason)
    else:
        age_min = None if health.age_seconds is None else health.age_seconds / 60.0
        _show_rows(
            "Santé sync",
            [
                {
                    "status": health.status,
                    "as_of": health.as_of,
                    "age_min": None if age_min is None else round(age_min, 1),
                }
            ],
        )


def main(argv: list[str]) -> None:
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        print("commandes:", ", ".join([*_COMMANDS, "memory"]), ", sql, all")
        return
    cmd = argv[0]
    if cmd == "memory":
        state_dir = Path(argv[1]) if len(argv) > 1 else _DEFAULT_STATE_DIR
        memory(state_dir)
        return
    journal = Path(argv[1]) if len(argv) > 1 and argv[0] == "sql" and False else _DEFAULT_JOURNAL
    con = _connect(journal)
    if cmd == "sql":
        if len(argv) < 2:
            sys.exit('usage: sql "SELECT ... FROM d ..."')
        con.sql(argv[1]).show(max_rows=100)
    elif cmd == "all":
        for fn in _COMMANDS.values():
            fn(con)
        memory(_DEFAULT_STATE_DIR)
    elif cmd in _COMMANDS:
        _COMMANDS[cmd](con)
    else:
        sys.exit(f"commande inconnue : {cmd} (voir --help)")


if __name__ == "__main__":
    main(sys.argv[1:])
