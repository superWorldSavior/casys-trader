"""Escape free-text so it is safe to pass to an FTS5 MATCH clause."""

from __future__ import annotations

import re

_WORD_CHAR = re.compile(r"\w", re.UNICODE)


def sanitize_fts5_query(text: str | None) -> str | None:
    """Quote each whitespace token so FTS5 cannot treat it as an operator.

    LLM queries are free text. Unquoted ``take-profit`` is an inverted column
    filter (``no such column: profit``); ``INGA.AS`` is a syntax error. Quoted
    tokens stay literals and keep implicit AND / BM25 ranking.
    """
    if not text or not str(text).strip():
        return None

    tokens: list[str] = []
    for raw in str(text).split():
        token = raw.strip().strip('"').strip()
        if not token or not _WORD_CHAR.search(token):
            continue
        tokens.append('"' + token.replace('"', '""') + '"')
    if not tokens:
        return None
    return " ".join(tokens)
