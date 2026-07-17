"""Tests de la page Reports (pages/reports_gallery.py) — galerie des rapports LLM.

Couvre :
- build_report_detail par kind (global / macro / regional / micro) : champs clés,
  pastilles et couleurs (spans), troncatures « +N de plus », placeholder None,
  état vide régional.
- Intégration Textual : page top-level (touche 8) enregistrée dans PAGES /
  _PAGE_WIDGETS, titre RAPPORTS rendu, j navigue, r re-scanne, footer contextuel,
  binding r retiré de la page universe.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Group
from rich.text import Text
from textual.widgets import DataTable, Static

import trader.interfaces.cockpit.app as cockpit_module
from trader.interfaces.cockpit.app import CockpitApp
from trader.interfaces.cockpit.pages import PAGE_BY_KEY, PAGES
from trader.interfaces.cockpit.pages.reports_gallery import (
    ReportsPage,
    build_report_detail,
)
from trader.interfaces.cockpit.pages.universe import UniversePage
from trader.interfaces.cockpit.projections.reports import ReportItem
from trader.interfaces.cockpit.shell import build_footer
from trader.interfaces.cockpit.supervisor import DaemonVitalState
from trader.interfaces.ui.palette import (
    CASYS_ACCENT,
    CASYS_ERROR,
    CASYS_SUCCESS,
    CASYS_WARNING,
)

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


def _flatten(renderable) -> list[Text]:
    """Tous les Text d'un arbre Group/Text (pour inspecter spans et plain)."""
    if isinstance(renderable, Text):
        return [renderable]
    if isinstance(renderable, Group):
        texts: list[Text] = []
        for child in renderable.renderables:
            texts.extend(_flatten(child))
        return texts
    return []


def _all_text(renderable) -> str:
    flattened = _flatten(renderable)
    if flattened:
        return "\n".join(text.plain for text in flattened)
    return _render(renderable)


def _span_styles_for(renderable, needle: str) -> list[str]:
    """Styles des spans dont le texte contient ``needle``."""
    styles: list[str] = []
    for text in _flatten(renderable):
        for span in text.spans:
            if needle in text.plain[span.start : span.end]:
                styles.append(str(span.style))
    return styles


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{json.dumps(row)}\n" for row in rows), encoding="utf-8")


async def _wait_for(predicate, timeout: float = 5.0) -> bool:
    """Attend une condition (worker thread) en laissant tourner la loop Textual."""
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(0.05)
        elapsed += 0.05
    return False


# ---------------------------------------------------------------------------
# Items synthétiques
# ---------------------------------------------------------------------------


def _global_item(payload: dict | None = None) -> ReportItem:
    return ReportItem(
        kind="global",
        key="global:current",
        label="Global",
        as_of="2026-07-17T06:00:00+00:00",
        depth=None,
        payload=payload
        if payload is not None
        else {
            "as_of": "2026-07-17T06:00:00+00:00",
            "venue_posture": {"EU": "favor", "TW": "selective", "US": "watch", "JP": "avoid"},
            "gross_mode": "normal",
            "net_bias": "long",
            "rationale": "breadth improving",
            "family_priority": {"favored": ["semis"], "deprioritized": ["banks"]},
            "valid_until": "2026-07-18T06:00:00+00:00",
            "history_count": 38,
        },
    )


def _macro_item(payload: dict) -> ReportItem:
    return ReportItem(
        kind="macro",
        key="macro:EU",
        label="EU",
        as_of=payload.get("as_of"),
        depth=None,
        payload=payload,
    )


