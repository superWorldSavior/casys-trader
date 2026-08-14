import json
from datetime import datetime, timezone

import pytest

from backtest.data import HistoryStore
from trader.agent.protocol.prompts import _SYMBOL_CALLS_FINAL_CONTRACT
from trader.reporting.bench import decision_bench
from trader.runtime import cli, daemon
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


def test_parse_model_specs_normalise_grok_en_alias_grok_build() -> None:
    specs = decision_bench.parse_model_specs("grok:grok-4.6,grok-build:grok-4.5")

    assert [(spec.provider, spec.model) for spec in specs] == [
        ("grok-build", "grok-4.6"),
        ("grok-build", "grok-4.5"),
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


def test_score_modele_ne_transforme_pas_un_hold_sans_direction_en_missed() -> None:
    audit = {
        "threshold_pct": 0.5,
        "horizons": ["4h"],
        "rows": [_row("d1", action="BUY", future_return_pct=1.0, verdict="good")],
    }
    cases = decision_bench.select_cases(
        audit,
        horizon="4h",
        limit=1,
        verdicts={"good"},
    )
    response = json.dumps(
        {
            "reviews": [
                {
                    "decision_id": "d1",
                    "action": "HOLD",
                    "opportunity_side": None,
                    "confidence": 0.8,
                    "rationale": "pas de direction déclarée",
                }
            ]
        }
    )

    reviews, summary = decision_bench.score_model_reviews(cases, response, threshold_pct=0.5)

    assert reviews[0]["candidate_verdict"] == "unknown"
    assert summary["unknown"] == 1
    assert summary["scored"] == 0
    assert summary["coverage_pct"] == 0.0


def test_score_modele_mesure_un_hold_directionnel_long_ou_short() -> None:
    audit = {
        "threshold_pct": 0.5,
        "horizons": ["4h"],
        "rows": [
            _row("d1", action="BUY", future_return_pct=1.0, verdict="good"),
            _row("d2", action="BUY", future_return_pct=1.0, verdict="good"),
        ],
    }
    cases = decision_bench.select_cases(
        audit,
        horizon="4h",
        limit=2,
        verdicts={"good"},
    )
    response = json.dumps(
        {
            "reviews": [
                {
                    "decision_id": "d1",
                    "action": "HOLD",
                    "opportunity_side": "long",
                    "confidence": 0.7,
                    "rationale": "long refusé",
                },
                {
                    "decision_id": "d2",
                    "action": "HOLD",
                    "opportunity_side": "short",
                    "confidence": 0.7,
                    "rationale": "short refusé",
                },
            ]
        }
    )

    reviews, summary = decision_bench.score_model_reviews(cases, response, threshold_pct=0.5)

    assert [row["candidate_verdict"] for row in reviews] == ["missed", "good"]
    assert [row["candidate_opportunity_side"] for row in reviews] == ["long", "short"]
    assert summary["missed"] == 1
    assert summary["good"] == 1
    assert summary["coverage_pct"] == 100.0


def test_prompt_bench_demande_la_direction_de_l_opportunite() -> None:
    prompt = decision_bench.build_prompt([], horizon="4h", threshold_pct=0.5)

    assert '"opportunity_side":"long|short|null"' in prompt
    assert "Pour HOLD, `opportunity_side` nomme la thèse précise refusée" in prompt


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


def test_parse_contract_default_reviews() -> None:
    assert decision_bench.parse_contract(None) == "reviews"
    assert decision_bench.parse_contract("reviews") == "reviews"
    assert decision_bench.parse_contract("PRODUCTION") == "production"


def test_parse_contract_refuse_valeur_inconnue() -> None:
    with pytest.raises(ValueError, match="reviews or production"):
        decision_bench.parse_contract("pine")


def test_build_prompt_reviews_par_defaut_reste_le_contrat_avis() -> None:
    prompt = decision_bench.build_prompt([], horizon="4h", threshold_pct=0.5)

    assert '{"reviews":[{"decision_id":"<id>","action":"BUY|SELL|HOLD"' in prompt
    assert _SYMBOL_CALLS_FINAL_CONTRACT not in prompt


def test_build_prompt_production_reutilise_le_contrat_live() -> None:
    prompt = decision_bench.build_prompt([], horizon="4h", threshold_pct=0.5, contract="production")

    assert _SYMBOL_CALLS_FINAL_CONTRACT in prompt
    assert '"decision_id"' in prompt
    assert '"calls"' in prompt
    assert "strategy_entry" in prompt
    assert '{"reviews":[{"decision_id":"<id>","action":"BUY|SELL|HOLD"' not in prompt


def _production_cases() -> list[dict]:
    audit = {
        "threshold_pct": 0.5,
        "horizons": ["4h"],
        "rows": [
            _row("d1", action="HOLD", future_return_pct=1.0, verdict="missed"),
            _row("d2", action="BUY", future_return_pct=-1.0, verdict="bad"),
            _row("d3", action="SELL", future_return_pct=1.0, verdict="good"),
            _row("d4", action="HOLD", future_return_pct=1.0, verdict="missed"),
            _row("d5", action="HOLD", future_return_pct=1.0, verdict="missed"),
            _row("d6", action="HOLD", future_return_pct=1.0, verdict="missed"),
        ],
    }
    return decision_bench.select_cases(
        audit,
        horizon="4h",
        limit=6,
        verdicts={"missed", "bad", "good"},
    )


def test_score_production_mappe_calls_vers_buy_sell_hold() -> None:
    cases = _production_cases()
    response = json.dumps(
        {
            "decisions": [
                {
                    "decision_id": "d1",
                    "symbol": "SPY",
                    "confidence": 0.8,
                    "rationale": "entrée long",
                    "opportunity_side": "long",
                    "decision_reason_code": "ENTRY_SIGNAL",
                    "calls": [{"tool": "strategy_entry", "args": {"direction": "long", "qty": 1}}],
                },
                {
                    "decision_id": "d2",
                    "symbol": "SPY",
                    "confidence": 0.7,
                    "rationale": "entrée short",
                    "opportunity_side": "short",
                    "decision_reason_code": "ENTRY_SIGNAL",
                    "calls": [{"tool": "strategy_entry", "args": {"direction": "short", "qty": 1}}],
                },
                {
                    "decision_id": "d3",
                    "symbol": "SPY",
                    "confidence": 0.6,
                    "rationale": "sortie",
                    "opportunity_side": "long",
                    "decision_reason_code": "EXIT_SIGNAL",
                    "calls": [{"tool": "strategy_close", "args": {"qty_percent": 100}}],
                },
                {
                    "decision_id": "d4",
                    "symbol": "SPY",
                    "confidence": 0.5,
                    "rationale": "veille",
                    "opportunity_side": "long",
                    "decision_reason_code": "WATCH_ARMED",
                    "calls": [{"tool": "set_next_wake", "args": {"minutes": 30}}],
                },
                {
                    "decision_id": "d5",
                    "symbol": "SPY",
                    "confidence": 0.4,
                    "rationale": "wake",
                    "opportunity_side": "long",
                    "decision_reason_code": "NO_EDGE",
                    "calls": [
                        {
                            "tool": "propose_indicator_watch",
                            "args": {"on_trigger": "WAKE", "conditions": []},
                        }
                    ],
                },
                {
                    "decision_id": "d6",
                    "symbol": "SPY",
                    "confidence": 0.9,
                    "rationale": "plan armé short",
                    "opportunity_side": "short",
                    "decision_reason_code": "ARMED_PLAN",
                    "calls": [
                        {
                            "tool": "propose_indicator_watch",
                            "args": {
                                "on_trigger": "EXECUTE_ORDER",
                                "order": {"direction": "short", "qty": 1},
                            },
                        }
                    ],
                },
            ]
        }
    )

    reviews, summary = decision_bench.score_model_reviews(cases, response, threshold_pct=0.5)

    assert [row["candidate_action"] for row in reviews] == [
        "BUY",
        "SELL",
        "SELL",
        "HOLD",
        "HOLD",
        "SELL",
    ]
    assert summary["parsed"] == 6
    assert summary["invalid"] == 0
    assert reviews[0]["candidate_opportunity_side"] == "long"
    assert reviews[5]["rationale"] == "plan armé short"


def test_score_production_calls_vides_sont_hold() -> None:
    cases = _production_cases()[:1]
    response = json.dumps(
        {
            "decisions": [
                {
                    "decision_id": "d1",
                    "symbol": "SPY",
                    "confidence": 0.3,
                    "rationale": "pas d'edge",
                    "opportunity_side": None,
                    "decision_reason_code": "NO_EDGE",
                    "calls": [],
                }
            ]
        }
    )

    reviews, _ = decision_bench.score_model_reviews(cases, response, threshold_pct=0.5)

    assert reviews[0]["candidate_action"] == "HOLD"


def test_score_production_execute_order_long_est_buy() -> None:
    cases = _production_cases()[:1]
    response = json.dumps(
        {
            "decisions": [
                {
                    "decision_id": "d1",
                    "symbol": "SPY",
                    "confidence": 0.85,
                    "rationale": "plan armé long",
                    "opportunity_side": "long",
                    "decision_reason_code": "ARMED_PLAN",
                    "calls": [
                        {
                            "tool": "propose_indicator_watch",
                            "args": {
                                "on_trigger": "EXECUTE_ORDER",
                                "order": {"direction": "long", "qty": 2},
                            },
                        }
                    ],
                }
            ]
        }
    )

    reviews, _ = decision_bench.score_model_reviews(cases, response, threshold_pct=0.5)

    assert reviews[0]["candidate_action"] == "BUY"


def test_dry_run_payload_thread_contract_production() -> None:
    audit = {
        "threshold_pct": 0.5,
        "horizons": ["4h"],
        "rows": [_row("d1", action="HOLD", future_return_pct=1.0, verdict="missed")],
    }

    payload = decision_bench.dry_run_payload(
        audit,
        models=[decision_bench.ModelSpec(provider="acpx", model="m")],
        horizon="4h",
        limit=1,
        verdicts={"missed"},
        contract="production",
        now=datetime(2026, 6, 13, tzinfo=timezone.utc),
    )

    assert payload["contract"] == "production"
    assert _SYMBOL_CALLS_FINAL_CONTRACT in payload["prompt"]
    assert payload["audit_stale"] is False


def test_dry_run_payload_audit_stale_si_cycle_ts_vieux() -> None:
    audit = {
        "threshold_pct": 0.5,
        "horizons": ["4h"],
        "rows": [_row("d1", action="HOLD", future_return_pct=1.0, verdict="missed")],
    }

    payload = decision_bench.dry_run_payload(
        audit,
        models=[decision_bench.ModelSpec(provider="acpx", model="m")],
        horizon="4h",
        limit=1,
        verdicts={"missed"},
        now=datetime(2026, 6, 20, tzinfo=timezone.utc),
    )

    assert payload["audit_stale"] is True
    assert payload["audit_as_of"] == "2026-06-12T01:00:00+00:00"


def test_run_bench_score_une_reponse_production() -> None:
    audit = {
        "threshold_pct": 0.5,
        "horizons": ["4h"],
        "rows": [_row("d1", action="HOLD", future_return_pct=1.0, verdict="missed")],
    }

    def fake_complete(model, prompt, timeout_s):
        assert _SYMBOL_CALLS_FINAL_CONTRACT in prompt
        return decision_bench.ModelCompletion(
            provider=model.provider,
            model=model.model,
            text=json.dumps(
                {
                    "decisions": [
                        {
                            "decision_id": "d1",
                            "symbol": "SPY",
                            "confidence": 0.81,
                            "rationale": "je prends le mouvement",
                            "opportunity_side": "long",
                            "decision_reason_code": "ENTRY_SIGNAL",
                            "calls": [
                                {"tool": "strategy_entry", "args": {"direction": "long", "qty": 1}}
                            ],
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
        contract="production",
        complete=fake_complete,
        now=datetime(2026, 6, 13, tzinfo=timezone.utc),
    )

    assert payload["contract"] == "production"
    assert payload["models"][0]["reviews"][0]["candidate_action"] == "BUY"
    assert payload["models"][0]["reviews"][0]["candidate_verdict"] == "good"


def test_parse_batch_size_default_is_one_call_per_case() -> None:
    assert decision_bench.parse_batch_size(None, n_cases=100) == 1
    assert decision_bench.parse_batch_size(1, n_cases=100) == 1
    assert decision_bench.parse_batch_size(0, n_cases=100) == 100


def test_run_bench_batch_size_one_fait_un_appel_par_cas() -> None:
    audit = {
        "threshold_pct": 0.5,
        "horizons": ["4h"],
        "rows": [
            _row("d1", action="HOLD", future_return_pct=1.0, verdict="missed"),
            _row("d2", action="BUY", future_return_pct=-1.0, verdict="bad"),
        ],
    }
    prompts: list[str] = []

    def fake_complete(model, prompt, timeout_s):
        prompts.append(prompt)
        decision_id = "d1" if "d1" in prompt else "d2"
        action = "HOLD" if decision_id == "d1" else "SELL"
        return decision_bench.ModelCompletion(
            provider=model.provider,
            model=model.model,
            text=json.dumps(
                {
                    "decisions": [
                        {
                            "decision_id": decision_id,
                            "symbol": "SPY",
                            "confidence": 0.5,
                            "rationale": "un cas",
                            "opportunity_side": "long",
                            "calls": [],
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
        limit=2,
        verdicts={"missed", "bad"},
        timeout_s=5,
        contract="production",
        batch_size=1,
        complete=fake_complete,
        now=datetime(2026, 6, 13, tzinfo=timezone.utc),
    )

    assert payload["batch_size"] == 1
    assert payload["n_calls"] == 2
    assert len(prompts) == 2
    assert "d1" in prompts[0] and "d2" not in prompts[0]
    assert "d2" in prompts[1] and "d1" not in prompts[1]
    assert payload["models"][0]["n_calls"] == 2
    assert len(payload["models"][0]["reviews"]) == 2


def test_cli_decisions_bench_contract_default_reviews() -> None:
    args = cli.build_parser().parse_args(["decisions", "bench"])

    assert args.contract == "reviews"
    assert args.batch_size == 1


def _write_bench_audit(state_dir, *, cycle_ts: str = "2026-06-12T01:00:00+00:00") -> None:
    (state_dir / "decision_audit.json").write_text(
        json.dumps(
            {
                "threshold_pct": 0.5,
                "horizons": ["4h"],
                "rows": [
                    {
                        "decision_id": "d1",
                        "cycle_ts": cycle_ts,
                        "symbol": "SPY",
                        "action": "HOLD",
                        "opportunity_side": "long",
                        "intent": "HOLD",
                        "confidence": 0.9,
                        "rationale": "range",
                        "reason": "hold",
                        "price": 100.0,
                        "portfolio_snapshot": {"cash": 100000.0, "equity": 100000.0},
                        "market_snapshot": {"stale_market_data": None},
                        "runtime": {"data_source": "ib"},
                        "audits": {
                            "4h": {
                                "entry_price": 100.0,
                                "future_price": 101.0,
                                "future_return_pct": 1.0,
                                "verdict": "missed",
                            }
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_cli_decisions_bench_dry_run_default_reste_reviews(monkeypatch, tmp_path, capsys) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    _write_bench_audit(state_dir)

    assert (
        cli.main(
            [
                "decisions",
                "bench",
                "--horizon",
                "4h",
                "--limit",
                "1",
                "--models",
                "spark:m",
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["contract"] == "reviews"
    assert '{"reviews":[{"decision_id":"<id>","action":"BUY|SELL|HOLD"' in payload["prompt"]


def test_cli_decisions_bench_humain_avertit_audit_perime(monkeypatch, tmp_path, capsys) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    _write_bench_audit(state_dir, cycle_ts="2026-01-01T00:00:00+00:00")

    assert (
        cli.main(
            [
                "decisions",
                "bench",
                "--horizon",
                "4h",
                "--limit",
                "1",
                "--models",
                "spark:m",
                "--dry-run",
            ]
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "périmé" in out or "stale" in out.lower()
    assert "decisions audit" in out
