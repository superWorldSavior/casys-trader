"""TDD L6 — thesis tag (attribution structurée).

Plan:
- thesis dans propose_order.args (pas au level décision : un HOLD n'a pas de thèse)
- Validation fail-safe : malformé → None, partiel → borné
- Persisté dans Decision.thesis puis dans la row ledger
"""
from __future__ import annotations

import pytest

from trader.agent.protocol import parsing as codex_client
from trader.agent.protocol.parsing import _normalize_thesis
from trader.agent.protocol.types import Decision
from trader.reporting import decision_ledger


# ---------------------------------------------------------------------------
# _normalize_thesis — validation fail-safe
# ---------------------------------------------------------------------------


def test_normalize_thesis_valide_retourne_dict_borne() -> None:
    raw = {"setup": "breakout structurel", "horizon": "intraday", "invalidation": "cassure sous 100"}
    result = _normalize_thesis(raw)
    assert result == raw


def test_normalize_thesis_horizon_invalid_ignore_le_champ() -> None:
    raw = {"setup": "pullback", "horizon": "UNKNOWN_VALUE", "invalidation": "perd 3%"}
    result = _normalize_thesis(raw)
    assert result is None


def test_normalize_thesis_horizon_case_insensitive() -> None:
    raw = {"setup": "pullback", "horizon": "SWING", "invalidation": "perd 3%"}
    result = _normalize_thesis(raw)
    assert result is not None
    assert result["horizon"] == "swing"


def test_normalize_thesis_non_dict_retourne_none() -> None:
    assert _normalize_thesis("texte libre") is None
    assert _normalize_thesis(42) is None
    assert _normalize_thesis(None) is None
    assert _normalize_thesis([]) is None


def test_normalize_thesis_setup_tronque_a_200_chars() -> None:
    long_str = "x" * 500
    raw = {"setup": long_str, "horizon": "swing", "invalidation": "stop cassé"}
    result = _normalize_thesis(raw)
    assert result is not None
    assert len(result["setup"]) == 200


def test_normalize_thesis_invalidation_tronque_a_200_chars() -> None:
    long_str = "z" * 500
    raw = {"setup": "breakout", "horizon": "position", "invalidation": long_str}
    result = _normalize_thesis(raw)
    assert result is not None
    assert len(result["invalidation"]) == 200


def test_normalize_thesis_champs_manquants_retourne_none() -> None:
    """Un thesis sans setup OU sans invalidation est ignoré (contrat incomplet)."""
    assert _normalize_thesis({"horizon": "swing"}) is None
    assert _normalize_thesis({"setup": "ok", "horizon": "intraday"}) is None
    assert _normalize_thesis({"setup": "ok", "invalidation": "x"}) is None


def test_normalize_thesis_champs_non_str_retourne_none() -> None:
    raw = {"setup": 123, "horizon": "swing", "invalidation": "casse"}
    assert _normalize_thesis(raw) is None


def test_normalize_thesis_setup_vide_retourne_none() -> None:
    raw = {"setup": "  ", "horizon": "intraday", "invalidation": "stop"}
    assert _normalize_thesis(raw) is None


# ---------------------------------------------------------------------------
# Parsing — thesis extrait de propose_order.args
# ---------------------------------------------------------------------------


def _batch_with_thesis(thesis: object | None = None) -> str:
    import json

    args: dict = {"intent": "OPEN_LONG", "qty": 10}
    if thesis is not None:
        args["thesis"] = thesis
    payload = {
        "decisions": [
            {
                "symbol": "AAPL",
                "confidence": 0.8,
                "rationale": "momentum propre",
                "decision_reason_code": "ENTRY_SIGNAL",
                "calls": [{"tool": "propose_order", "args": args}],
            }
        ]
    }
    return json.dumps(payload)


def test_parse_thesis_valide_stocke_dans_decision() -> None:
    thesis = {"setup": "breakout EMA20", "horizon": "swing", "invalidation": "cassure sous EMA50"}
    raw = _batch_with_thesis(thesis)
    parsed = codex_client.parse_batch(raw, ["AAPL"], allow_context_request=False)["AAPL"]
    assert parsed.thesis == thesis


def test_parse_thesis_absent_donne_none() -> None:
    raw = _batch_with_thesis(None)
    parsed = codex_client.parse_batch(raw, ["AAPL"], allow_context_request=False)["AAPL"]
    assert parsed.thesis is None


def test_parse_thesis_malformé_ne_fait_pas_tomber_la_decision() -> None:
    """Un thesis malformé → thesis=None, la décision est valide."""
    raw = _batch_with_thesis("texte libre invalide")
    parsed = codex_client.parse_batch(raw, ["AAPL"], allow_context_request=False)["AAPL"]
    assert parsed.action == "BUY"
    assert parsed.thesis is None


def test_parse_thesis_horizon_invalide_ne_fait_pas_tomber_la_decision() -> None:
    bad_thesis = {"setup": "ok", "horizon": "UNKNOWN", "invalidation": "stop"}
    raw = _batch_with_thesis(bad_thesis)
    parsed = codex_client.parse_batch(raw, ["AAPL"], allow_context_request=False)["AAPL"]
    assert parsed.action == "BUY"
    assert parsed.thesis is None


def test_parse_hold_explicite_sans_propose_order_thesis_none() -> None:
    import json

    payload = json.dumps({
        "decisions": [
            {
                "symbol": "AAPL",
                "confidence": 0.3,
                "rationale": "pas d'edge",
                "decision_reason_code": "NO_EDGE",
                "calls": [],
            }
        ]
    })
    parsed = codex_client.parse_batch(payload, ["AAPL"], allow_context_request=False)["AAPL"]
    assert parsed.action == "HOLD"
    assert parsed.thesis is None


# ---------------------------------------------------------------------------
# Ledger — thesis persisté dans la row
# ---------------------------------------------------------------------------


def _report(decisions: list[dict] | None = None) -> dict:
    return {
        "ts": "2026-07-03T10:00:00+00:00",
        "dry_run": False,
        "symbols_due": ["AAPL"],
        "prices": {"AAPL": 195.0},
        "portfolio": {"cash": 100000.0, "equity": 100000.0, "holdings": []},
        "stale_market_data": {},
        "model_calls_used": 1,
        "code_version": {"git_commit": "abc123", "git_commit_short": "abc123", "git_branch": "main", "git_dirty": False},
        "decisions": decisions or [],
    }


def test_build_decision_row_inclut_thesis_quand_present() -> None:
    thesis = {"setup": "gap-up", "horizon": "intraday", "invalidation": "retour gap"}
    decision = {
        "symbol": "AAPL",
        "action": "BUY",
        "qty": 5.0,
        "confidence": 0.85,
        "rationale": "gap propre",
        "intent": "OPEN_LONG",
        "executed": False,
        "reason": "entry",
        "thesis": thesis,
    }
    report = _report([decision])
    row = decision_ledger.build_decision_row(report, decision, sequence=0, source="daemon")
    assert row["thesis"] == thesis


def test_build_decision_row_thesis_none_quand_absent() -> None:
    decision = {
        "symbol": "AAPL",
        "action": "HOLD",
        "qty": 0.0,
        "confidence": 0.3,
        "rationale": "hold",
        "intent": "HOLD",
        "executed": False,
        "reason": "hold",
    }
    report = _report([decision])
    row = decision_ledger.build_decision_row(report, decision, sequence=0, source="daemon")
    assert row["thesis"] is None
