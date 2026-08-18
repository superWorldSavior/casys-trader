"""Tests de la projection galerie de rapports LLM (projections/reports.py).

Couvre :
- collecte par kind (global / macro / regional / micro) depuis des mini-artifacts
- ordre des sections + tri as_of décroissant (None en dernier)
- choix du brief micro le plus récent (+ tie → deep)
- label avec company_names, history_count global
- tolérance totale (répertoires absents, JSON corrompu ignoré, jamais d'exception)
- build_gallery_row (pastille colorée par kind, label, âge relatif)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from rich.text import Text

from trader.interfaces.cockpit.projections.reports import (
    KIND_DOT_STYLES,
    ReportItem,
    build_gallery_row,
    collect_report_items,
)
from trader.interfaces.ui.palette import CASYS_ACCENT

NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(renderable, width: int = 120) -> str:
    from rich.console import Console

    console = Console(width=width, legacy_windows=False)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def _styles(text: Text) -> list[str]:
    return [str(span.style) for span in text.spans]


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{json.dumps(row)}\n" for row in rows), encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _global_payload(as_of: str = "2026-07-17T06:00:00+00:00") -> dict:
    return {
        "as_of": as_of,
        "venue_posture": {"EU": "constructive", "TW": "neutral", "US": "cautious"},
        "family_priority": {"favored": ["semis"], "deprioritized": ["banks"]},
        "gross_mode": "normal",
        "net_bias": "long",
        "rationale": "breadth improving",
        "valid_until": "2026-07-18T06:00:00+00:00",
        "source_refs": ["brief:EU"],
        "posture_id": "posture-1",
    }


def _macro_payload(venue: str, as_of: str | None) -> dict:
    payload = {
        "brief_id": f"brief-{venue}",
        "venue": venue,
        "valid_until": "2026-07-18T00:00:00+00:00",
        "input_refs": ["news:1"],
        "zones": {"europe": [{"point": "pmi up", "sources": ["x"], "source_refs": ["r"], "symbols": [], "severity": "info", "signal": "weak", "horizon": "1w", "direction": "up"}]},
        "families": {},
        "symbols": {},
        "alerts": [],
    }
    if as_of is not None:
        payload["as_of"] = as_of
    return payload


def _regional_payload(as_of: str) -> dict:
    return {
        "summary": "rotation into semis",
        "family_postures": {"semis": "favored"},
        "symbol_rationales": {"3443.TW": "leader"},
        "selected_hotlist": ["3443.TW"],
        "as_of": as_of,
    }


def _micro_brief(symbol: str, as_of: str, depth: str) -> dict:
    return {
        "brief_id": f"{symbol}-{depth}",
        "symbol": symbol,
        "as_of": as_of,
        "input_signature": "sig",
        "depth": depth,
        "business": {"summary": "makes chips", "points": ["p1"]},
        "company_thesis": {
            "status": "intact",
            "summary": "leader",
            "pillars": ["moat"],
            "confirming_evidence": [],
            "disconfirming_evidence": [],
            "kill_criteria": [],
            "next_proof_points": [],
        },
        "catalysts": [],
        "risks": [],
        "open_questions": [],
        "selection_view": {"posture": "candidate", "confidence": 0.6, "reasons": [], "scope": "scope-1"},
        "security_readiness": "ready",
        "coverage": "full",
        "source_refs": ["filing:1"],
    }


def _micro_envelope(symbol: str, briefs: dict[str, dict]) -> dict:
    return {"schema_version": 1, "symbol": symbol, "briefs": briefs}


# ---------------------------------------------------------------------------
# Collecte — cas vides / tolérance
# ---------------------------------------------------------------------------


def test_collect_empty_state_dir(tmp_path: Path) -> None:
    """Répertoires absents → liste vide, aucune exception."""
    assert collect_report_items(tmp_path) == []


def test_collect_ignores_corrupted_artifacts_everywhere(tmp_path: Path) -> None:
    """JSON corrompu dans chaque source → tout est ignoré, jamais d'exception."""
    _write_text(tmp_path / "global_universe_postures" / "current.json", "{not json")
    _write_text(tmp_path / "news_briefs" / "latest-EU.jsonl", "{broken\n")
    _write_text(tmp_path / "universe_runs" / "latest-TW.json", "[1, 2]")
    _write_text(tmp_path / "company_intelligence" / "current" / "abc.json", "null")
    assert collect_report_items(tmp_path) == []