def _macro_payload(points: list[dict] | None = None) -> dict:
    return {
        "brief_id": "brief-EU",
        "venue": "EU",
        "as_of": "2026-07-17T05:00:00+00:00",
        "valid_until": "2026-07-18T00:00:00+00:00",
        "zones": {
            "europe": points
            if points is not None
            else [
                {
                    "point": "pmi up",
                    "severity": "risk",
                    "signal": "strong",
                    "direction": "up",
                    "horizon": "1w",
                }
            ]
        },
        "families": {
            "semis": [
                {"point": "asml orders", "severity": "watch", "signal": "weak", "horizon": "2w"}
            ]
        },
        "symbols": {
            "ASML": [
                {"point": "earnings beat", "severity": "info", "signal": "event", "direction": "up"}
            ]
        },
        "alerts": ["eurostoxx gap down"],
    }


def _regional_item(payload: dict) -> ReportItem:
    return ReportItem(
        kind="regional",
        key="regional:TW",
        label="TW",
        as_of=payload.get("as_of"),
        depth=None,
        payload=payload,
    )


def _micro_item(payload: dict | None = None) -> ReportItem:
    return ReportItem(
        kind="micro",
        key="micro:AAPL",
        label="AAPL — Apple Inc.",
        as_of="2026-07-17T04:00:00+00:00",
        depth="deep",
        payload=payload
        if payload is not None
        else {
            "symbol": "AAPL",
            "as_of": "2026-07-17T04:00:00+00:00",
            "depth": "deep",
            "business": {"summary": "makes phones", "points": []},
            "company_thesis": {
                "status": "intact",
                "summary": "ecosystem lock-in",
                "pillars": [
                    {"point": f"pillar {index}", "evidence_label": f"ev {index}"}
                    for index in range(8)
                ],
                "confirming_evidence": [{"point": "services growth"}],
                "disconfirming_evidence": ["china share loss"],
                "kill_criteria": [{"point": "margin < 30%"}],
            },
            "selection_view": {"posture": "candidate", "confidence": 0.6},
            "security_readiness": "ready",
            "coverage": "full",
            "source_refs": ["filing:1", "news:2", "transcript:3"],
        },
    )


# ---------------------------------------------------------------------------
# build_report_detail — placeholder / kind inconnu
# ---------------------------------------------------------------------------


def test_detail_none_placeholder() -> None:
    assert "sélectionne un rapport" in _all_text(build_report_detail(None, now=NOW))


def test_detail_unknown_kind() -> None:
    item = ReportItem(kind="weird", key="weird:1", label="?", as_of=None, depth=None, payload={})
    assert "kind de rapport inconnu" in _all_text(build_report_detail(item, now=NOW))


# ---------------------------------------------------------------------------
# build_report_detail — global
# ---------------------------------------------------------------------------


def test_detail_global_fields_and_styles() -> None:
    detail = build_report_detail(_global_item(), now=NOW)
    text = _all_text(detail)

    assert "GLOBAL" in text
    assert "as_of 2026-07-17 06:00" in text
    assert "il y a 6h" in text
    assert "valid_until 2026-07-18 06:00" in text
    assert "gross normal" in text
    assert "net long" in text
    assert "▎" in text and "breadth improving" in text
    assert "favored semis" in text
    assert "deprioritized banks" in text
    assert "38 changements aujourd'hui" in text

    # Chips venue_posture colorés : favor=success, selective=accent,
    # watch=warning, avoid=error.
    assert any(CASYS_SUCCESS in style for style in _span_styles_for(detail, "favor"))
    assert any(CASYS_ACCENT in style for style in _span_styles_for(detail, "selective"))
    assert any(CASYS_WARNING in style for style in _span_styles_for(detail, "watch"))
    assert any(CASYS_ERROR in style for style in _span_styles_for(detail, "avoid"))


def test_detail_global_minimal_payload_no_crash() -> None:
    text = _all_text(build_report_detail(_global_item(payload={}), now=NOW))
    assert "GLOBAL" in text
    assert "—" in text  # as_of inconnu


# ---------------------------------------------------------------------------
# build_report_detail — macro
# ---------------------------------------------------------------------------


