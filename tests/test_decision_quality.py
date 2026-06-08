"""Tests du scoring de qualité des décisions sans LLM ni P&L."""

from datetime import timedelta

import pytest

from trader.tools.market import Bar

import backtest.decision_quality as decision_quality
from backtest.data import HistoryStore
from backtest.decision_quality import aggregate, classify, forward_return, score_decisions, score_from_ledger


def _bar(ts: str, close: float) -> Bar:
    return Bar(ts=ts, open=close, high=close, low=close, close=close, volume=100.0)


@pytest.mark.parametrize(
    ("action", "rendement", "verdict"),
    [
        ("BUY", 0.006, "gagnant"),
        ("BUY", -0.006, "perdant"),
        ("BUY", 0.004, "neutre"),
        ("BUY", None, "non_evaluable"),
        ("SELL", -0.006, "gagnant"),
        ("SELL", 0.006, "perdant"),
        ("SELL", -0.004, "neutre"),
        ("SELL", None, "non_evaluable"),
        ("HOLD", 0.006, "opportunite_manquee"),
        ("HOLD", -0.006, "bonne_prudence"),
        ("HOLD", 0.004, "justifie"),
        ("HOLD", None, "non_evaluable"),
    ],
)
def test_classify_qualifie_les_decisions_selon_la_bande(action: str, rendement: float | None, verdict: str) -> None:
    assert classify(action, rendement, band=0.005) == verdict


def test_forward_return_utilise_le_prix_asof_et_le_prix_posterieur() -> None:
    store = HistoryStore.from_bars(
        {
            "AAPL": [
                _bar("2026-01-01T10:00:00", 100.0),
                _bar("2026-01-01T14:00:00", 104.0),
            ]
        }
    )

    assert forward_return(store, "AAPL", "2026-01-01T10:30:00", timedelta(hours=4)) == pytest.approx(0.04)


def test_forward_return_non_evaluable_si_aucune_barre_nouvelle_dans_horizon() -> None:
    store = HistoryStore.from_bars(
        {
            "AAPL": [
                _bar("2026-01-02T16:00:00", 100.0),
                _bar("2026-01-05T10:00:00", 110.0),
            ]
        }
    )

    assert store.price_after("AAPL", "2026-01-02T16:00:00", timedelta(days=1)) is None
    assert forward_return(store, "AAPL", "2026-01-02T16:00:00", timedelta(days=1)) is None


def test_score_decisions_tagge_le_provider_ou_unknown() -> None:
    decisions = [
        {
            "decision_id": "2026-01-01T10:00:00|0|SPARK",
            "cycle_ts": "2026-01-01T10:00:00",
            "symbol": "SPARK",
            "action": "BUY",
            "reason": "ok",
            "llm_provider": "spark",
            "llm_fallback_reason": None,
        },
        {"cycle_ts": "2026-01-01T10:00:00", "symbol": "UNKNOWN", "action": "HOLD", "reason": "hold"},
    ]
    store = HistoryStore.from_bars(
        {
            "SPARK": [
                _bar("2026-01-01T10:00:00", 100.0),
                _bar("2026-01-01T14:00:00", 102.0),
            ],
            "UNKNOWN": [
                _bar("2026-01-01T10:00:00", 100.0),
                _bar("2026-01-01T14:00:00", 99.0),
            ],
        }
    )

    rows = score_decisions(decisions, store, band=0.005)

    spark_row = next(row for row in rows if row["symbol"] == "SPARK" and row["horizon"] == "4h")
    unknown_row = next(row for row in rows if row["symbol"] == "UNKNOWN" and row["horizon"] == "4h")
    assert spark_row["decision_id"] == "2026-01-01T10:00:00|0|SPARK"
    assert unknown_row["decision_id"] is None
    assert spark_row["provider"] == "spark"
    assert spark_row["fallback_reason"] is None
    assert unknown_row["provider"] == "unknown"
    assert unknown_row["fallback_reason"] is None


