from __future__ import annotations

import json

import trader.reporting.read_models.runtime_state as runtime_state_module
from trader.reporting.read_models.runtime_state import load_runtime_state


def _write_report(state_dir, payload: dict) -> None:
    (state_dir / "current_report.json").write_text(json.dumps(payload), encoding="utf-8")


def _stub_empty_live(monkeypatch) -> None:
    monkeypatch.setattr(runtime_state_module, "_compute_live_kpis_safe", lambda path: {})
    monkeypatch.setattr(runtime_state_module, "_compute_attribution_safe", lambda path: {})


def test_sources_label_report_fallback_without_changing_values(tmp_path, monkeypatch) -> None:
    _stub_empty_live(monkeypatch)
    _write_report(
        tmp_path,
        {
            "kpis": {"equity": 100_000.0},
            "attribution": {"n_closed_trades": 2},
        },
    )

    state = load_runtime_state(state_dir=tmp_path, config_dir=str(tmp_path))

    assert state["kpis"] == {"equity": 100_000.0}
    assert state["kpis_source"] == "report"
    assert state["attribution"] == {"n_closed_trades": 2}
    assert state["attribution_source"] == "report"


def test_sources_label_live_when_the_read_model_is_fresh(tmp_path, monkeypatch) -> None:
    _write_report(
        tmp_path,
        {
            "kpis": {"equity": 1.0},
            "attribution": {"n_closed_trades": 1},
        },
    )
    monkeypatch.setattr(
        runtime_state_module, "_compute_live_kpis_safe", lambda path: {"equity": 9.0}
    )
    monkeypatch.setattr(
        runtime_state_module,
        "_compute_attribution_safe",
        lambda path: {"n_closed_trades": 5},
    )

    state = load_runtime_state(state_dir=tmp_path, config_dir=str(tmp_path))

    assert state["kpis"] == {"equity": 9.0}
    assert state["kpis_source"] == "live"
    assert state["attribution"] == {"n_closed_trades": 5}
    assert state["attribution_source"] == "live"


def test_sources_label_empty_when_nothing_is_available(tmp_path, monkeypatch) -> None:
    _stub_empty_live(monkeypatch)
    state = load_runtime_state(state_dir=tmp_path, config_dir=str(tmp_path))

    assert state["kpis"] == {}
    assert state["kpis_source"] == "empty"
    assert state["attribution"] == {}
    assert state["attribution_source"] == "empty"


def test_non_dict_report_sections_stay_fail_open(tmp_path, monkeypatch) -> None:
    _stub_empty_live(monkeypatch)
    _write_report(tmp_path, {"kpis": ["not-a-dict"], "attribution": None})

    state = load_runtime_state(state_dir=tmp_path, config_dir=str(tmp_path))

    assert state["kpis"] == {}
    assert state["kpis_source"] == "empty"
    assert state["attribution"] == {}
    assert state["attribution_source"] == "empty"