def test_detail_macro_fields_and_styles() -> None:
    detail = build_report_detail(_macro_item(_macro_payload()), now=NOW)
    text = _all_text(detail)

    assert "MACRO — EU" in text
    assert "brief-EU" in text
    assert "valid_until 2026-07-18 00:00" in text
    assert "ZONES" in text and "europe" in text
    assert "FAMILIES" in text and "semis" in text
    assert "SYMBOLS" in text and "ASML" in text
    assert "pmi up" in text
    assert "[strong]" in text
    assert "(up · 1w)" in text
    assert "ALERTS" in text and "eurostoxx gap down" in text

    # Pastille severity : risk → error, watch → warning ; badge signal strong → accent.
    dot_styles = _span_styles_for(detail, "●")
    assert any(CASYS_ERROR in style for style in dot_styles)
    assert any(CASYS_WARNING in style for style in dot_styles)
    assert any(CASYS_ACCENT in style for style in _span_styles_for(detail, "[strong]"))
    # Alertes rendues en warning.
    assert any(CASYS_WARNING in style for style in _span_styles_for(detail, "eurostoxx gap down"))


def test_detail_macro_point_truncation() -> None:
    points = [{"point": f"point {index}", "severity": "info"} for index in range(15)]
    text = _all_text(build_report_detail(_macro_item(_macro_payload(points)), now=NOW))
    assert "point 11" in text  # 12 premiers affichés (0-11)
    assert "point 12" not in text
    assert "+3 de plus" in text


def test_detail_macro_empty_sections_no_crash() -> None:
    payload = _macro_payload()
    payload["zones"] = {}
    payload["families"] = {}
    payload["symbols"] = {}
    payload["alerts"] = []
    text = _all_text(build_report_detail(_macro_item(payload), now=NOW))
    assert "MACRO — EU" in text
    assert "ZONES" not in text
    assert "ALERTS" not in text


# ---------------------------------------------------------------------------
# build_report_detail — regional
# ---------------------------------------------------------------------------


def test_detail_regional_full() -> None:
    item = _regional_item(
        {
            "summary": "rotation into semis",
            "family_postures": {"semis": "favored"},
            "selected_hotlist": ["3443.TW", "2317.TW"],
            "as_of": "2026-07-17T05:30:00+00:00",
        }
    )
    detail = build_report_detail(item, now=NOW)
    text = _all_text(detail)

    assert "REGIONAL — TW" in text
    assert "▎" in text and "rotation into semis" in text
    assert "semis" in text and "favored" in text
    assert "hotlist sélectionnée : 2 symboles" in text
    assert "as_of 2026-07-17 05:30" in text
    assert any(CASYS_SUCCESS in style for style in _span_styles_for(detail, "favored"))


def test_detail_regional_empty_payload_explicit_state() -> None:
    text = _all_text(build_report_detail(_regional_item({}), now=NOW))
    assert "pas encore de run régional" in text
    assert "clôtures/pre-open" in text


# ---------------------------------------------------------------------------
# build_report_detail — micro
# ---------------------------------------------------------------------------


def test_detail_micro_fields_and_styles() -> None:
    detail = build_report_detail(_micro_item(), now=NOW)
    text = _all_text(detail)

    assert "AAPL — Apple Inc." in text
    assert "[deep]" in text
    assert "as_of 2026-07-17 04:00" in text
    assert "intact" in text
    assert "▎" in text and "ecosystem lock-in" in text
    assert "PILLARS" in text
    assert "pillar 0" in text and "(ev 0)" in text  # evidence_label affiché
    assert "CONFIRMING" in text and "services growth" in text
    assert "DISCONFIRMING" in text and "china share loss" in text
    assert "KILL CRITERIA" in text and "margin < 30%" in text
    assert "candidate" in text and "confidence 0.60" in text
    assert "readiness ready" in text
    assert "coverage full" in text
    assert "BUSINESS" in text and "makes phones" in text
    assert "3 sources" in text

    assert any(CASYS_ACCENT in style for style in _span_styles_for(detail, "deep"))
    assert any(CASYS_SUCCESS in style for style in _span_styles_for(detail, "intact"))


