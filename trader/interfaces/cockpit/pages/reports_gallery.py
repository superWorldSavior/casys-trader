"""reports_gallery — page 8 « Reports » : galerie des rapports LLM (global/macro/régional/micro).

Page standard du ContentSwitcher (contrairement à un modal — révision) :

    ┌────────────────────────────┬──────────────────────────────────────┐
    │ RAPPORTS — compteurs       │ RAPPORTS — galerie                   │
    │ DataTable #reports-list    │ VerticalScroll #reports-detail       │
    │ sections global→macro→…    │ Static #reports-detail-body          │
    └────────────────────────────┴──────────────────────────────────────┘

La collecte disque (projections/reports.py) tourne dans un worker thread —
jamais sur le thread UI, jamais dans le cycle de polling : scan au mount,
rescan throttlé (>60 s) depuis ``update_state``, rescan immédiat quand
``company_map`` devient disponible, touche ``r`` manuelle. Builders purs
``(item, now) → RenderableType`` testables sans Textual. Lecture seule.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Group, RenderableType
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import DataTable, Static

from trader.interfaces.cockpit import format as f
from trader.interfaces.cockpit.pages._shared import (
    PANEL_CSS,
    ResizeRefresh,
    preserve_cursor,
    rows_available,
)
from trader.interfaces.cockpit.projections.reports import (
    KIND_ORDER,
    ReportItem,
    build_gallery_row,
    collect_report_items,
)
from trader.interfaces.ui.palette import (
    CASYS_ACCENT,
    CASYS_DIM,
    CASYS_ERROR,
    CASYS_FAINT,
    CASYS_FG,
    CASYS_MUTED,
    CASYS_SUCCESS,
    CASYS_WARNING,
)
from trader.support.coercion import finite_float as _safe_float

logger = logging.getLogger(__name__)

_MACRO_POINT_LIMIT = 12
_MICRO_LIST_LIMIT = 6

# Postures de venue/famille : favor→success, selective→accent, watch→warning,
# avoid→error. Valeur inconnue → muted (la couleur n'est jamais bloquante).
_POSTURE_STYLES: dict[str, str] = {
    "favor": CASYS_SUCCESS,
    "favored": CASYS_SUCCESS,
    "selective": CASYS_ACCENT,
    "watch": CASYS_WARNING,
    "avoid": CASYS_ERROR,
}

# Pastille severity des points macro : info→muted, watch→warning, risk→error.
_SEVERITY_DOT_STYLES: dict[str, str] = {
    "info": CASYS_MUTED,
    "watch": CASYS_WARNING,
    "risk": CASYS_ERROR,
}

# Badge signal des points macro : weak→faint, strong→accent, event→warning.
_SIGNAL_STYLES: dict[str, str] = {
    "weak": CASYS_FAINT,
    "strong": CASYS_ACCENT,
    "event": CASYS_WARNING,
}

_THESIS_STATUS_STYLES: dict[str, str] = {
    "intact": CASYS_SUCCESS,
    "weakening": CASYS_WARNING,
    "broken": CASYS_ERROR,
}


# ---------------------------------------------------------------------------
# Helpers purs
# ---------------------------------------------------------------------------


def _quote(text: str) -> Text:
    """Citation bordée « ▎ » (convention symbol_detail)."""

    quote = Text()
    quote.append("▎ ", style=CASYS_ACCENT)
    quote.append(text, style=CASYS_MUTED)
    return quote


def _age(as_of: object, *, now: datetime) -> str:
    """Âge du rapport : "45m" / "3h" / "2d", "—" quand inconnu."""

    parsed = f.parse_ts(as_of)
    if parsed is None:
        return "—"
    reference = now if now.tzinfo else now.replace(tzinfo=UTC)
    minutes = max(0, int((reference - parsed).total_seconds())) // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _ts_label(raw: object) -> str:
    """Timestamp ISO → "YYYY-MM-DD HH:MM" ; brut raccourci sinon, "—" si vide."""

    parsed = f.parse_ts(raw)
    if parsed is None:
        text = str(raw or "").strip()
        return text or "—"
    return parsed.strftime("%Y-%m-%d %H:%M")


def _meta_line(payload: dict, *, now: datetime, show_valid_until: bool = True) -> Text:
    """Ligne méta commune : as_of + âge relatif (+ valid_until si présent)."""

    meta = Text()
    meta.append(f"as_of {_ts_label(payload.get('as_of'))}", style=CASYS_DIM)
    meta.append(f" · il y a {_age(payload.get('as_of'), now=now)}", style=CASYS_FAINT)
    if show_valid_until and str(payload.get("valid_until") or "").strip():
        meta.append(f" · valid_until {_ts_label(payload.get('valid_until'))}", style=CASYS_DIM)
    return meta


def _str_list(raw: object) -> list[str]:
    return [str(value) for value in raw if str(value).strip()] if isinstance(raw, list) else []


def _append_failure_warning(parts: list[RenderableType], payload: dict) -> None:
    latest = f.safe_dict(payload.get("latest_failure"))
    failure = latest if latest else payload if payload.get("status") in {"error", "invalid"} else {}
    if not failure:
        return
    error = str(failure.get("error_code") or failure.get("status") or "unknown").strip()
    message = str(failure.get("error_message") or "").strip()
    attempt = failure.get("retry_attempt") or failure.get("attempt")
    next_retry_at = str(failure.get("next_retry_at") or failure.get("next_at") or "").strip()
    detail = f"dernier essai en échec : {error}"
    if attempt:
        detail += f" (tentative {attempt})"
    if next_retry_at:
        detail += f" · nouvelle tentative {_ts_label(next_retry_at)}"
    elif failure.get("retry_delay_seconds") == 0:
        detail += " · tentatives automatiques épuisées"
    parts.append(Text(""))
    parts.append(Text(detail, style=f"bold {CASYS_WARNING}"))
    if message:
        parts.append(Text(message, style=CASYS_DIM))


# ---------------------------------------------------------------------------
# Builders de détail — un par kind
# ---------------------------------------------------------------------------


def _build_global_detail(item: ReportItem, *, now: datetime) -> RenderableType:
    payload = item.payload
    parts: list[RenderableType] = []

    header = Text()
    header.append("GLOBAL", style=f"bold {CASYS_ACCENT}")
    header.append(" — posture d'univers", style=CASYS_MUTED)
    parts.append(header)
    parts.append(_meta_line(payload, now=now))
    _append_failure_warning(parts, payload)
    parts.append(Text(""))

    postures = f.safe_dict(payload.get("venue_posture"))
    if postures:
        chips = Text()
        for index, (venue, posture) in enumerate(postures.items()):
            if index:
                chips.append("   ", style=CASYS_FAINT)
            chips.append(f"{venue} ", style=CASYS_DIM)
            chips.append(
                str(posture),
                style=f"bold {_POSTURE_STYLES.get(str(posture).strip().lower(), CASYS_MUTED)}",
            )
        parts.append(chips)

    mode = Text()
    mode.append(f"gross {payload.get('gross_mode') or '—'}", style=CASYS_DIM)
    mode.append("  ·  ", style=CASYS_FAINT)
    mode.append(f"net {payload.get('net_bias') or '—'}", style=CASYS_DIM)
    parts.append(mode)

    rationale = str(payload.get("rationale") or "").strip()
    if rationale:
        parts.append(Text(""))
        parts.append(_quote(rationale))

    family_priority = f.safe_dict(payload.get("family_priority"))
    favored = _str_list(family_priority.get("favored"))
    deprioritized = _str_list(family_priority.get("deprioritized"))
    if favored or deprioritized:
        parts.append(Text(""))
        families = Text()
        if favored:
            families.append("favored ", style=CASYS_FAINT)
            families.append(", ".join(favored), style=CASYS_SUCCESS)
        if deprioritized:
            if favored:
                families.append("   ", style=CASYS_FAINT)
            families.append("deprioritized ", style=CASYS_FAINT)
            families.append(", ".join(deprioritized), style=CASYS_DIM)
        parts.append(families)

    history_count = payload.get("history_count")
    if isinstance(history_count, int) and not isinstance(history_count, bool):
        parts.append(Text(""))
        parts.append(Text(f"{history_count} changements aujourd'hui", style=CASYS_FAINT))

    return Group(*parts)


def _macro_point_line(entry: object) -> Text:
    """Un point macro : pastille severity + badge signal + texte + direction/horizon."""

    if isinstance(entry, dict):
        point = str(entry.get("point") or "").strip()
        severity = str(entry.get("severity") or "info").strip().lower()
        signal = str(entry.get("signal") or "").strip().lower()
        direction = str(entry.get("direction") or "").strip()
        horizon = str(entry.get("horizon") or "").strip()
    else:
        point, severity, signal, direction, horizon = str(entry).strip(), "info", "", "", ""

    line = Text()
    line.append("● ", style=_SEVERITY_DOT_STYLES.get(severity, CASYS_MUTED))
    if signal:
        line.append(f"[{signal}] ", style=_SIGNAL_STYLES.get(signal, CASYS_FAINT))
    line.append(point or "—", style=CASYS_MUTED)
    tail = " · ".join(part for part in (direction, horizon) if part)
    if tail:
        line.append(f"  ({tail})", style=CASYS_FAINT)
    return line


def _macro_section(title: str, mapping: dict, parts: list[RenderableType]) -> None:
    """Section nom → points (troncature à _MACRO_POINT_LIMIT, « +N de plus »)."""

    entries = [
        (name, points)
        for name, points in mapping.items()
        if isinstance(points, list) and points
    ]
    if not entries:
        return
    parts.append(Text(title, style=CASYS_FAINT))
    for name, points in entries:
        parts.append(Text(f"  {name}", style=CASYS_DIM))
        for entry in points[:_MACRO_POINT_LIMIT]:
            parts.append(Text("    ") + _macro_point_line(entry))
        more = len(points) - _MACRO_POINT_LIMIT
        if more > 0:
            parts.append(Text(f"    +{more} de plus", style=CASYS_FAINT))
    parts.append(Text(""))


def _build_macro_detail(item: ReportItem, *, now: datetime) -> RenderableType:
    payload = item.payload
    parts: list[RenderableType] = []

    venue = str(payload.get("venue") or item.label)
    header = Text()
    header.append(f"MACRO — {venue}", style=f"bold {CASYS_ACCENT}")
    brief_id = str(payload.get("brief_id") or "").strip()
    if brief_id:
        header.append(f"  ·  {brief_id}", style=CASYS_FAINT)
    parts.append(header)
    parts.append(_meta_line(payload, now=now))
    _append_failure_warning(parts, payload)
    parts.append(Text(""))

    for title, key in (("ZONES", "zones"), ("FAMILIES", "families"), ("SYMBOLS", "symbols")):
        _macro_section(title, f.safe_dict(payload.get(key)), parts)

    alerts = [
        str(alert.get("point") or alert.get("message") or "")
        if isinstance(alert, dict)
        else str(alert)
        for alert in (payload.get("alerts") if isinstance(payload.get("alerts"), list) else [])
    ]
    alerts = [alert for alert in alerts if alert.strip()]
    if alerts:
        parts.append(Text("ALERTS", style=CASYS_FAINT))
        for alert in alerts[:_MACRO_POINT_LIMIT]:
            line = Text()
            line.append("  ● ", style=CASYS_WARNING)
            line.append(alert, style=CASYS_WARNING)
            parts.append(line)
        more = len(alerts) - _MACRO_POINT_LIMIT
        if more > 0:
            parts.append(Text(f"  +{more} de plus", style=CASYS_FAINT))

    return Group(*parts)


def _build_regional_detail(item: ReportItem, *, now: datetime) -> RenderableType:
    payload = item.payload
    header = Text()
    header.append(f"REGIONAL — {item.label}", style=f"bold {CASYS_ACCENT}")

    if not payload:
        return Group(
            header,
            Text(""),
            Text(
                "pas encore de run régional — produit aux clôtures/pre-open",
                style=f"italic {CASYS_FAINT}",
            ),
        )

    parts: list[RenderableType] = [header]
    parts.append(_meta_line(payload, now=now, show_valid_until=False))

    _append_failure_warning(parts, payload)

    summary = str(payload.get("summary") or "").strip()
    if summary:
        parts.append(Text(""))
        parts.append(_quote(summary))

    postures = f.safe_dict(payload.get("family_postures"))
    if postures:
        parts.append(Text(""))
        parts.append(Text("FAMILY POSTURES", style=CASYS_FAINT))
        for family, posture in postures.items():
            line = Text()
            line.append(f"  {family} ", style=CASYS_DIM)
            line.append(
                str(posture),
                style=f"bold {_POSTURE_STYLES.get(str(posture).strip().lower(), CASYS_MUTED)}",
            )
            parts.append(line)

    hotlist = payload.get("selected_hotlist")
    if isinstance(hotlist, list):
        parts.append(Text(""))
        parts.append(
            Text(f"hotlist sélectionnée : {len(hotlist)} symboles", style=CASYS_FAINT)
        )

    return Group(*parts)


def _micro_point_text(entry: object) -> str:
    """Point micro : champ ``point`` + ``(evidence_label)`` optionnel ; str toléré."""

    if isinstance(entry, dict):
        point = str(entry.get("point") or "").strip()
        evidence = str(entry.get("evidence_label") or "").strip()
        return f"{point}  ({evidence})" if point and evidence else point or evidence
    return str(entry).strip()


def _micro_list_section(title: str, entries: object, parts: list[RenderableType]) -> None:
    """Liste micro (pillars/evidence/kill) tronquée à _MICRO_LIST_LIMIT."""

    if not isinstance(entries, list) or not entries:
        return
    parts.append(Text(title, style=CASYS_FAINT))
    for entry in entries[:_MICRO_LIST_LIMIT]:
        text = _micro_point_text(entry)
        if text:
            parts.append(Text(f"  • {text}", style=CASYS_MUTED))
    more = len(entries) - _MICRO_LIST_LIMIT
    if more > 0:
        parts.append(Text(f"  +{more} de plus", style=CASYS_FAINT))
    parts.append(Text(""))


def _build_micro_detail(item: ReportItem, *, now: datetime) -> RenderableType:
    payload = item.payload
    parts: list[RenderableType] = []

    header = Text()
    header.append(item.label, style=f"bold {CASYS_FG}")
    if item.depth:
        header.append("  [", style=CASYS_FAINT)
        header.append(item.depth, style=f"bold {CASYS_ACCENT}")
        header.append("]", style=CASYS_FAINT)
    parts.append(header)
    parts.append(_meta_line(payload, now=now, show_valid_until=False))
    _append_failure_warning(parts, payload)

    thesis = f.safe_dict(payload.get("company_thesis"))
    status = str(thesis.get("status") or "").strip()
    if status:
        status_line = Text()
        status_line.append("thesis ", style=CASYS_FAINT)
        status_line.append(
            status,
            style=f"bold {_THESIS_STATUS_STYLES.get(status.lower(), CASYS_FG)}",
        )
        parts.append(status_line)
    summary = str(thesis.get("summary") or "").strip()
    if summary:
        parts.append(_quote(summary))
    parts.append(Text(""))

    for title, key in (
        ("PILLARS", "pillars"),
        ("CONFIRMING", "confirming_evidence"),
        ("DISCONFIRMING", "disconfirming_evidence"),
        ("KILL CRITERIA", "kill_criteria"),
    ):
        _micro_list_section(title, thesis.get(key), parts)

    selection = f.safe_dict(payload.get("selection_view"))
    posture = str(selection.get("posture") or "").strip()
    confidence = _safe_float(selection.get("confidence"), default=None)
    if posture or confidence is not None:
        selection_line = Text()
        selection_line.append("selection ", style=CASYS_FAINT)
        selection_line.append(posture or "—", style=f"bold {CASYS_FG}")
        if confidence is not None:
            selection_line.append(f" · confidence {confidence:.2f}", style=CASYS_FAINT)
        parts.append(selection_line)

    readiness = str(payload.get("security_readiness") or "").strip()
    coverage = str(payload.get("coverage") or "").strip()
    if readiness or coverage:
        meta = Text()
        if readiness:
            meta.append(f"readiness {readiness}", style=CASYS_DIM)
        if coverage:
            if readiness:
                meta.append("  ·  ", style=CASYS_FAINT)
            meta.append(f"coverage {coverage}", style=CASYS_DIM)
        parts.append(meta)

    business = f.safe_dict(payload.get("business"))
    business_summary = str(business.get("summary") or "").strip()
    if business_summary:
        parts.append(Text(""))
        parts.append(Text("BUSINESS", style=CASYS_FAINT))
        parts.append(_quote(business_summary))

    source_refs = payload.get("source_refs")
    if isinstance(source_refs, list) and source_refs:
        parts.append(Text(""))
        parts.append(Text(f"{len(source_refs)} sources", style=CASYS_FAINT))

    return Group(*parts)


def build_report_detail(item: ReportItem | None, *, now: datetime) -> RenderableType:
    """Détail Rich d'un rapport, dispatché par kind. ``None`` → placeholder."""

    if item is None:
        return Text("sélectionne un rapport", style=f"italic {CASYS_FAINT}")
    if item.kind == "global":
        return _build_global_detail(item, now=now)
    if item.kind == "macro":
        return _build_macro_detail(item, now=now)
    if item.kind == "regional":
        return _build_regional_detail(item, now=now)
    if item.kind == "micro":
        return _build_micro_detail(item, now=now)
    return Text(f"kind de rapport inconnu : {item.kind}", style=f"italic {CASYS_FAINT}")


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