def test_collect_ignores_non_dict_json(tmp_path: Path) -> None:
    """Un JSON valide mais non-objet (liste, scalaire) est ignoré."""
    _write_json(tmp_path / "global_universe_postures" / "current.json", [1, 2, 3])
    _write_json(tmp_path / "universe_runs" / "latest-EU.json", "text")
    assert collect_report_items(tmp_path) == []


def test_collect_regional_keeps_success_payload_when_latest_attempt_failed(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "universe_runs" / "latest-EU.json",
        {
            **_regional_payload("2026-07-17T05:30:00+00:00"),
            "status": "success",
            "latest_failure": {
                "status": "error",
                "error_code": "TimeoutError",
                "retry_attempt": 1,
            },
        },
    )

    items = collect_report_items(tmp_path)

    assert len(items) == 1
    assert items[0].kind == "regional"
    assert items[0].payload["summary"] == "rotation into semis"
    assert items[0].payload["latest_failure"]["error_code"] == "TimeoutError"


def test_collect_regional_exposes_latest_waiting_overlay(tmp_path: Path) -> None:
    waiting = {
        "status": "waiting_brief",
        "error_code": "brief_scope_mismatch",
        "candidate_scope_id": "scope-today",
        "as_of": "2026-07-17T12:07:00+00:00",
    }
    _write_json(
        tmp_path / "universe_runs" / "latest-US.json",
        {
            **_regional_payload("2026-07-17T05:30:00+00:00"),
            "status": "success",
            "candidate_scope_id": "scope-yesterday",
            "latest_waiting": waiting,
        },
    )

    items = collect_report_items(tmp_path)

    assert len(items) == 1
    assert items[0].payload["summary"] == "rotation into semis"
    assert items[0].payload["latest_waiting"]["error_code"] == "brief_scope_mismatch"
    assert items[0].payload["latest_waiting"]["candidate_scope_id"] == "scope-today"


# ---------------------------------------------------------------------------
# Collecte — global
# ---------------------------------------------------------------------------


def test_collect_global_item(tmp_path: Path) -> None:
    _write_json(tmp_path / "global_universe_postures" / "current.json", _global_payload())
    items = collect_report_items(tmp_path)
    assert len(items) == 1
    item = items[0]
    assert item.kind == "global"
    assert item.key == "global:current"
    assert item.as_of == "2026-07-17T06:00:00+00:00"
    assert item.depth is None
    assert item.payload["venue_posture"]["EU"] == "constructive"
    assert item.payload["posture_id"] == "posture-1"


def test_collect_global_history_count(tmp_path: Path) -> None:
    """history_count = lignes non vides du ledger YYYY-MM-DD.jsonl du jour de as_of."""
    _write_json(tmp_path / "global_universe_postures" / "current.json", _global_payload())
    ledger = tmp_path / "global_universe_postures" / "2026-07-17.jsonl"
    _write_jsonl(ledger, [{"a": 1}, {"a": 2}, {"a": 3}])
    _write_text(ledger, ledger.read_text(encoding="utf-8") + "\n")  # ligne vide ignorée
    items = collect_report_items(tmp_path)
    assert items[0].payload["history_count"] == 3


def test_collect_global_no_history_count_without_ledger(tmp_path: Path) -> None:
    """Pas de ledger du jour → pas de clé history_count."""
    _write_json(tmp_path / "global_universe_postures" / "current.json", _global_payload())
    items = collect_report_items(tmp_path)
    assert "history_count" not in items[0].payload