def test_detail_micro_list_truncation() -> None:
    detail = build_report_detail(_micro_item(), now=NOW)
    text = _all_text(detail)
    assert "pillar 5" in text  # 6 premiers affichés (0-5)
    assert "pillar 6" not in text
    assert "+2 de plus" in text


def test_detail_micro_minimal_payload_no_crash() -> None:
    text = _all_text(build_report_detail(_micro_item(payload={"symbol": "AAPL"}), now=NOW))
    assert "AAPL — Apple Inc." in text
    assert "PILLARS" not in text


# ---------------------------------------------------------------------------
# Intégration Textual — universe → r → galerie
# ---------------------------------------------------------------------------


def _patch_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_AGENT_TRACE_FILE", tmp_path / "agent_trace.log")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")
    # Daemon vivant → pas d'overlay FirstRunScreen (pattern test_cockpit_page_portfolio).
    alive = DaemonVitalState(status="alive", since_seconds=5.0, battement_old=False)
    monkeypatch.setattr(cockpit_module, "daemon_vital_state", lambda _path: alive)


def _make_minimal_state(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "current_report.json",
        {
            "ts": "2026-07-17T03:00:00+00:00",
            "dry_run": True,
            "portfolio": {"cash": 100000.0, "equity": 100000.0, "holdings": []},
            "decisions": [],
        },
    )
    _write_json(
        tmp_path / "daemon_status.json",
        {
            "phase": "idle",
            "decisions_done": 0,
            "symbols_total": 0,
            "model_calls_used": 0,
            "max_model_calls_per_cycle": 25,
        },
    )


def _write_report_artifacts(state_dir: Path) -> None:
    """Mini-artifacts lus par collect_report_items : 1 par kind."""
    _write_json(
        state_dir / "global_universe_postures" / "current.json",
        {
            "as_of": "2026-07-17T06:00:00+00:00",
            "venue_posture": {"EU": "favor"},
            "gross_mode": "normal",
            "net_bias": "long",
            "rationale": "breadth improving",
            "valid_until": "2026-07-18T06:00:00+00:00",
            "family_priority": {"favored": ["semis"], "deprioritized": ["banks"]},
        },
    )
    _write_jsonl(
        state_dir / "news_briefs" / "latest-EU.jsonl",
        [
            {
                "brief_id": "brief-EU",
                "venue": "EU",
                "as_of": "2026-07-17T05:00:00+00:00",
                "valid_until": "2026-07-18T00:00:00+00:00",
                "zones": {
                    "europe": [
                        {"point": "pmi up", "severity": "info", "signal": "weak"}
                    ]
                },
                "families": {},
                "symbols": {},
                "alerts": [],
            }
        ],
    )
    _write_json(
        state_dir / "universe_runs" / "latest-TW.json",
        {
            "summary": "rotation into semis",
            "family_postures": {"semis": "favored"},
            "selected_hotlist": ["3443.TW"],
            "as_of": "2026-07-17T05:30:00+00:00",
        },
    )
    _write_json(
        state_dir / "company_intelligence" / "current" / "a1b2c3d4.json",
        {
            "schema_version": 1,
            "symbol": "AAPL",
            "briefs": {
                "deep": {
                    "symbol": "AAPL",
                    "as_of": "2026-07-17T04:00:00+00:00",
                    "depth": "deep",
                    "business": {"summary": "makes phones"},
                    "company_thesis": {"status": "intact", "summary": "lock-in", "pillars": []},
                    "selection_view": {"posture": "candidate", "confidence": 0.6},
                    "security_readiness": "ready",
                    "coverage": "full",
                    "source_refs": ["filing:1"],
                }
            },
        },
    )


def _gallery_title(page: ReportsPage) -> str:
    return str(page.query_one("#reports-list-panel").border_title)


# ---------------------------------------------------------------------------
# Enregistrement de la page + footer (pur, sans UI)
# ---------------------------------------------------------------------------


