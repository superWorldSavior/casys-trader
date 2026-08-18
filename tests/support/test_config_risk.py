from __future__ import annotations

from pathlib import Path

from trader.runtime.consolidation_inputs import build_consolidation_inputs
from trader.support.config.risk import (
    attribution_min_entry_confidence,
    read_attribution_min_entry_confidence,
    read_min_trade_confidence,
)


def test_attribution_min_entry_confidence_none_quand_gate_off() -> None:
    assert (
        attribution_min_entry_confidence(
            {"min_trade_confidence": 0.7},
            confidence_gate_enabled=False,
        )
        is None
    )


def test_attribution_min_entry_confidence_seuil_quand_gate_on() -> None:
    assert (
        attribution_min_entry_confidence(
            {"min_trade_confidence": 0.65},
            confidence_gate_enabled=True,
        )
        == 0.65
    )
    assert (
        attribution_min_entry_confidence({}, confidence_gate_enabled=True) == 0.7
    )


def test_read_attribution_min_entry_confidence_suit_le_yaml(tmp_path: Path) -> None:
    risk = tmp_path / "risk.yaml"
    risk.write_text(
        "min_trade_confidence: 0.7\nconfidence_gate_enabled: false\n",
        encoding="utf-8",
    )
    assert read_attribution_min_entry_confidence(risk) is None
    assert read_min_trade_confidence(risk) == 0.7

    risk.write_text(
        "min_trade_confidence: 0.65\nconfidence_gate_enabled: true\n",
        encoding="utf-8",
    )
    assert read_attribution_min_entry_confidence(risk) == 0.65


def test_read_attribution_min_entry_confidence_live_safe_si_yaml_absent(
    tmp_path: Path,
) -> None:
    assert read_attribution_min_entry_confidence(tmp_path / "missing.yaml") == 0.7


def _write_low_conf_round_trip(state_dir: Path) -> None:
    import json

    state_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "ts": "2026-08-17T18:20:00+00:00",
            "symbol": "NOC",
            "action": "BUY",
            "quantity": 1,
            "price": 100.0,
            "confidence": 0.58,
            "intent": "OPEN_LONG",
        },
        {
            "ts": "2026-08-17T18:21:00+00:00",
            "symbol": "NOC",
            "action": "SELL",
            "quantity": 1,
            "price": 90.0,
            "confidence": None,
            "intent": "PLANNED_EXIT",
            "exit_reason": "llm_exit",
        },
    ]
    (state_dir / "model_performance.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_consolidation_inputs_ne_censure_pas_quand_gate_off(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_low_conf_round_trip(state_dir)
    risk = tmp_path / "risk.yaml"
    risk.write_text(
        "min_trade_confidence: 0.7\nconfidence_gate_enabled: false\n",
        encoding="utf-8",
    )

    attr, _meta = build_consolidation_inputs(
        state_dir=state_dir,
        risk_yaml_path=risk,
    )

    assert attr["n_closed_trades"] == 1
    assert attr["recent_trips"][0]["symbol"] == "NOC"
    assert attr["regime"]["min_entry_confidence"] is None


def test_consolidation_inputs_censure_quand_gate_on(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _write_low_conf_round_trip(state_dir)
    risk = tmp_path / "risk.yaml"
    risk.write_text(
        "min_trade_confidence: 0.7\nconfidence_gate_enabled: true\n",
        encoding="utf-8",
    )

    attr, _meta = build_consolidation_inputs(
        state_dir=state_dir,
        risk_yaml_path=risk,
    )

    assert attr["n_closed_trades"] == 0
    assert attr["regime"]["n_excluded_low_confidence"] == 1