def test_collect_global_keeps_success_and_attaches_latest_failure(tmp_path: Path) -> None:
    _write_json(tmp_path / "global_universe_postures" / "current.json", _global_payload())
    _write_json(
        tmp_path / "global_universe_postures" / "latest_failure.json",
        {
            "status": "error",
            "as_of": "2026-07-17T07:00:00+00:00",
            "error_code": "TimeoutError",
            "retry_attempt": 2,
            "next_retry_at": "2026-07-17T08:00:00+00:00",
        },
    )

    item = collect_report_items(tmp_path)[0]

    assert item.payload["rationale"] == "breadth improving"
    assert item.payload["latest_failure"]["error_code"] == "TimeoutError"


def test_collect_global_bootstrap_failure_is_visible_without_success(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "global_universe_postures" / "latest_failure.json",
        {
            "status": "error",
            "as_of": "2026-07-17T07:00:00+00:00",
            "error_code": "ProviderUnavailable",
        },
    )

    items = collect_report_items(tmp_path)

    assert [item.key for item in items] == ["global:current"]
    assert items[0].payload["error_code"] == "ProviderUnavailable"


# ---------------------------------------------------------------------------
# Collecte — macro / regional
# ---------------------------------------------------------------------------


def test_collect_macro_sorted_by_as_of_desc_none_last(tmp_path: Path) -> None:
    _write_jsonl(tmp_path / "news_briefs" / "latest-EU.jsonl", [_macro_payload("EU", "2026-07-16T08:00:00+00:00")])
    _write_jsonl(tmp_path / "news_briefs" / "latest-US.jsonl", [_macro_payload("US", "2026-07-17T05:00:00+00:00")])
    _write_jsonl(tmp_path / "news_briefs" / "latest-GLOBAL.jsonl", [_macro_payload("GLOBAL", None)])
    items = collect_report_items(tmp_path)
    assert [item.key for item in items] == ["macro:US", "macro:EU", "macro:GLOBAL"]
    assert items[1].label == "EU"
    assert items[1].payload["zones"]["europe"][0]["severity"] == "info"
    assert items[2].as_of is None


def test_collect_macro_skips_corrupted_file_keeps_valid(tmp_path: Path) -> None:
    _write_text(tmp_path / "news_briefs" / "latest-EU.jsonl", "not json at all\n")
    _write_jsonl(tmp_path / "news_briefs" / "latest-TW.jsonl", [_macro_payload("TW", "2026-07-17T01:00:00+00:00")])
    items = collect_report_items(tmp_path)
    assert [item.key for item in items] == ["macro:TW"]


def test_collect_macro_keeps_success_and_attaches_status_failure(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path / "news_briefs" / "latest-EU.jsonl",
        [_macro_payload("EU", "2026-07-17T05:00:00+00:00")],
    )
    _write_json(
        tmp_path / "news_macro_analysis_status.json",
        {
            "EU": {
                "latest_failure": {
                    "failed_at": "2026-07-17T06:00:00+00:00",
                    "error_code": "NewsMacroAnalystError",
                    "error_message": "provider unavailable",
                    "retry_attempt": 1,
                }
            }
        },
    )

    item = collect_report_items(tmp_path)[0]

    assert item.payload["brief_id"] == "brief-EU"
    assert item.payload["latest_failure"]["error_message"] == "provider unavailable"


def test_collect_regional_missing_dir_is_normal(tmp_path: Path) -> None:
    """universe_runs/ absent → 0 item regional, les autres kinds restent collectés."""
    _write_jsonl(tmp_path / "news_briefs" / "latest-EU.jsonl", [_macro_payload("EU", "2026-07-16T08:00:00+00:00")])
    items = collect_report_items(tmp_path)
    assert [item.kind for item in items] == ["macro"]


def test_collect_regional_sorted_by_as_of_desc(tmp_path: Path) -> None:
    _write_json(tmp_path / "universe_runs" / "latest-EU.json", _regional_payload("2026-07-15T10:00:00+00:00"))
    _write_json(tmp_path / "universe_runs" / "latest-TW.json", _regional_payload("2026-07-17T02:00:00+00:00"))
    items = collect_report_items(tmp_path)
    assert [item.key for item in items] == ["regional:TW", "regional:EU"]
    assert items[0].payload["selected_hotlist"] == ["3443.TW"]


# ---------------------------------------------------------------------------
# Collecte — micro
# ---------------------------------------------------------------------------