def test_reports_page_registered() -> None:
    """PageSpec 8 + _PAGE_WIDGETS — la navigation chiffres est générée depuis PAGES."""
    spec = PAGE_BY_KEY.get("reports")
    assert spec is not None
    assert spec.number == 8
    assert spec.label == "reports"
    assert spec.widget_id == "reports-page"
    assert PAGES[-1] == spec
    assert cockpit_module._PAGE_WIDGETS["reports"] is ReportsPage


def test_universe_no_longer_has_reports_binding() -> None:
    """L'accès modal (r sur universe) est retiré — la galerie est la page 8."""
    universe_keys = {binding.key for binding in UniversePage.BINDINGS}
    assert "r" not in universe_keys


def test_footer_reports_context() -> None:
    footer = build_footer("reports").plain
    assert "navigate" in footer
    assert "refresh" in footer
    assert "rapports" not in build_footer("universe").plain


# ---------------------------------------------------------------------------
# Intégration Textual — touche 8 → page Reports
# ---------------------------------------------------------------------------


async def test_reports_page_opens_and_populates(tmp_path: Path, monkeypatch) -> None:
    """Touche 8 → page affichée ; le worker peuple la liste et le détail."""
    _patch_paths(monkeypatch, tmp_path)
    _make_minimal_state(tmp_path)
    _write_report_artifacts(tmp_path)

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.press("8")
        await pilot.pause()

        page = app.query_one("#reports-page", ReportsPage)
        assert page.display is True

        # Le worker thread scanne le disque puis peuple — attendre la fin.
        found = await _wait_for(
            lambda: page.query_one("#reports-list", DataTable).row_count >= 8
        )
        assert found, "la galerie n'a pas été peuplée par le worker"

        title = _gallery_title(page)
        assert "RAPPORTS" in title
        assert "1 global" in title
        assert "1 macro" in title
        assert "1 régional" in title
        assert "1 micro" in title

        # Le détail du premier item (global) est rendu.
        detail = page.query_one("#reports-detail-body", Static)
        assert "GLOBAL" in _all_text(detail.content)


async def test_reports_page_navigation_and_refresh(tmp_path: Path, monkeypatch) -> None:
    """j déplace le curseur (détail suit) ; r re-scanne les artifacts."""
    _patch_paths(monkeypatch, tmp_path)
    _make_minimal_state(tmp_path)
    _write_report_artifacts(tmp_path)

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.press("8")
        await pilot.pause()
        page = app.query_one("#reports-page", ReportsPage)
        assert await _wait_for(
            lambda: page.query_one("#reports-list", DataTable).row_count >= 8
        )

        # j × 2 : global → header section MACRO (inerte) → macro:EU (détail suit).
        await pilot.press("j")
        await pilot.pause()
        await pilot.press("j")
        await pilot.pause()
        detail = page.query_one("#reports-detail-body", Static)
        assert "MACRO — EU" in _all_text(detail.content)

        # r : re-scan manuel — un brief US apparaît → 2 macro.
        _write_jsonl(
            tmp_path / "news_briefs" / "latest-US.jsonl",
            [{"brief_id": "brief-US", "venue": "US", "as_of": "2026-07-17T07:00:00+00:00"}],
        )
        await pilot.press("r")
        refreshed = await _wait_for(lambda: "2 macro" in _gallery_title(page))
        assert refreshed, "le re-scan n'a pas pris en compte le nouvel artifact"


async def test_reports_page_update_state_never_raises(tmp_path: Path, monkeypatch) -> None:
    """update_state avec états partiels ne lève jamais (contrat page)."""
    _patch_paths(monkeypatch, tmp_path)
    _make_minimal_state(tmp_path)

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.pause()
        page = app.query_one("#reports-page", ReportsPage)
        page.update_state({})
        page.update_state({"company_map": {"AAPL": "Apple Inc."}})
        page.update_state({"company_map": None})