def test_score_from_ledger_renvoie_les_rows_scorables_et_la_fenetre(monkeypatch, tmp_path) -> None:
    ledger = tmp_path / "decisions.jsonl"
    ledger.write_text(
        "\n".join(
            [
                (
                    '{"decision_id":"d1","cycle_ts":"2026-01-01T10:00:00",'
                    '"symbol":"SPY","action":"BUY","reason":"ok"}'
                ),
                (
                    '{"decision_id":"d2","cycle_ts":"2026-01-02T10:00:00",'
                    '"symbol":"QQQ","action":"HOLD","reason":"stale_market_data"}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    def fake_load_available_history(symbols, start, end, interval):
        assert symbols == ["SPY"]
        assert start == "2025-12-31"
        assert end == "2026-01-03"
        assert interval == "1h"
        return HistoryStore.from_bars(
            {
                "SPY": [
                    _bar("2026-01-01T10:00:00", 100.0),
                    _bar("2026-01-01T14:00:00", 102.0),
                ]
            }
        ), []

    monkeypatch.setattr(decision_quality, "_load_available_history", fake_load_available_history)

    result = score_from_ledger(ledger, band=0.005, days_buffer=1)

    assert [row["decision_id"] for row in result["scored_rows"]] == ["d1", "d1"]
    assert [row["decision_id"] for row in result["judgeable"]] == ["d1"]
    assert result["exclusions"] == {
        "stale_market_data": 1,
        "gates": 0,
        "by_reason": {"stale_market_data": 1},
    }
    assert result["window"] == ("2025-12-31", "2026-01-03")


def test_score_decisions_et_aggregate_comptent_buckets_taux_et_non_evaluables() -> None:
    decisions = [
        {"cycle_ts": "2026-01-01T10:00:00", "symbol": "UP", "action": "HOLD", "reason": "hold"},
        {"cycle_ts": "2026-01-01T10:00:00", "symbol": "FLAT", "action": "HOLD", "reason": "hold"},
        {"cycle_ts": "2026-01-01T10:00:00", "symbol": "BUYWIN", "action": "BUY", "reason": "ok"},
        {"cycle_ts": "2026-01-01T10:00:00", "symbol": "MISSING", "action": "SELL", "reason": "ok"},
    ]
    store = HistoryStore.from_bars(
        {
            "UP": [_bar("2026-01-01T10:00:00", 100.0), _bar("2026-01-01T14:00:00", 101.0)],
            "FLAT": [_bar("2026-01-01T10:00:00", 100.0), _bar("2026-01-01T14:00:00", 100.2)],
            "BUYWIN": [_bar("2026-01-01T10:00:00", 100.0), _bar("2026-01-01T14:00:00", 101.0)],
        }
    )

    rows = score_decisions(decisions, store, band=0.005)
    stats = {(row["action"], row["horizon"]): row for row in aggregate(rows)["aggregate"]}

    hold_4h = stats[("HOLD", "4h")]
    assert hold_4h["n_total"] == 2
    assert hold_4h["n_evaluable"] == 2
    assert hold_4h["n_non_evaluable"] == 0
    assert hold_4h["buckets"] == {"opportunite_manquee": 1, "justifie": 1}
    assert hold_4h["frileux_rate"] == pytest.approx(0.5)
    assert hold_4h["hit_rate"] is None

    buy_4h = stats[("BUY", "4h")]
    assert buy_4h["buckets"] == {"gagnant": 1}
    assert buy_4h["hit_rate"] == pytest.approx(1.0)
    assert buy_4h["frileux_rate"] is None

    sell_4h = stats[("SELL", "4h")]
    assert sell_4h["n_evaluable"] == 0
    assert sell_4h["n_non_evaluable"] == 1
    assert sell_4h["mean_forward_return"] is None
    assert sell_4h["buckets"] == {"non_evaluable": 1}
    assert sell_4h["hit_rate"] is None


def test_aggregate_croise_les_metriques_par_provider() -> None:
    decisions = [
        {
            "cycle_ts": "2026-01-01T10:00:00",
            "symbol": "SPARK_BUY",
            "action": "BUY",
            "reason": "ok",
            "llm_provider": "spark",
        },
        {
            "cycle_ts": "2026-01-01T10:00:00",
            "symbol": "SPARK_HOLD",
            "action": "HOLD",
            "reason": "hold",
            "llm_provider": "spark",
        },
        {
            "cycle_ts": "2026-01-01T10:00:00",
            "symbol": "OLLAMA_SELL",
            "action": "SELL",
            "reason": "ok",
            "llm_provider": "ollama-cloud",
        },
    ]
    store = HistoryStore.from_bars(
        {
            "SPARK_BUY": [
                _bar("2026-01-01T10:00:00", 100.0),
                _bar("2026-01-01T14:00:00", 102.0),
                _bar("2026-01-02T10:00:00", 104.0),
            ],
            "SPARK_HOLD": [
                _bar("2026-01-01T10:00:00", 100.0),
                _bar("2026-01-01T14:00:00", 99.0),
                _bar("2026-01-02T10:00:00", 98.0),
            ],
            "OLLAMA_SELL": [
                _bar("2026-01-01T10:00:00", 100.0),
                _bar("2026-01-01T14:00:00", 95.0),
                _bar("2026-01-02T10:00:00", 90.0),
            ],
        }
    )

    rows = score_decisions(decisions, store, band=0.005)
    by_provider = {item["provider"]: item for item in aggregate(rows)["by_provider"]}

    assert by_provider["spark"]["n_total"] == 4
    assert by_provider["spark"]["n_evaluable"] == 4
    assert by_provider["spark"]["n_non_evaluable"] == 0
    assert by_provider["spark"]["mean_forward_return"] == pytest.approx((0.02 + 0.04 - 0.01 - 0.02) / 4)
    assert by_provider["spark"]["actions"] == {"BUY": 2, "HOLD": 2, "SELL": 0}

    assert by_provider["ollama-cloud"]["n_total"] == 2
    assert by_provider["ollama-cloud"]["n_evaluable"] == 2
    assert by_provider["ollama-cloud"]["n_non_evaluable"] == 0
    assert by_provider["ollama-cloud"]["mean_forward_return"] == pytest.approx((-0.05 - 0.10) / 2)
    assert by_provider["ollama-cloud"]["actions"] == {"BUY": 0, "HOLD": 0, "SELL": 2}