def test_collect_micro_picks_most_recent_brief(tmp_path: Path) -> None:
    """Le brief le plus récent (as_of) gagne, quel que soit le depth."""
    envelope = _micro_envelope(
        "AAPL",
        {
            "screen": _micro_brief("AAPL", "2026-07-10T09:00:00+00:00", "screen"),
            "deep": _micro_brief("AAPL", "2026-07-15T09:00:00+00:00", "deep"),
        },
    )
    _write_json(tmp_path / "company_intelligence" / "current" / "aapl.json", envelope)
    items = collect_report_items(tmp_path)
    assert len(items) == 1
    assert items[0].depth == "deep"
    assert items[0].as_of == "2026-07-15T09:00:00+00:00"
    assert items[0].payload["brief_id"] == "AAPL-deep"


def test_collect_micro_screen_wins_when_more_recent(tmp_path: Path) -> None:
    envelope = _micro_envelope(
        "ASML",
        {
            "deep": _micro_brief("ASML", "2026-07-10T09:00:00+00:00", "deep"),
            "screen": _micro_brief("ASML", "2026-07-16T09:00:00+00:00", "screen"),
        },
    )
    _write_json(tmp_path / "company_intelligence" / "current" / "asml.json", envelope)
    items = collect_report_items(tmp_path)
    assert items[0].depth == "screen"


def test_collect_micro_tie_prefers_deep(tmp_path: Path) -> None:
    """as_of identiques → deep préféré (plus informatif)."""
    stamp = "2026-07-12T09:00:00+00:00"
    envelope = _micro_envelope(
        "NVDA",
        {
            "screen": _micro_brief("NVDA", stamp, "screen"),
            "deep": _micro_brief("NVDA", stamp, "deep"),
        },
    )
    _write_json(tmp_path / "company_intelligence" / "current" / "nvda.json", envelope)
    items = collect_report_items(tmp_path)
    assert items[0].depth == "deep"


def test_collect_micro_label_with_company_names(tmp_path: Path) -> None:
    envelope = _micro_envelope("ASML", {"screen": _micro_brief("ASML", "2026-07-16T09:00:00+00:00", "screen")})
    _write_json(tmp_path / "company_intelligence" / "current" / "asml.json", envelope)
    items = collect_report_items(tmp_path, company_names={"ASML": "ASML Holding"})
    assert items[0].label == "ASML — ASML Holding"
    assert items[0].key == "micro:ASML"


def test_collect_micro_label_without_company_names(tmp_path: Path) -> None:
    envelope = _micro_envelope("AAPL", {"screen": _micro_brief("AAPL", "2026-07-16T09:00:00+00:00", "screen")})
    _write_json(tmp_path / "company_intelligence" / "current" / "aapl.json", envelope)
    items = collect_report_items(tmp_path)
    assert items[0].label == "AAPL"


def test_collect_micro_skips_envelope_without_symbol(tmp_path: Path) -> None:
    envelope = {"schema_version": 1, "briefs": {"screen": _micro_brief("X", "2026-07-16T09:00:00+00:00", "screen")}}
    _write_json(tmp_path / "company_intelligence" / "current" / "ghost.json", envelope)
    assert collect_report_items(tmp_path) == []


def test_collect_micro_sorted_by_as_of_desc(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "company_intelligence" / "current" / "aapl.json",
        _micro_envelope("AAPL", {"screen": _micro_brief("AAPL", "2026-07-11T09:00:00+00:00", "screen")}),
    )
    _write_json(
        tmp_path / "company_intelligence" / "current" / "asml.json",
        _micro_envelope("ASML", {"screen": _micro_brief("ASML", "2026-07-16T09:00:00+00:00", "screen")}),
    )
    items = collect_report_items(tmp_path)
    assert [item.key for item in items] == ["micro:ASML", "micro:AAPL"]


