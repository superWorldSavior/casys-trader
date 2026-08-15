"""logs — page 6 : tail live de state/events.jsonl + trace agent.

Grille 1.4fr | 1fr : EVENTS (chips de filtre par classe, regex, follow) et
AGENT TRACE (state/agent_trace.log). Les chips REMPLACENT les toggles
invisibles : l'état des filtres est toujours affiché.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import RichLog, Static

from trader.interfaces.cockpit.events import (
    EventClass,
    EventLine,
    format_event_line,
    read_new_lines,
)
from trader.interfaces.cockpit.pages._shared import PANEL_CSS
from trader.interfaces.ui.palette import (
    CASYS_ACCENT,
    CASYS_DIM,
    CASYS_ERROR,
    CASYS_FAINT,
    CASYS_MUTED,
    CASYS_SUCCESS,
    CASYS_WARNING,
    PALETTE_CASYS,
    Palette,
)

logger = logging.getLogger(__name__)

_MAX_EVENT_LINES = 500

_EVENT_STYLES: dict[EventClass, str] = {
    EventClass.DECISION_EXECUTED: PALETTE_CASYS["event_decision_exec"],
    EventClass.RISK_REJECT: PALETTE_CASYS["event_risk_reject"],
    EventClass.STALE: PALETTE_CASYS["event_stale"],
    EventClass.HOLD: PALETTE_CASYS["event_hold"],
    EventClass.WATCH: PALETTE_CASYS["event_watch"],
    EventClass.LEARNING: PALETTE_CASYS["event_learning"],
    EventClass.CYCLE: PALETTE_CASYS["event_cycle"],
    EventClass.ERROR: PALETTE_CASYS["event_error"],
    EventClass.OTHER: PALETTE_CASYS["event_other"],
}

# Chips affichées, dans l'ordre du design : label court + classe + couleur.
_CHIPS: tuple[tuple[str, EventClass, str], ...] = (
    ("fills", EventClass.DECISION_EXECUTED, CASYS_SUCCESS),
    ("risk", EventClass.RISK_REJECT, CASYS_ERROR),
    ("watches", EventClass.WATCH, CASYS_ACCENT),
    ("stale", EventClass.STALE, CASYS_WARNING),
    ("learnings", EventClass.LEARNING, CASYS_MUTED),
    ("holds", EventClass.HOLD, CASYS_MUTED),
    ("cycles", EventClass.CYCLE, CASYS_FAINT),
)


def build_filter_chips(
    *,
    class_filter: "set[EventClass] | None",
    show_cycles: bool,
    regex_text: str | None,
    follow: bool,
) -> Text:
    """Ligne d'état des filtres : `fills ✓ · … · cycles ✗ · / regex — · follow ✓`."""
    text = Text()
    for index, (label, event_class, color) in enumerate(_CHIPS):
        if index:
            text.append(" · ", style=CASYS_FAINT)
        if event_class is EventClass.CYCLE:
            shown = show_cycles and (class_filter is None or event_class in class_filter)
        else:
            shown = class_filter is None or event_class in class_filter
        text.append(label, style=color if shown else CASYS_FAINT)
        text.append(" ✓" if shown else " ✗", style=CASYS_DIM if shown else CASYS_FAINT)
    text.append("   / ", style=f"bold {CASYS_ACCENT}")
    text.append(f"regex {regex_text}" if regex_text else "regex —", style=CASYS_DIM)
    text.append(" · follow ", style=CASYS_DIM)
    text.append("✓" if follow else "✗", style=CASYS_SUCCESS if follow else CASYS_WARNING)
    return text


