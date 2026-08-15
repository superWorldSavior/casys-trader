"""FTS5 MATCH must accept raw LLM queries without operator injection."""

from __future__ import annotations

from trader.infrastructure.state_db.fts_query import sanitize_fts5_query


def test_sanitize_vide_ou_none() -> None:
    assert sanitize_fts5_query(None) is None
    assert sanitize_fts5_query("") is None
    assert sanitize_fts5_query("   ") is None


def test_sanitize_mots_simples_sont_quotés() -> None:
    assert sanitize_fts5_query("momentum") == '"momentum"'
    assert sanitize_fts5_query("momentum breakout") == '"momentum" "breakout"'


def test_sanitize_take_profit_ne_devient_pas_filtre_colonne() -> None:
    """`take-profit` est un filtre de colonne inversé FTS5 si on passe la query brute."""
    assert sanitize_fts5_query("long take-profit at highs") == (
        '"long" "take-profit" "at" "highs"'
    )


def test_sanitize_ticker_avec_point() -> None:
    """`INGA.AS` plante FTS5 (`syntax error near \".\"`) sans quoting."""
    assert sanitize_fts5_query("INGA.AS eu_financials") == '"INGA.AS" "eu_financials"'


def test_sanitize_operateurs_et_ponctuation_seuls_sont_ignorés() -> None:
    assert sanitize_fts5_query("---") is None
    assert sanitize_fts5_query("***") is None
    assert sanitize_fts5_query("AND OR NOT") == '"AND" "OR" "NOT"'


def test_sanitize_guillemets_internes_sont_échappés() -> None:
    assert sanitize_fts5_query('foo "bar" baz') == '"foo" "bar" "baz"'
