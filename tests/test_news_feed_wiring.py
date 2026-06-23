"""Test de câblage : entry["news"] → build_decision_row (Task 6).

Vérifie que la chaîne entry → build_decision_row préserve le snapshot de news
et que la clé `news` n'est jamais ajoutée au contexte LLM (per_symbol).
"""
from __future__ import annotations

from datetime import datetime, timezone

from trader import decision_ledger
from trader.tools import news_feed as nf

NOW = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)


def test_entry_with_snapshot_flows_into_row():
    nf.reset_cache()
    raw = nf.RawNews(mapped=True, earnings_dates=(), news_count=0)
    snap = nf.news_snapshot("ACA.PA", now=NOW, fetcher=lambda s, *, now: raw)
    entry = {"symbol": "ACA.PA", "action": "HOLD", "news": snap}
    report = {"ts": "2026-06-23T12:00:00+00:00", "prices": {"ACA.PA": 12.3}}
    row = decision_ledger.build_decision_row(report, entry, sequence=0)
    assert row["news"]["news_coverage"] == "empty"
    assert row["news"]["source"] == "yahoo"