class LogsPane(Static):
    """Tail live d'events.jsonl : chips d'état, filtres classe/regex, follow.

    Cycles masqués par défaut (design) — `c` les réaffiche.
    """

    log_widget_id: str = "events-log"

    _offset: int = 0
    _show_cycles: bool = False
    _auto_scroll: bool = True
    _last_file_status: str = "ok"
    _backlog_loaded: bool = False
    _class_filter: "set[EventClass] | None" = None
    _regex_text: str | None = None
    _regex: "re.Pattern[str] | None" = None
    _current_palette: Palette = PALETTE_CASYS  # compat interface app

    DEFAULT_CSS = """
    LogsPane {
        height: 100%;
        layout: vertical;
    }
    LogsPane .logs-chips { height: 1; padding: 0 1; }
    LogsPane RichLog { height: 1fr; }
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._buffer: deque[EventLine] = deque(maxlen=_MAX_EVENT_LINES)

    def compose(self) -> ComposeResult:
        yield Static(id=f"{self.log_widget_id}-chips", classes="logs-chips")
        yield RichLog(
            id=self.log_widget_id,
            highlight=False,
            markup=False,
            max_lines=_MAX_EVENT_LINES,
        )

    def on_mount(self) -> None:
        self._render_chips()
        self.call_after_refresh(self._load_initial_backlog)

    def _render_chips(self) -> None:
        try:
            chips = self.query_one(f"#{self.log_widget_id}-chips", Static)
        except Exception:
            return
        chips.update(
            build_filter_chips(
                class_filter=self._class_filter,
                show_cycles=self._show_cycles,
                regex_text=self._regex_text,
                follow=self._auto_scroll,
            )
        )

    def _load_initial_backlog(self) -> None:
        if self._backlog_loaded:
            return
        self._backlog_loaded = True
        events_path = getattr(self.app, "_events_file", None)
        if events_path is not None:
            self.poll_events(events_path)

    def _passes(self, line: EventLine) -> bool:
        if line.markup_class == EventClass.CYCLE and not self._show_cycles:
            return False
        if self._class_filter is not None and line.markup_class not in self._class_filter:
            return False
        if self._regex is not None and not self._regex.search(line.text):
            return False
        return True

    def _write_line(self, log: RichLog, line: EventLine) -> None:
        log.write(Text(line.text, style=_EVENT_STYLES.get(line.markup_class, "")))

    def set_filters(self, classes: "set[EventClass] | None", regex_text: str | None) -> None:
        """Applique les filtres et re-rend tout le buffer. Regex invalide → ignorée."""
        self._class_filter = classes
        self._regex_text = regex_text or None
        if regex_text:
            try:
                self._regex = re.compile(regex_text)
            except re.error:
                self._regex = None
        else:
            self._regex = None
        log: RichLog = self.query_one(f"#{self.log_widget_id}", RichLog)
        log.clear()
        for line in self._buffer:
            if self._passes(line):
                self._write_line(log, line)
        if self._auto_scroll:
            log.scroll_end(animate=False)
        self._render_chips()

    def toggle_cycles(self) -> None:
        self._show_cycles = not self._show_cycles
        self.set_filters(self._class_filter, self._regex_text)

    def toggle_scroll(self) -> None:
        self._auto_scroll = not self._auto_scroll
        self._render_chips()

    def poll_events(self, events_path: Path) -> None:
        """Lit les nouvelles lignes et les ajoute au RichLog."""
        log: RichLog = self.query_one(f"#{self.log_widget_id}", RichLog)

        if not events_path.exists():
            if self._last_file_status != "absent":
                self._last_file_status = "absent"
                log.write(Text(f"[events] waiting for {events_path.name}…", style=CASYS_FAINT))
            return

        if self._last_file_status == "absent":
            self._last_file_status = "ok"
            log.write(Text(f"[events] {events_path.name} available", style=CASYS_FAINT))

        new_dicts, new_offset = read_new_lines(events_path, self._offset)
        self._offset = new_offset
        if not new_dicts:
            return

        for ev_dict in new_dicts:
            ev_line = format_event_line(ev_dict)
            self._buffer.append(ev_line)
            if self._passes(ev_line):
                self._write_line(log, ev_line)

        if self._auto_scroll:
            log.scroll_end(animate=False)


def _read_new_text_lines(path: Path, offset: int) -> tuple[list[str], int]:
    try:
        if not path.exists():
            return [], 0
        size = path.stat().st_size
        if size == 0:
            return [], 0
        _CHUNK_SIZE = 1 * 1024 * 1024
        is_init = (offset == 0) or (size < offset)
        if is_init:
            tail_start = max(0, size - _CHUNK_SIZE)
        else:
            tail_start = offset
        with path.open("rb") as fh:
            fh.seek(tail_start)
            raw = fh.read(_CHUNK_SIZE)
        if is_init and tail_start > 0:
            first_nl = raw.find(b"\n")
            if first_nl == -1:
                return [], tail_start + len(raw)
            raw = raw[first_nl + 1 :]
            read_offset = tail_start + first_nl + 1
        else:
            read_offset = tail_start
        last_newline = raw.rfind(b"\n")
        if last_newline == -1:
            return [], read_offset
        complete_raw = raw[: last_newline + 1]
        new_offset = read_offset + len(complete_raw)
        lines = [
            raw_line.decode("utf-8", errors="replace").strip()
            for raw_line in complete_raw.split(b"\n")
            if raw_line.strip()
        ]
        if is_init and len(lines) > _MAX_EVENT_LINES:
            lines = lines[-_MAX_EVENT_LINES:]
        return lines, new_offset
    except Exception:
        return [], 0


class AgentTracePane(Static):
    """Trace live des appels/outcomes agent (state/agent_trace.log)."""

    log_widget_id: str = "agent-trace-log"

    _offset: int = 0
    _auto_scroll: bool = True
    _last_file_status: str = "ok"
    _backlog_loaded: bool = False
    _current_palette: Palette = PALETTE_CASYS  # compat interface app

    DEFAULT_CSS = """
    AgentTracePane {
        height: 100%;
        layout: vertical;
    }
    AgentTracePane .logs-chips { height: 1; padding: 0 1; }
    AgentTracePane RichLog { height: 1fr; }
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._buffer: deque[str] = deque(maxlen=_MAX_EVENT_LINES)

    def compose(self) -> ComposeResult:
        yield Static(
            Text("per decision — tools · outcomes · risk gate", style=CASYS_FAINT),
            id=f"{self.log_widget_id}-chips",
            classes="logs-chips",
        )
        yield RichLog(
            id=self.log_widget_id,
            highlight=False,
            markup=False,
            max_lines=_MAX_EVENT_LINES,
        )

    def on_mount(self) -> None:
        self.call_after_refresh(self._load_initial_backlog)

    def _load_initial_backlog(self) -> None:
        if self._backlog_loaded:
            return
        self._backlog_loaded = True
        trace_path = getattr(self.app, "_agent_trace_file", None)
        if trace_path is not None:
            self.poll_trace(trace_path)

    def _write_line(self, log: RichLog, line: str) -> None:
        indented = line.startswith((" ", "\t", "└", "─"))
        style = CASYS_DIM if indented else CASYS_MUTED
        if line.startswith("[agent]"):
            style = CASYS_ACCENT
        log.write(Text(line, style=style))

    def poll_trace(self, trace_path: Path) -> None:
        log: RichLog = self.query_one(f"#{self.log_widget_id}", RichLog)

        if not trace_path.exists():
            if self._last_file_status != "absent":
                self._last_file_status = "absent"
                log.write(Text(f"[agent] waiting for {trace_path.name}…", style=CASYS_FAINT))
            return

        if self._last_file_status == "absent":
            self._last_file_status = "ok"
            log.write(Text(f"[agent] {trace_path.name} available", style=CASYS_FAINT))

        lines, new_offset = _read_new_text_lines(trace_path, self._offset)
        self._offset = new_offset
        if not lines:
            return

        for line in lines:
            self._buffer.append(line)
            self._write_line(log, line)

        if self._auto_scroll:
            log.scroll_end(animate=False)

    def toggle_scroll(self) -> None:
        self._auto_scroll = not self._auto_scroll


