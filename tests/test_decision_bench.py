import json

from backtest.data import HistoryStore
from trader.reporting import decision_bench
from trader.market.market_data import Bar


def _bar(ts: str, close: float) -> Bar:
    return Bar(ts=ts, open=close, high=close + 1.0, low=close - 1.0, close=close, volume=1000.0)


def _row(decision_id: str, *, action: str, future_return_pct: float, verdict: str) -> dict:
    return {
        "decision_id": decision_id,
        "cycle_ts": f"2026-06-12T0{decision_id[-1]}:00:00+00:00",
        "symbol": "SPY",
        "action": action,
        "intent": "HOLD" if action == "HOLD" else "OPEN_LONG",
        "confidence": 0.9,
        "rationale": "decision historique",
        "reason": "hold" if action == "HOLD" else "ok",
        "price": 100.0,
        "portfolio_snapshot": {"cash": 100000.0, "equity": 100000.0, "positions": {}},
        "market_snapshot": {"stale_market_data": None, "symbols_due": ["SPY"]},
        "runtime": {"data_source": "ib", "context_request": None},
        "audits": {
            "4h": {
                "entry_price": 100.0,
                "future_price": None if future_return_pct is None else 100.0 * (1 + future_return_pct / 100.0),
                "future_return_pct": future_return_pct,
                "verdict": verdict,
            }
        },
    }


def test_select_cases_prend_des_decisions_evaluees_du_dernier_audit() -> None:
    audit = {
        "threshold_pct": 0.5,
        "horizons": ["4h"],
        "rows": [
            _row("d1", action="HOLD", future_return_pct=1.0, verdict="missed"),
            _row("d2", action="BUY", future_return_pct=-1.0, verdict="bad"),
            _row("d3", action="HOLD", future_return_pct=None, verdict="unknown"),
        ],
    }

    cases = decision_bench.select_cases(audit, horizon="4h", limit=2, verdicts={"missed", "bad"})

    assert [case["decision_id"] for case in cases] == ["d1", "d2"]
    assert "audits" not in cases[0]["case"]
    assert cases[0]["audit"]["verdict"] == "missed"


def test_select_cases_offset_sapplique_apres_filtrage() -> None:
    audit = {
        "threshold_pct": 0.5,
        "horizons": ["4h"],
        "rows": [
            _row("d1", action="HOLD", future_return_pct=1.0, verdict="missed"),
            _row("d2", action="BUY", future_return_pct=-1.0, verdict="bad"),
            _row("d3", action="HOLD", future_return_pct=-1.0, verdict="good"),
        ],
    }

    cases = decision_bench.select_cases(
        audit,
        horizon="4h",
        limit=1,
        offset=1,
        verdicts={"missed", "bad"},
    )

    assert [case["decision_id"] for case in cases] == ["d2"]


def test_parse_model_specs_par_defaut_couvre_le_banc_demande() -> None:
    specs = decision_bench.parse_model_specs(None)

    assert [(spec.provider, spec.model) for spec in specs] == [
        ("acpx", "gpt-5.3-codex-spark"),
        ("acpx", "gpt-5.5"),
        ("ollama-cloud", "nemotron-3-super:cloud"),
        ("ollama-cloud", "glm-5.1:cloud"),
    ]


def test_parse_model_specs_normalise_spark_en_alias_acpx() -> None:
    specs = decision_bench.parse_model_specs("spark:gpt-5.5/medium,acpx:gpt-5.5/xhigh")

    assert [(spec.provider, spec.model) for spec in specs] == [
        ("acpx", "gpt-5.5/medium"),
        ("acpx", "gpt-5.5/xhigh"),
    ]


def test_run_bench_score_une_reponse_modele_sans_exposer_le_futur() -> None:
    audit = {
        "threshold_pct": 0.5,
        "horizons": ["4h"],
        "rows": [_row("d1", action="HOLD", future_return_pct=1.0, verdict="missed")],
    }
    prompts = []

    def fake_complete(model, prompt, timeout_s):
        prompts.append(prompt)
        return decision_bench.ModelCompletion(
            provider=model.provider,
            model=model.model,
            text=json.dumps(
                {
                    "reviews": [
                        {
                            "decision_id": "d1",
                            "action": "BUY",
                            "confidence": 0.81,
                            "rationale": "je prends le mouvement",
                        }
                    ]
                }
            ),
            latency_s=0.01,
        )

    payload = decision_bench.run_bench(
        audit,
        models=[decision_bench.ModelSpec(provider="acpx", model="m")],
        horizon="4h",
        limit=1,
        verdicts={"missed"},
        timeout_s=5,
        complete=fake_complete,
    )

    assert "future_return_pct" not in prompts[0]
    model = payload["models"][0]
    assert model["summary"]["good"] == 1
    assert model["summary"]["bad"] == 0
    assert model["summary"]["same_as_original"] == 0
    assert model["reviews"][0]["candidate_verdict"] == "good"
    assert model["reviews"][0]["original_verdict"] == "missed"


def test_reconstruct_case_contexts_utilise_uniquement_les_bars_asof() -> None:
    audit = {
        "threshold_pct": 0.5,
        "horizons": ["4h"],
        "rows": [_row("d1", action="HOLD", future_return_pct=1.0, verdict="missed")],
    }
    cases = decision_bench.select_cases(
        audit,
        horizon="4h",
        limit=1,
        verdicts={"missed"},
        include_original=False,
    )
    store = HistoryStore.from_bars(
        {
            "SPY": [
                _bar("2026-06-12T00:00:00+00:00", 100.0),
                _bar("2026-06-12T00:15:00+00:00", 101.0),
                _bar("2026-06-12T00:30:00+00:00", 102.0),
                _bar("2026-06-12T01:00:00+00:00", 103.0),
                _bar("2026-06-12T01:15:00+00:00", 999.0),
            ],
            "QQQ": [
                _bar("2026-06-12T00:00:00+00:00", 200.0),
                _bar("2026-06-12T00:15:00+00:00", 201.0),
                _bar("2026-06-12T00:30:00+00:00", 202.0),
                _bar("2026-06-12T01:00:00+00:00", 203.0),
            ],
            "DIA": [
                _bar("2026-06-12T00:00:00+00:00", 300.0),
                _bar("2026-06-12T00:15:00+00:00", 301.0),
                _bar("2026-06-12T00:30:00+00:00", 302.0),
                _bar("2026-06-12T01:00:00+00:00", 303.0),
            ],
        }
    )

    enriched = decision_bench.reconstruct_case_contexts(
        cases,
        history=store,
        symbols=["SPY", "QQQ", "DIA"],
        interval="15m",
        lookback_bars=4,
        cockpit_window=3,
    )

    context = enriched[0]["case"]["reconstructed_context"]
    assert context["exact_replay"] is False
    assert context["latest_bar_ts"]["SPY"] == "2026-06-12T01:00:00+00:00"
    assert context["prices"]["SPY"] == 103.0
    assert "indices" in context["regime_families"]
    assert any(row[0] == "SPY" for row in context["cockpit"]["rows"])
    prompt = decision_bench.build_prompt(enriched, horizon="4h", threshold_pct=0.5)
    assert "reconstructed_context" in prompt
    assert "999.0" not in prompt
