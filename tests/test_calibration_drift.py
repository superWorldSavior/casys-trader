"""TDD — read-model calibration / dérive / fenêtre de session."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.calibration_drift import main

UNKNOWN_VERSION = "(unknown)"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _decision(
    *,
    decision_id: str,
    symbol: str = "AIR.PA",
    action: str = "BUY",
    confidence: float | None = 0.72,
    executed: bool = True,
    decision_source: str | None = "llm",
    model_called: bool | None = True,
    cycle_ts: str = "2026-08-10T08:15:00+00:00",
    git_commit_short: str | None = "aaa111aaa111",
    since_open_m: int | None = None,
    to_close_m: int | None = None,
    venue: str | None = None,
    mandate_venue: str | None = None,
    reason: str = "ok",
    llm_error: str | None = None,
    include_code_version: bool = True,
    include_session_keys: bool = False,
) -> dict:
    row: dict = {
        "decision_id": decision_id,
        "cycle_ts": cycle_ts,
        "symbol": symbol,
        "action": action,
        "confidence": confidence,
        "executed": executed,
        "decision_source": decision_source,
        "model_called": model_called,
        "reason": reason,
        "llm_error": llm_error,
        "market_snapshot": {
            "price": 100.0,
            "stale_market_data": None,
            "symbols_due": [symbol],
            "model_calls_used": 1,
        },
    }
    if include_code_version:
        row["code_version"] = {"git_commit_short": git_commit_short}
    if include_session_keys or since_open_m is not None or to_close_m is not None or venue:
        row["market_snapshot"]["since_open_m"] = since_open_m
        row["market_snapshot"]["to_close_m"] = to_close_m
        row["market_snapshot"]["venue"] = venue
    if mandate_venue is not None:
        row["mandate_ref"] = {"venue": mandate_venue, "mandate_id": "m-1"}
    return row


def _fill(
    *,
    ts: str,
    symbol: str,
    action: str,
    price: float,
    quantity: float = 10.0,
    confidence: float | None = 0.72,
    decision_id: str | None = None,
    intent: str = "OPEN_LONG",
    fx_rate: float = 1.0,
    commission_model: str | None = None,
    commission_currency: str | None = None,
) -> dict:
    if commission_model is None:
        commission_model = "ibkr_europe_stock_tiered" if symbol.endswith((".PA", ".DE")) else "ibkr_us_stock_tiered"
    if commission_currency is None:
        commission_currency = "EUR" if symbol.endswith((".PA", ".DE")) else "USD"
    row = {
        "ts": ts,
        "symbol": symbol,
        "action": action,
        "quantity": quantity,
        "price": price,
        "confidence": confidence,
        "intent": intent,
        "fx_rate": fx_rate,
        "commission": 0.0,
        "commission_model": commission_model,
        "commission_currency": commission_currency,
    }
    if decision_id is not None:
        row["decision_id"] = decision_id
    return row


def _run(tmp_path: Path, command: str, *extra: str, capsys) -> dict:
    argv = [command, "--state-dir", str(tmp_path), "--json", *extra]
    assert main(argv) == 0
    return json.loads(capsys.readouterr().out)


def test_fichiers_absents_ne_cassent_pas(tmp_path: Path, capsys) -> None:
    payload = _run(tmp_path, "calibration", capsys=capsys)
    assert payload["command"] == "calibration"
    assert payload["inputs"]["decisions_found"] is False
    assert payload["inputs"]["fills_found"] is False
    assert payload["overall"]["n_executed"] == 0
    assert payload["overall"]["n_closed_trips"] == 0
    assert payload["overall"]["pnl_quality"] == {
        "status": "unavailable",
        "n_total": 0,
        "n_available": 0,
        "n_unavailable": 0,
        "coverage_ratio": None,
    }
    assert not list(tmp_path.iterdir())


def test_hold_infra_exclu_des_stats_de_calibration(tmp_path: Path, capsys) -> None:
    _write_jsonl(
        tmp_path / "decisions.jsonl",
        [
            _decision(
                decision_id="infra-hold",
                action="HOLD",
                confidence=0.95,
                executed=False,
                decision_source="infra",
                model_called=False,
                reason="no_decision_in_batch",
            ),
            _decision(
                decision_id="legacy-machine",
                action="HOLD",
                confidence=0.91,
                executed=False,
                decision_source=None,
                model_called=False,
                reason="stale_market_data",
            ),
            _decision(
                decision_id="agent-buy",
                action="BUY",
                confidence=0.72,
                executed=True,
            ),
        ],
    )
    payload = _run(tmp_path, "calibration", capsys=capsys)
    assert payload["exclusions"]["n_infra"] == 2
    assert payload["overall"]["n_decisions"] == 1
    assert payload["overall"]["n_executed"] == 1
    bucket = next(item for item in payload["buckets"] if item["bucket"] == "0.7-0.8")
    assert bucket["n_executed"] == 1
    high = next(item for item in payload["buckets"] if item["bucket"] == "0.8-1.0")
    assert high["n_executed"] == 0


def test_decision_sans_fill_compte_executee_sans_trip(tmp_path: Path, capsys) -> None:
    _write_jsonl(
        tmp_path / "decisions.jsonl",
        [_decision(decision_id="open-only", executed=True, confidence=0.74)],
    )
    payload = _run(tmp_path, "calibration", capsys=capsys)
    bucket = next(item for item in payload["buckets"] if item["bucket"] == "0.7-0.8")
    assert bucket["n_executed"] == 1
    assert bucket["n_closed_trips"] == 0
    assert bucket["win_rate"] is None
    assert bucket["calibration_gap"] is None


def test_fill_sans_decision_reste_non_joint(tmp_path: Path, capsys) -> None:
    _write_jsonl(
        tmp_path / "decisions.jsonl",
        [_decision(decision_id="other", symbol="MSFT", executed=False, action="HOLD")],
    )
    _write_jsonl(
        tmp_path / "model_performance.jsonl",
        [
            _fill(ts="2026-08-10T10:00:00+00:00", symbol="QQQ", action="BUY", price=100.0),
            _fill(
                ts="2026-08-10T11:00:00+00:00",
                symbol="QQQ",
                action="SELL",
                price=110.0,
                intent="CLOSE",
            ),
        ],
    )
    payload = _run(tmp_path, "calibration", capsys=capsys)
    assert payload["overall"]["n_closed_trips"] == 0
    assert payload["unmatched_trips"]["n_closed_trips"] == 1
    assert payload["unmatched_trips"]["win_rate"] == 1.0


def test_confiance_none_hors_tranches_numeriques(tmp_path: Path, capsys) -> None:
    _write_jsonl(
        tmp_path / "decisions.jsonl",
        [_decision(decision_id="no-conf", confidence=None, executed=True)],
    )
    payload = _run(tmp_path, "calibration", capsys=capsys)
    assert payload["exclusions"]["n_confidence_missing"] == 1
    numeric = [item for item in payload["buckets"] if item["bucket"] != "confidence_missing"]
    assert all(item["n_executed"] == 0 for item in numeric)
    missing = next(item for item in payload["buckets"] if item["bucket"] == "confidence_missing")
    assert missing["n_executed"] == 1


def test_courbe_de_fiabilite_et_gap_de_calibration(tmp_path: Path, capsys) -> None:
    _write_jsonl(
        tmp_path / "decisions.jsonl",
        [
            _decision(decision_id="win-high", symbol="AIR.PA", confidence=0.75, action="BUY"),
            _decision(decision_id="lose-mid", symbol="MC.PA", confidence=0.55, action="BUY"),
            _decision(
                decision_id="win-sell",
                symbol="VNA.DE",
                confidence=0.65,
                action="SELL",
                cycle_ts="2026-08-10T09:00:00+00:00",
            ),
        ],
    )
    _write_jsonl(
        tmp_path / "model_performance.jsonl",
        [
            _fill(
                ts="2026-08-10T08:15:00+00:00",
                symbol="AIR.PA",
                action="BUY",
                price=100.0,
                decision_id="win-high",
            ),
            _fill(
                ts="2026-08-10T12:15:00+00:00",
                symbol="AIR.PA",
                action="SELL",
                price=110.0,
                intent="CLOSE",
            ),
            _fill(
                ts="2026-08-10T08:15:00+00:00",
                symbol="MC.PA",
                action="BUY",
                price=200.0,
                confidence=0.55,
                decision_id="lose-mid",
            ),
            _fill(
                ts="2026-08-10T12:15:00+00:00",
                symbol="MC.PA",
                action="SELL",
                price=190.0,
                intent="CLOSE",
            ),
            _fill(
                ts="2026-08-10T09:00:00+00:00",
                symbol="VNA.DE",
                action="SELL",
                price=50.0,
                confidence=0.65,
                decision_id="win-sell",
                intent="OPEN_SHORT",
            ),
            _fill(
                ts="2026-08-10T13:00:00+00:00",
                symbol="VNA.DE",
                action="BUY",
                price=45.0,
                intent="CLOSE",
            ),
        ],
    )
    payload = _run(tmp_path, "calibration", capsys=capsys)
    by_bucket = {item["bucket"]: item for item in payload["buckets"]}
    high = by_bucket["0.7-0.8"]
    assert high["n_executed"] == 1
    assert high["n_closed_trips"] == 1
    assert high["win_rate"] == 1.0
    assert high["mean_announced_confidence"] == 0.75
    assert high["calibration_gap"] == -0.25
    assert high["mean_pnl_bps"] == 1000.0

    mid = by_bucket["0.5-0.6"]
    assert mid["win_rate"] == 0.0
    assert mid["calibration_gap"] == 0.55
    assert mid["mean_pnl_bps"] == -500.0

    sell = by_bucket["0.6-0.7"]
    assert sell["win_rate"] == 1.0
    assert sell["mean_pnl_bps"] == 1000.0

    assert payload["overall"]["n_closed_trips"] == 3
    assert payload["overall"]["win_rate"] == 2 / 3
    assert payload["by_action"]["BUY"]["n_closed_trips"] == 2
    assert payload["by_action"]["SELL"]["n_closed_trips"] == 1

    only_sell = _run(tmp_path, "calibration", "--action", "SELL", capsys=capsys)
    assert only_sell["overall"]["n_executed"] == 1
    assert only_sell["overall"]["n_closed_trips"] == 1
    assert only_sell["filters"]["action"] == "SELL"


def test_filtre_venue_derivee_du_suffixe_et_du_mandat(tmp_path: Path, capsys) -> None:
    _write_jsonl(
        tmp_path / "decisions.jsonl",
        [
            _decision(decision_id="eu-1", symbol="AIR.PA", confidence=0.72),
            _decision(
                decision_id="us-1",
                symbol="MSFT",
                confidence=0.72,
                mandate_venue="US",
                cycle_ts="2026-08-10T14:00:00+00:00",
            ),
        ],
    )
    eu = _run(tmp_path, "calibration", "--venue", "EU", capsys=capsys)
    assert eu["overall"]["n_executed"] == 1
    assert eu["filters"]["venue"] == "EU"
    us = _run(tmp_path, "calibration", "--venue", "US", capsys=capsys)
    assert us["overall"]["n_executed"] == 1


def test_since_filtre_les_decisions_et_les_trips(tmp_path: Path, capsys) -> None:
    _write_jsonl(
        tmp_path / "decisions.jsonl",
        [
            _decision(
                decision_id="old",
                symbol="AIR.PA",
                cycle_ts="2026-07-01T08:00:00+00:00",
                confidence=0.72,
            ),
            _decision(
                decision_id="new",
                symbol="MC.PA",
                cycle_ts="2026-08-10T08:00:00+00:00",
                confidence=0.72,
            ),
        ],
    )
    payload = _run(tmp_path, "calibration", "--since", "2026-08-01", capsys=capsys)
    assert payload["overall"]["n_executed"] == 1
    assert payload["filters"]["since"] == "2026-08-01"


def test_drift_hebdo_marque_le_changement_de_code_version(tmp_path: Path, capsys) -> None:
    _write_jsonl(
        tmp_path / "decisions.jsonl",
        [
            _decision(
                decision_id="w1-hold",
                action="HOLD",
                executed=False,
                confidence=0.80,
                cycle_ts="2026-08-03T10:00:00+00:00",
                git_commit_short="oldoldoldold",
            ),
            _decision(
                decision_id="w1-buy",
                action="BUY",
                executed=True,
                confidence=0.70,
                cycle_ts="2026-08-04T10:00:00+00:00",
                git_commit_short="oldoldoldold",
            ),
            _decision(
                decision_id="w2-sell",
                action="SELL",
                executed=True,
                confidence=0.60,
                cycle_ts="2026-08-10T10:00:00+00:00",
                git_commit_short="newnewnewnew",
            ),
            _decision(
                decision_id="w2-hold",
                action="HOLD",
                executed=False,
                confidence=0.50,
                cycle_ts="2026-08-11T10:00:00+00:00",
                git_commit_short="newnewnewnew",
            ),
            _decision(
                decision_id="w2-infra",
                action="HOLD",
                executed=False,
                confidence=0.99,
                decision_source="infra",
                model_called=False,
                reason="quiet_gate",
                cycle_ts="2026-08-11T11:00:00+00:00",
                git_commit_short="newnewnewnew",
            ),
            _decision(
                decision_id="w2-old-schema",
                action="HOLD",
                executed=False,
                confidence=0.40,
                cycle_ts="2026-08-12T10:00:00+00:00",
                include_code_version=False,
            ),
        ],
    )
    _write_jsonl(
        tmp_path / "model_performance.jsonl",
        [
            _fill(
                ts="2026-08-04T10:00:00+00:00",
                symbol="AIR.PA",
                action="BUY",
                price=100.0,
                decision_id="w1-buy",
            ),
            _fill(
                ts="2026-08-04T16:00:00+00:00",
                symbol="AIR.PA",
                action="SELL",
                price=102.0,
                intent="CLOSE",
            ),
        ],
    )
    payload = _run(tmp_path, "drift", capsys=capsys)
    weeks = {item["iso_week"]: item for item in payload["weeks"]}
    assert "2026-W32" in weeks
    assert "2026-W33" in weeks
    w32 = weeks["2026-W32"]
    w33 = weeks["2026-W33"]
    assert w32["n_llm"] == 2
    assert w32["dominant_code_version"] == "oldoldoldold"
    assert w32["dominant_changed"] is False
    assert w32["action_mix"]["BUY"] == 50.0
    assert w32["action_mix"]["HOLD"] == 50.0
    assert w32["n_closed_trips"] == 1
    assert w32["win_rate"] == 1.0
    assert w33["dominant_code_version"] == "newnewnewnew"
    assert w33["dominant_changed"] is True
    assert w33["previous_dominant"] == "oldoldoldold"
    versions = {item["code_version"] for item in w33["by_code_version"]}
    assert UNKNOWN_VERSION in versions
    assert "newnewnewnew" in versions
    assert w33["n_llm"] == 3
    assert w33["n_infra_excluded"] == 1


def test_pnl_indisponible_est_exclu_de_calibration_et_drift(
    tmp_path: Path,
    capsys,
) -> None:
    _write_jsonl(
        tmp_path / "decisions.jsonl",
        [
            _decision(
                decision_id="known-win",
                symbol="AIR.PA",
                confidence=0.75,
                cycle_ts="2026-08-10T08:00:00+00:00",
            ),
            _decision(
                decision_id="unknown-pnl",
                symbol="MC.PA",
                confidence=0.95,
                cycle_ts="2026-08-10T09:00:00+00:00",
            ),
        ],
    )
    _write_jsonl(
        tmp_path / "model_performance.jsonl",
        [
            _fill(
                ts="2026-08-10T08:00:00+00:00",
                symbol="AIR.PA",
                action="BUY",
                price=100.0,
                decision_id="known-win",
            ),
            _fill(
                ts="2026-08-10T10:00:00+00:00",
                symbol="AIR.PA",
                action="SELL",
                price=110.0,
                intent="CLOSE",
            ),
            _fill(
                ts="2026-08-10T09:00:00+00:00",
                symbol="MC.PA",
                action="BUY",
                price=100.0,
                decision_id="unknown-pnl",
                commission_model="ibkr_unknown",
            ),
            _fill(
                ts="2026-08-10T11:00:00+00:00",
                symbol="MC.PA",
                action="SELL",
                price=120.0,
                intent="CLOSE",
                commission_model="ibkr_unknown",
            ),
        ],
    )

    calibration = _run(tmp_path, "calibration", capsys=capsys)

    assert calibration["overall"]["n_closed_trips"] == 2
    assert calibration["overall"]["win_rate"] == 1.0
    assert calibration["overall"]["mean_announced_confidence"] == 0.75
    assert calibration["overall"]["pnl_quality"] == {
        "status": "partial",
        "n_total": 2,
        "n_available": 1,
        "n_unavailable": 1,
        "coverage_ratio": 0.5,
    }

    drift = _run(tmp_path, "drift", capsys=capsys)
    week = next(item for item in drift["weeks"] if item["iso_week"] == "2026-W33")
    assert week["n_closed_trips"] == 2
    assert week["win_rate"] == 1.0
    assert week["pnl_quality"] == calibration["overall"]["pnl_quality"]
    version = next(item for item in week["by_code_version"] if item["code_version"] == "aaa111aaa111")
    assert version["win_rate"] == 1.0
    assert version["pnl_quality"] == calibration["overall"]["pnl_quality"]
    assert drift["pnl_quality"] == calibration["overall"]["pnl_quality"]


def test_sorties_partielles_forment_un_seul_cycle_gagnant(
    tmp_path: Path,
    capsys,
) -> None:
    _write_jsonl(
        tmp_path / "decisions.jsonl",
        [
            _decision(
                decision_id="partial-win",
                symbol="AIR.PA",
                confidence=0.70,
                cycle_ts="2026-08-10T08:00:00+00:00",
            )
        ],
    )
    _write_jsonl(
        tmp_path / "model_performance.jsonl",
        [
            _fill(
                ts="2026-08-10T08:00:00+00:00",
                symbol="AIR.PA",
                action="BUY",
                price=100.0,
                quantity=2.0,
                decision_id="partial-win",
            ),
            _fill(
                ts="2026-08-10T09:00:00+00:00",
                symbol="AIR.PA",
                action="SELL",
                price=90.0,
                quantity=1.0,
                intent="CLOSE",
            ),
            _fill(
                ts="2026-08-10T10:00:00+00:00",
                symbol="AIR.PA",
                action="SELL",
                price=120.0,
                quantity=1.0,
                intent="CLOSE",
            ),
        ],
    )

    calibration = _run(tmp_path, "calibration", capsys=capsys)

    assert calibration["overall"]["n_closed_trips"] == 1
    assert calibration["overall"]["win_rate"] == 1.0
    assert calibration["overall"]["pnl_quality"] == {
        "status": "available",
        "n_total": 1,
        "n_available": 1,
        "n_unavailable": 0,
        "coverage_ratio": 1.0,
    }

    drift = _run(tmp_path, "drift", capsys=capsys)
    week = next(item for item in drift["weeks"] if item["iso_week"] == "2026-W33")
    assert week["n_closed_trips"] == 1
    assert week["win_rate"] == 1.0
    assert week["pnl_quality"]["n_available"] == 1


def test_session_window_buckets_et_effectif_champ_manquant(tmp_path: Path, capsys) -> None:
    _write_jsonl(
        tmp_path / "decisions.jsonl",
        [
            _decision(
                decision_id="open-eu",
                symbol="AIR.PA",
                since_open_m=12,
                to_close_m=500,
                venue="EU",
                include_session_keys=True,
            ),
            _decision(
                decision_id="mid",
                symbol="MC.PA",
                since_open_m=45,
                to_close_m=400,
                venue="EU",
                include_session_keys=True,
                cycle_ts="2026-08-10T09:00:00+00:00",
            ),
            _decision(
                decision_id="late",
                symbol="SAP.DE",
                since_open_m=120,
                to_close_m=200,
                venue="EU",
                include_session_keys=True,
                cycle_ts="2026-08-10T11:00:00+00:00",
            ),
            _decision(
                decision_id="very-late",
                symbol="SIE.DE",
                since_open_m=300,
                to_close_m=20,
                venue="EU",
                include_session_keys=True,
                cycle_ts="2026-08-10T14:00:00+00:00",
            ),
            _decision(
                decision_id="no-field",
                symbol="MSFT",
                cycle_ts="2026-08-10T15:00:00+00:00",
                include_session_keys=False,
            ),
        ],
    )
    _write_jsonl(
        tmp_path / "model_performance.jsonl",
        [
            _fill(
                ts="2026-08-10T08:15:00+00:00",
                symbol="AIR.PA",
                action="BUY",
                price=100.0,
                decision_id="open-eu",
            ),
            _fill(
                ts="2026-08-10T10:15:00+00:00",
                symbol="AIR.PA",
                action="SELL",
                price=99.0,
                intent="CLOSE",
            ),
            _fill(
                ts="2026-08-10T09:00:00+00:00",
                symbol="MC.PA",
                action="BUY",
                price=100.0,
                decision_id="mid",
            ),
            _fill(
                ts="2026-08-10T11:00:00+00:00",
                symbol="MC.PA",
                action="SELL",
                price=101.0,
                intent="CLOSE",
            ),
            _fill(
                ts="2026-08-10T11:00:00+00:00",
                symbol="SAP.DE",
                action="BUY",
                price=100.0,
                decision_id="late",
            ),
            _fill(
                ts="2026-08-10T13:00:00+00:00",
                symbol="SAP.DE",
                action="SELL",
                price=103.0,
                intent="CLOSE",
            ),
            _fill(
                ts="2026-08-10T14:00:00+00:00",
                symbol="SIE.DE",
                action="BUY",
                price=100.0,
                decision_id="very-late",
            ),
            _fill(
                ts="2026-08-10T15:00:00+00:00",
                symbol="SIE.DE",
                action="SELL",
                price=104.0,
                intent="CLOSE",
            ),
        ],
    )
    payload = _run(tmp_path, "session-window", capsys=capsys)
    by_bucket = {item["bucket"]: item for item in payload["by_since_open"]}
    assert by_bucket["0-30"]["n_closed_trips"] == 1
    assert by_bucket["0-30"]["win_rate"] == 0.0
    assert by_bucket["30-90"]["n_closed_trips"] == 1
    assert by_bucket["90-240"]["n_closed_trips"] == 1
    assert by_bucket["240+"]["n_closed_trips"] == 1
    assert payload["near_close"]["bucket"] == "to_close_m<60"
    assert payload["near_close"]["n_closed_trips"] == 1
    assert payload["near_close"]["win_rate"] == 1.0
    assert payload["coverage"]["n_with_key"] == 4
    assert payload["coverage"]["n_with_value"] == 4
    assert payload["missing"]["n_decisions"] == 1
    assert payload["field_too_recent"] is False
    eu = _run(tmp_path, "session-window", "--venue", "EU", capsys=capsys)
    assert eu["missing"]["n_decisions"] == 0
    assert eu["overall"]["n_closed_trips"] == 4


def test_session_window_dit_honnetement_quand_le_champ_est_trop_recent(tmp_path: Path, capsys) -> None:
    _write_jsonl(
        tmp_path / "decisions.jsonl",
        [
            _decision(decision_id="old-schema", include_session_keys=False),
            _decision(
                decision_id="schema-null",
                include_session_keys=True,
                since_open_m=None,
                to_close_m=None,
                cycle_ts="2026-08-16T03:00:00+00:00",
            ),
        ],
    )
    payload = _run(tmp_path, "session-window", capsys=capsys)
    assert payload["coverage"]["n_with_key"] == 1
    assert payload["coverage"]["n_with_value"] == 0
    assert payload["field_too_recent"] is True
    assert payload["overall"]["n_closed_trips"] == 0
    assert any("since_open_m" in note for note in payload["notes"])


def test_sortie_humaine_et_aucun_etat_ecrit(tmp_path: Path, capsys) -> None:
    _write_jsonl(
        tmp_path / "decisions.jsonl",
        [
            _decision(
                decision_id="w1",
                cycle_ts="2026-08-03T10:00:00+00:00",
                git_commit_short="oldoldoldold",
                executed=False,
                action="HOLD",
            ),
            _decision(
                decision_id="w2",
                cycle_ts="2026-08-10T10:00:00+00:00",
                git_commit_short="newnewnewnew",
                executed=False,
                action="HOLD",
            ),
        ],
    )
    assert main(["drift", "--state-dir", str(tmp_path)]) == 0
    text = capsys.readouterr().out
    assert "2026-W32" in text
    assert "changement" in text.lower()
    written = [path.name for path in tmp_path.iterdir()]
    assert written == ["decisions.jsonl"]
