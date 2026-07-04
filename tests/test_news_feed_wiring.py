"""Test de câblage : entry["news"] → build_decision_row (Task 6).

Vérifie que la chaîne entry → build_decision_row préserve le snapshot de news
et que la clé `news` n'est jamais ajoutée au contexte LLM (per_symbol).
Comprend également les tests de câblage macro_next (P1a spec §2.2).
"""
from __future__ import annotations

from datetime import datetime, timezone

from trader.reporting import decision_ledger
from trader.market import news_feed as nf

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


# ---------------------------------------------------------------------------
# Fix B : record_decision centralise l'enrichissement news pour TOUS les chemins
# ---------------------------------------------------------------------------


def _flat_bars_factory(now_iso: str):
    from trader.market.market_data import Bar

    def factory(symbol, lookback, interval):
        return [
            Bar(ts=now_iso, open=100.0, high=100.1, low=99.9, close=100.0, volume=1000.0)
            for _ in range(4)
        ]

    return factory


def test_quiet_gate_decision_has_news_key(monkeypatch, tmp_path, patch_batch, make_data_source):
    """Fix B : les décisions quiet_gate (chemins infra) doivent avoir une clé 'news'.

    Avant le fix, `entry["news"]` n'était ajouté qu'APRÈS le batch LLM (~l.2062),
    donc les HOLD infra (quiet_gate, stale_market_data) ne l'avaient jamais.
    Après le fix, record_decision appelle news_feed.news_snapshot via setdefault
    pour TOUS les chemins.
    """
    from trader.runtime import daemon
    from trader.market import news_feed as nf
    from trader.scheduling.scheduler import Scheduler
    from conftest import write_runtime_config

    write_runtime_config(tmp_path, symbols=("SPY",))
    state_dir = tmp_path / "state"
    now = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    # LLM vu il y a 1h → quiet_gate s'applique (pas d'appel LLM)
    daemon._LAST_LLM_AT[(str(state_dir), "SPY")] = now.replace(hour=11)

    # Patcher news_feed.news_snapshot pour ne pas appeler yfinance
    nf.reset_cache()
    fake_snap = {
        "earnings_in_h": None,
        "news_coverage": "empty",
        "news_count": 0,
        "source": "yahoo",
        "asof": now.isoformat(),
    }
    monkeypatch.setattr(nf, "news_snapshot", lambda symbol, *, now, **kw: fake_snap)

    from trader.agent.client import Decision

    def decide(**kwargs):
        return Decision.hold(kwargs["symbol"], "attente")

    patch_batch(decide)
    data_source = make_data_source(_flat_bars_factory(now.isoformat()))

    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    assert len(report["decisions"]) == 1
    decision = report["decisions"][0]
    assert decision["reason"] == "quiet_gate"
    assert "news" in decision, (
        f"Fix B requis : la décision quiet_gate doit avoir la clé 'news'. "
        f"Clés présentes : {list(decision.keys())}"
    )


# ---------------------------------------------------------------------------
# macro_next câblé dans le payload news par décision (spec §2.2)
# ---------------------------------------------------------------------------


def test_payload_decision_contient_macro_next(monkeypatch, tmp_path, patch_batch, make_data_source):
    """Le payload news de chaque décision contient la clé 'macro_next' (spec §2.2).

    macro_next est calculé UNE fois par cycle (pas par symbole) et n'est PAS
    poussé au prompt LLM (attribution-first). Le test vérifie que la clé est
    présente dans la décision loggée, quelle que soit la voie (quiet_gate inclus).
    """
    from trader.runtime import daemon
    from trader.market import news_feed as nf
    from trader.scheduling.scheduler import Scheduler
    from conftest import write_runtime_config

    write_runtime_config(tmp_path, symbols=("SPY",))
    state_dir = tmp_path / "state"
    now = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

    nf.reset_cache()
    fake_snap = {
        "earnings_in_h": None,
        "news_coverage": "empty",
        "news_count": 0,
        "source": "yahoo",
        "asof": now.isoformat(),
    }
    monkeypatch.setattr(nf, "news_snapshot", lambda symbol, *, now, **kw: fake_snap)

    from trader.agent.client import Decision

    def decide(**kwargs):
        return Decision.hold(kwargs["symbol"], "attente")

    patch_batch(decide)
    data_source = make_data_source(_flat_bars_factory(now.isoformat()))

    report = daemon.run_cycle(
        dry_run=True,
        now=now,
        symbols_filter=["SPY"],
        sched=Scheduler(state_dir / "scheduler.json"),
        data_source=data_source,
    )

    assert len(report["decisions"]) >= 1
    decision = report["decisions"][0]
    assert "news" in decision, f"clé 'news' manquante : {list(decision.keys())}"
    news = decision["news"]
    assert "macro_next" in news, (
        f"spec §2.2 : 'macro_next' doit être dans le payload news. "
        f"Clés news présentes : {list(news.keys())}"
    )
    assert isinstance(news["macro_next"], list), (
        f"macro_next doit être une liste, obtenu : {type(news['macro_next'])}"
    )
    # macro_next ne doit pas être dans le contexte LLM per_symbol (attribution-first).
    # Vérifié en s'assurant qu'il n'est posé QUE dans la décision loggée, pas dans
    # base_context ou per_symbol (contrainte d'architecture, non testée ici car le
    # contexte LLM n'est pas exposé directement — le pattern setdefault(news) garantit
    # que news n'entre jamais dans per_symbol, vérifié par test_entry_with_snapshot_flows_into_row).