class LogsPage(Static):
    """Page 6 — EVENTS | AGENT TRACE."""

    DEFAULT_CSS = (
        PANEL_CSS
        + """
    LogsPage {
        layout: horizontal;
        height: 100%;
        padding: 1 2 0 2;
    }
    LogsPage #events-panel {
        width: 7fr;
        height: 100%;
        margin-right: 1;
    }
    LogsPage #agent-trace-panel {
        width: 5fr;
        height: 100%;
    }
    """
    )

    def compose(self) -> ComposeResult:
        logs = LogsPane(id="events-panel", classes="casys-panel")
        logs.border_title = "EVENTS — state/events.jsonl"
        yield logs
        trace = AgentTracePane(id="agent-trace-panel", classes="casys-panel")
        trace.border_title = "AGENT TRACE — state/agent_trace.log"
        yield trace

    def update_state(self, state: dict) -> None:  # noqa: ARG002 — poll séparé (1s)
        """Les logs se rafraîchissent via poll_events/poll_trace, pas via l'état."""

    def poll(self, events_path: Path, trace_path: Path) -> None:
        try:
            self.query_one("#events-panel", LogsPane).poll_events(events_path)
        except Exception:
            logger.debug("%s update error", "logs events", exc_info=True)
        try:
            self.query_one("#agent-trace-panel", AgentTracePane).poll_trace(trace_path)
        except Exception:
            logger.debug("%s update error", "logs agent trace", exc_info=True)