class ReportsPage(ResizeRefresh, Static):
    """Page 8 — Reports : galerie des rapports LLM (lecture seule).

    Le scan disque ne tourne jamais dans le cycle de polling : mount + rescan
    throttlé (>60 s) depuis ``update_state`` + rescan immédiat quand
    ``company_map`` devient disponible + touche ``r`` manuelle.
    """

    BINDINGS = [
        Binding("r", "refresh_gallery", "refresh", show=False),
    ]

    DEFAULT_CSS = (
        PANEL_CSS
        + """
    ReportsPage {
        layout: horizontal;
        height: 100%;
        padding: 1 2 0 2;
    }
    ReportsPage #reports-list-panel {
        width: 38;
        height: 100%;
        margin-right: 1;
    }
    ReportsPage #reports-list {
        height: 100%;
    }
    ReportsPage #reports-detail {
        width: 1fr;
        height: 100%;
    }
    """
    )

    _RESCAN_INTERVAL_S = 60.0

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._row_map: dict[str, ReportItem] = {}
        self._ordered_keys: list[str] = []
        self._last_state: dict = {}
        self._last_scan_at: float = 0.0
        self._scanned_company_names: dict[str, str] = {}
        self._populated_once: bool = False

    def compose(self) -> ComposeResult:
        with Vertical(id="reports-list-panel", classes="casys-panel") as panel:
            panel.border_title = "RAPPORTS — chargement…"
            yield DataTable(id="reports-list", cursor_type="row", show_header=False)
        with VerticalScroll(id="reports-detail", classes="casys-panel") as detail:
            detail.border_title = "RAPPORTS — galerie"
            detail.border_subtitle = "j/k naviguer · r rafraîchir"
            yield Static(id="reports-detail-body")

    def on_mount(self) -> None:
        table = self.query_one("#reports-list", DataTable)
        table.add_column(Text("rapports", style=CASYS_FAINT))
        # État « chargement… » à peu près centré verticalement dans la liste.
        pad = max(0, rows_available(table, reserved=1) // 2 - 1)
        for index in range(pad):
            table.add_row(Text(""), key=f"pad|{index}")
        table.add_row(Text("chargement…", style=f"italic {CASYS_FAINT}"), key="loading")
        self._start_scan()

    # ------------------------------------------------------------------
    # update_state (polling — jamais de scan disque ici, juste un trigger)
    # ------------------------------------------------------------------

    def update_state(self, state: dict) -> None:
        """Stocke l'état ; déclenche un rescan throttlé si utile. Jamais d'exception."""

        try:
            self._last_state = state if isinstance(state, dict) else {}
            names = self._company_names()
            stale = time.monotonic() - self._last_scan_at > self._RESCAN_INTERVAL_S
            # company_map arrive souvent après le scan du mount → rescan
            # immédiat pour résoudre les labels « SYM — Nom » (sans attendre
            # le throttle de 60 s).
            new_names = bool(names) and names != self._scanned_company_names
            if stale or new_names:
                self._start_scan()
        except Exception:
            logger.debug("%s update error", "reports gallery", exc_info=True)

    # ------------------------------------------------------------------
    # Scan disque (worker thread — jamais sur le thread UI)
    # ------------------------------------------------------------------

    def _state_dir(self) -> Path:
        """State dir : ``app._state_dir`` une fois posé, sinon la constante module.

        Les pages sont montées AVANT ``app.on_mount`` (qui pose ``_state_dir``)
        — le scan du mount tomberait sinon sur un cwd potentiellement faux.
        Le fallback lit le module paresseusement (pas de cycle d'import) et
        reste patchable par les tests (``monkeypatch.setattr(cockpit_module, …)``).
        """

        direct = getattr(self.app, "_state_dir", None)
        if direct:
            return Path(direct)
        from trader.interfaces.cockpit import app as cockpit_module

        return Path(cockpit_module._STATE_DIR)

    def _company_names(self) -> dict[str, str]:
        """symbol → nom depuis le ``company_map`` du dernier état reçu (tolérant)."""

        return {
            str(symbol): str(name)
            for symbol, name in f.safe_dict(self._last_state.get("company_map")).items()
        }

    def _start_scan(self, *, show_loading: bool = False) -> None:
        self._last_scan_at = time.monotonic()
        # Pas de flicker du titre sur les rescans d'arrière-plan (60 s) — le
        # « chargement… » n'apparaît qu'au premier peuplement ou sur ``r``.
        if show_loading or not self._populated_once:
            try:
                self.query_one("#reports-list-panel").border_title = "RAPPORTS — chargement…"
            except Exception:
                pass
        try:
            self.run_worker(self._scan_worker, thread=True, exclusive=True, group="reports-scan")
        except Exception:
            pass

    def _scan_worker(self) -> None:
        """Thread : collecte les artifacts puis rappelle le thread UI."""

        try:
            names = self._company_names()
            self._scanned_company_names = dict(names)
            items = collect_report_items(self._state_dir(), company_names=names)
        except Exception:
            items = []
        try:
            # call_from_thread vit sur l'App (pas sur les widgets/screens).
            self.app.call_from_thread(self._populate, items)
        except Exception:
            pass  # page démontée entre-temps

    # ------------------------------------------------------------------
    # Peuplement (thread UI)
    # ------------------------------------------------------------------

    def _populate(self, items: list[ReportItem]) -> None:
        now = datetime.now(UTC)
        self._populated_once = True
        self._row_map = {item.key: item for item in items}
        self._ordered_keys = []

        counts = {kind: 0 for kind in KIND_ORDER}
        for item in items:
            counts[item.kind] = counts.get(item.kind, 0) + 1
        try:
            self.query_one("#reports-list-panel").border_title = (
                f"RAPPORTS — {counts.get('global', 0)} global · {counts.get('macro', 0)} macro"
                f" · {counts.get('regional', 0)} régional · {counts.get('micro', 0)} micro"
            )
        except Exception:
            pass

        table = self.query_one("#reports-list", DataTable)
        with preserve_cursor(table):
            table.clear()
            last_kind: str | None = None
            for item in items:
                if item.kind != last_kind:
                    last_kind = item.kind
                    header_key = f"section|{item.kind}"
                    table.add_row(Text(item.kind.upper(), style=CASYS_FAINT), key=header_key)
                    self._ordered_keys.append(header_key)
                table.add_row(build_gallery_row(item, now=now), key=item.key)
                self._ordered_keys.append(item.key)

        if not items:
            try:
                self.query_one("#reports-detail-body", Static).update(
                    Text("aucun rapport collecté", style=f"italic {CASYS_FAINT}")
                )
            except Exception:
                pass
            return

        # Détail : item sous le curseur si valide, sinon premier item.
        key: str | None = None
        try:
            candidate = self._ordered_keys[table.cursor_row]
            if candidate in self._row_map:
                key = candidate
        except Exception:
            key = None
        if key is None:
            key = items[0].key
            try:
                table.move_cursor(row=self._ordered_keys.index(key), animate=False)
            except Exception:
                pass
        self._render_detail(key)

    def _render_detail(self, row_key: str | None) -> None:
        item = self._row_map.get(str(row_key or ""))
        try:
            self.query_one("#reports-detail-body", Static).update(
                build_report_detail(item, now=datetime.now(UTC))
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Événements
    # ------------------------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = str(event.row_key.value or "") if event.row_key is not None else ""
        if key and key not in self._row_map:
            return  # header de section / row décorative — détail inchangé
        self._render_detail(key or None)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = str(event.row_key.value or "") if event.row_key is not None else ""
        if key and key not in self._row_map:
            return
        self._render_detail(key or None)

    def on_key(self, event: events.Key) -> None:
        """j/k : curseur dans la liste (convention universe)."""

        if event.key == "j":
            try:
                self.query_one("#reports-list", DataTable).action_cursor_down()
            except Exception:
                pass
            event.prevent_default()
        elif event.key == "k":
            try:
                self.query_one("#reports-list", DataTable).action_cursor_up()
            except Exception:
                pass
            event.prevent_default()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_refresh_gallery(self) -> None:
        """r — re-scan manuel des artifacts."""
        self._start_scan(show_loading=True)