def test_collect_micro_keeps_brief_and_attaches_run_failure(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "company_intelligence" / "current" / "aapl.json",
        _micro_envelope(
            "AAPL",
            {"screen": _micro_brief("AAPL", "2026-07-17T05:00:00+00:00", "screen")},
        ),
    )
    _write_json(
        tmp_path / "company_analysis_runs" / "latest" / "aapl.json",
        {
            "symbol": "AAPL",
            "status": "success",
            "latest_failure": {
                "symbol": "AAPL",
                "status": "error",
                "as_of": "2026-07-17T06:00:00+00:00",
                "error_code": "CompanyMicroAnalystError",
                "retry_attempt": 3,
                "next_retry_at": "2026-07-17T08:00:00+00:00",
            },
        },
    )

    item = collect_report_items(tmp_path)[0]

    assert item.payload["brief_id"] == "AAPL-screen"
    assert item.payload["latest_failure"]["retry_attempt"] == 3


# ---------------------------------------------------------------------------
# Collecte — ordre des sections
# ---------------------------------------------------------------------------


def test_collect_section_order_fixed_regardless_of_as_of(tmp_path: Path) -> None:
    """Sections global → macro → regional → micro même si un rapport plus vieux précède."""
    _write_json(tmp_path / "global_universe_postures" / "current.json", _global_payload("2026-07-10T06:00:00+00:00"))
    _write_jsonl(tmp_path / "news_briefs" / "latest-EU.jsonl", [_macro_payload("EU", "2026-07-17T08:00:00+00:00")])
    _write_json(tmp_path / "universe_runs" / "latest-TW.json", _regional_payload("2026-07-17T09:00:00+00:00"))
    _write_json(
        tmp_path / "company_intelligence" / "current" / "aapl.json",
        _micro_envelope("AAPL", {"screen": _micro_brief("AAPL", "2026-07-17T10:00:00+00:00", "screen")}),
    )
    items = collect_report_items(tmp_path)
    assert [item.kind for item in items] == ["global", "macro", "regional", "micro"]
    assert [item.key for item in items] == ["global:current", "macro:EU", "regional:TW", "micro:AAPL"]


# ---------------------------------------------------------------------------
# build_gallery_row
# ---------------------------------------------------------------------------


def _item(kind: str, as_of: str | None, *, label: str = "EU", key: str | None = None, depth: str | None = None) -> ReportItem:
    return ReportItem(kind=kind, key=key or f"{kind}:x", label=label, as_of=as_of, depth=depth, payload={})


def test_gallery_row_renders_dot_label_age() -> None:
    rendered = _render(build_gallery_row(_item("macro", "2026-07-17T09:00:00+00:00"), now=NOW))
    assert "●" in rendered
    assert "EU" in rendered
    assert "3h" in rendered


def test_gallery_row_age_minutes() -> None:
    rendered = _render(build_gallery_row(_item("macro", "2026-07-17T11:15:00+00:00"), now=NOW))
    assert "45m" in rendered


def test_gallery_row_age_days() -> None:
    rendered = _render(build_gallery_row(_item("regional", "2026-07-15T12:00:00+00:00", label="TW"), now=NOW))
    assert "2d" in rendered


def test_gallery_row_no_as_of_renders_dash() -> None:
    rendered = _render(build_gallery_row(_item("macro", None), now=NOW))
    assert "—" in rendered


def test_gallery_row_future_as_of_clamps_to_zero() -> None:
    rendered = _render(build_gallery_row(_item("macro", "2026-07-17T13:00:00+00:00"), now=NOW))
    assert "0m" in rendered


def test_gallery_row_dot_uses_kind_palette_color() -> None:
    """La pastille porte la couleur du kind (contrat pour l'écran galerie)."""
    for kind, expected in KIND_DOT_STYLES.items():
        row = build_gallery_row(_item(kind, None), now=NOW)
        assert expected in _styles(row), f"kind={kind}"


def test_gallery_row_global_dot_is_accent() -> None:
    row = build_gallery_row(_item("global", None, label="Global"), now=NOW)
    assert CASYS_ACCENT in _styles(row)


def test_gallery_row_micro_label_with_company_name() -> None:
    item = _item("micro", "2026-07-17T06:00:00+00:00", label="ASML — ASML Holding", key="micro:ASML", depth="deep")
    rendered = _render(build_gallery_row(item, now=NOW))
    assert "ASML — ASML Holding" in rendered
    assert "6h" in rendered
