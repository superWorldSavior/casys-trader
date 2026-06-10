# Cockpit Console Unifié v1 — Plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Créer `trader/cockpit.py` — app Textual combinant TOUT le contenu du dashboard TUI existant (KPIs, courbe d'équité, positions, attribution, décisions, learnings, header) PLUS un flux de logs live `events.jsonl`, sans rien perdre de tui.py.

**Architecture:** Le cockpit réutilise les fonctions `_build_*` de `tui.py` directement dans des widgets `Static` Textual (qui acceptent nativement des Rich Renderables via `Static.update(renderable)`). Aucun panneau n'est réécrit. Un module séparé `trader/cockpit_events.py` gère la logique pure du tail incrémental (parsing, classification, formatage) — testable sans UI. Layout : zone supérieure = header 1 ligne d'état enrichi ; zone principale = 2 colonnes : gauche (~60%) contient tout le dashboard TUI, droite (~40%) contient le panneau logs live.

**Tech Stack:** Python 3.11+, `textual>=0.80` (à ajouter via `uv add textual`), `rich>=13.0` (déjà présent), `pytest>=8.0` avec `App.run_test()` / Pilot pour les tests de fumée.

---

## Cartographie des fichiers

| Fichier | Rôle | Action |
|---|---|---|
| `trader/cockpit_events.py` | Logique pure : parsing/formatage ligne event, tail incrémental | Créer |
| `trader/cockpit.py` | App Textual : layout, widgets, polling, raccourcis clavier | Créer |
| `tests/test_cockpit_events.py` | Tests unitaires de la logique pure (parsing, tail, formatage) | Créer |
| `tests/test_cockpit_smoke.py` | Tests de fumée Textual Pilot (montage app, panes, touche `q`) | Créer |
| `pyproject.toml` | Ajout dépendance `textual>=0.80` | Modifier |
| `Makefile` | Ajout cible `cockpit` | Modifier |

Fichiers intouchables : `trader/daemon.py`, `trader/consolidator.py`, `tests/test_consolidator.py`, `tests/test_cli_semantic.py`, `tests/test_daemon_learnings.py`, `tests/test_llm.py`, `README.md`, tout autre `docs/` (sauf ce plan).

---

## Task 1 : Ajouter la dépendance `textual` et vérifier la suite existante

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Installer textual via uv**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader
uv add textual
```

Expected output : ligne `textual>=...` ajoutée dans `pyproject.toml` sous `dependencies`, `uv.lock` mis à jour.

- [ ] **Step 2: Vérifier que textual s'importe**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader
uv run python -c "import textual; print(textual.__version__)"
```

Expected : un numéro de version s'affiche (≥ 0.80 typiquement).

- [ ] **Step 3: Vérifier que la suite existante reste verte**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader
uv run pytest -q --ignore=tests/test_cockpit_events.py --ignore=tests/test_cockpit_smoke.py
```

Expected : tous les tests passent. Si une régression apparaît ici, stopper et diagnostiquer avant de continuer.

---

## Task 2 : Module `trader/cockpit_events.py` — logique pure (TDD)

**Files:**
- Create: `trader/cockpit_events.py`
- Create: `tests/test_cockpit_events.py`

Ce module contient **uniquement** des fonctions pures : pas d'I/O dans les tests. L'I/O (lecture fichier) est encapsulée dans une seule fonction `read_new_lines`.

- [ ] **Step 1 : Écrire les tests qui échouent — `tests/test_cockpit_events.py`**

```python
"""Tests unitaires de trader.cockpit_events — logique pure, zéro I/O externe."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from trader.cockpit_events import (
    EventLine,
    format_event_line,
    classify_event,
    EventClass,
    read_new_lines,
)


# ---------------------------------------------------------------------------
# classify_event
# ---------------------------------------------------------------------------


def test_classify_cycle_started():
    ev = {"event": "cycle_started", "ts": "2026-06-10T10:00:00+00:00"}
    assert classify_event(ev) == EventClass.CYCLE


def test_classify_cycle_completed():
    ev = {"event": "cycle_completed", "decisions_done": 5}
    assert classify_event(ev) == EventClass.CYCLE


def test_classify_decision_executed():
    ev = {
        "event": "decision_recorded",
        "action": "BUY",
        "executed": True,
        "reason": "ok",
    }
    assert classify_event(ev) == EventClass.DECISION_EXECUTED


def test_classify_decision_rejected_risk():
    ev = {
        "event": "decision_recorded",
        "action": "BUY",
        "executed": False,
        "reason": "risk:order_value_exceeded",
    }
    assert classify_event(ev) == EventClass.RISK_REJECT


def test_classify_decision_hold():
    ev = {
        "event": "decision_recorded",
        "action": "HOLD",
        "executed": False,
        "reason": "hold",
    }
    assert classify_event(ev) == EventClass.HOLD


def test_classify_stale():
    ev = {
        "event": "decision_recorded",
        "action": "HOLD",
        "executed": False,
        "reason": "stale_market_data",
    }
    assert classify_event(ev) == EventClass.STALE


def test_classify_watch_triggered():
    ev = {"event": "indicator_watch_triggered", "symbol": "AAPL"}
    assert classify_event(ev) == EventClass.WATCH


def test_classify_learning_consolidated():
    ev = {"event": "learning_consolidated", "triggered": True}
    assert classify_event(ev) == EventClass.LEARNING


def test_classify_unknown_falls_back_to_other():
    ev = {"event": "some_future_event_type"}
    assert classify_event(ev) == EventClass.OTHER


# ---------------------------------------------------------------------------
# format_event_line
# ---------------------------------------------------------------------------


def test_format_event_line_decision_executed():
    ev = {
        "ts": "2026-06-10T10:01:02+00:00",
        "event": "decision_recorded",
        "symbol": "AAPL",
        "action": "BUY",
        "executed": True,
        "reason": "ok",
    }
    line = format_event_line(ev)
    assert isinstance(line, EventLine)
    assert "AAPL" in line.text
    assert "BUY" in line.text
    assert "10:01" in line.text
    assert line.markup_class == EventClass.DECISION_EXECUTED


def test_format_event_line_cycle_started():
    ev = {
        "ts": "2026-06-10T09:00:00+00:00",
        "event": "cycle_started",
        "symbols_due": ["SPY", "QQQ"],
        "dry_run": True,
    }
    line = format_event_line(ev)
    assert "cycle" in line.text.lower()
    assert line.markup_class == EventClass.CYCLE


def test_format_event_line_risk_reject():
    ev = {
        "ts": "2026-06-10T10:02:00+00:00",
        "event": "decision_recorded",
        "symbol": "NZDUSD=X",
        "action": "BUY",
        "executed": False,
        "reason": "risk:order_value_exceeded",
    }
    line = format_event_line(ev)
    assert "NZDUSD=X" in line.text
    assert line.markup_class == EventClass.RISK_REJECT


def test_format_event_line_watch():
    ev = {
        "ts": "2026-06-10T10:05:00+00:00",
        "event": "indicator_watch_triggered",
        "symbol": "NG=F",
        "on_trigger": "WAKE",
    }
    line = format_event_line(ev)
    assert "NG=F" in line.text
    assert line.markup_class == EventClass.WATCH


def test_format_event_line_unknown_json_survives():
    """Une ligne JSON valide mais avec un event inconnu ne lève pas."""
    ev = {"ts": "2026-06-10T10:06:00+00:00", "event": "mystery_event", "foo": "bar"}
    line = format_event_line(ev)
    assert isinstance(line, EventLine)


# ---------------------------------------------------------------------------
# read_new_lines — tail incrémental
# ---------------------------------------------------------------------------


def test_read_new_lines_fichier_absent_retourne_vide():
    """Fichier absent → liste vide, offset 0, pas de crash."""
    lines, new_offset = read_new_lines(Path("/tmp/fichier_inexistant_cockpit.jsonl"), offset=0)
    assert lines == []
    assert new_offset == 0


def test_read_new_lines_lit_tout_au_premier_appel(tmp_path):
    f = tmp_path / "events.jsonl"
    f.write_text(
        '{"event":"cycle_started","ts":"2026-06-10T01:00:00+00:00"}\n'
        '{"event":"cycle_completed","ts":"2026-06-10T01:00:05+00:00"}\n',
        encoding="utf-8",
    )
    lines, offset = read_new_lines(f, offset=0)
    assert len(lines) == 2
    assert offset == f.stat().st_size


def test_read_new_lines_incremental_ne_rend_que_les_nouvelles(tmp_path):
    f = tmp_path / "events.jsonl"
    f.write_text(
        '{"event":"cycle_started","ts":"2026-06-10T01:00:00+00:00"}\n',
        encoding="utf-8",
    )
    lines1, off1 = read_new_lines(f, offset=0)
    assert len(lines1) == 1

    # Ajout d'une nouvelle ligne
    with f.open("a", encoding="utf-8") as fh:
        fh.write('{"event":"cycle_completed","ts":"2026-06-10T01:00:05+00:00"}\n')

    lines2, off2 = read_new_lines(f, offset=off1)
    assert len(lines2) == 1
    assert off2 > off1


def test_read_new_lines_fichier_tronque_resynchro(tmp_path):
    """Si le fichier est plus court que l'offset (rotation), recommence depuis 0."""
    f = tmp_path / "events.jsonl"
    f.write_text(
        '{"event":"cycle_started","ts":"2026-06-10T01:00:00+00:00"}\n'
        '{"event":"cycle_completed","ts":"2026-06-10T01:00:05+00:00"}\n',
        encoding="utf-8",
    )
    _, big_offset = read_new_lines(f, offset=0)
    assert big_offset > 0

    # Simule une troncature : on réécrit un fichier plus court
    f.write_text(
        '{"event":"cycle_started","ts":"2026-06-10T02:00:00+00:00"}\n',
        encoding="utf-8",
    )
    lines, new_offset = read_new_lines(f, offset=big_offset)
    # Doit avoir re-lu depuis le début
    assert len(lines) == 1
    assert new_offset == f.stat().st_size


def test_read_new_lines_ligne_json_cassee_est_ignoree(tmp_path):
    """Une ligne JSON invalide ne plante pas et est simplement ignorée."""
    f = tmp_path / "events.jsonl"
    f.write_text(
        '{"event":"cycle_started","ts":"2026-06-10T01:00:00+00:00"}\n'
        'JSON INVALIDE ICI !!!\n'
        '{"event":"cycle_completed","ts":"2026-06-10T01:00:05+00:00"}\n',
        encoding="utf-8",
    )
    lines, _ = read_new_lines(f, offset=0)
    # Seules les 2 lignes valides sont retournées
    assert len(lines) == 2


def test_read_new_lines_fichier_vide_retourne_vide(tmp_path):
    f = tmp_path / "events.jsonl"
    f.write_text("", encoding="utf-8")
    lines, offset = read_new_lines(f, offset=0)
    assert lines == []
    assert offset == 0
```

- [ ] **Step 2 : Vérifier que les tests échouent (module absent)**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader
uv run pytest tests/test_cockpit_events.py -q 2>&1 | head -20
```

Expected : `ModuleNotFoundError` ou `ImportError` — le module n'existe pas encore.

- [ ] **Step 3 : Créer `trader/cockpit_events.py`**

```python
"""cockpit_events — logique pure pour le panneau de logs live du cockpit.

Parsing, classification et formatage des lignes d'events.jsonl.
Séparé de l'app Textual pour pouvoir être testé sans UI.

Ne lit RIEN dans state/ sauf via read_new_lines (I/O isolée).
N'écrit RIEN.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any


class EventClass(Enum):
    """Classification sémantique d'un événement daemon."""
    CYCLE = auto()              # cycle_started / cycle_completed
    DECISION_EXECUTED = auto()  # décision exécutée (BUY/SELL effectif)
    RISK_REJECT = auto()        # rejet risk:*
    HOLD = auto()               # décision HOLD normale
    STALE = auto()              # stale_market_data / stale_backoff
    WATCH = auto()              # indicator_watch_triggered
    LEARNING = auto()           # learning_consolidated
    ERROR = auto()              # erreur explicite dans l'event
    OTHER = auto()              # tout le reste


@dataclass(frozen=True)
class EventLine:
    """Résultat du formatage d'une ligne d'event."""
    text: str                  # texte human-readable (pas de markup Rich)
    markup_class: EventClass   # pour le coloriage dans le widget


def classify_event(event: dict[str, Any]) -> EventClass:
    """Détermine la classe sémantique d'un event dict.

    Règles, par ordre de priorité :
    1. event == "cycle_started" / "cycle_completed"  → CYCLE
    2. event == "indicator_watch_triggered"          → WATCH
    3. event == "learning_consolidated"              → LEARNING
    4. event == "decision_recorded" :
       - reason.startswith("risk:")                 → RISK_REJECT
       - reason.startswith("stale")                 → STALE
       - executed == True                           → DECISION_EXECUTED
       - sinon                                      → HOLD
    5. "error" dans event_type ou event.get("error")→ ERROR
    6. sinon                                        → OTHER
    """
    event_type = str(event.get("event", ""))
    reason = str(event.get("reason", ""))
    executed = event.get("executed", False)

    if event_type in ("cycle_started", "cycle_completed"):
        return EventClass.CYCLE
    if event_type == "indicator_watch_triggered":
        return EventClass.WATCH
    if event_type == "learning_consolidated":
        return EventClass.LEARNING
    if event_type == "decision_recorded":
        if reason.startswith("risk:"):
            return EventClass.RISK_REJECT
        if reason.startswith("stale"):
            return EventClass.STALE
        if executed:
            return EventClass.DECISION_EXECUTED
        return EventClass.HOLD
    if "error" in event_type or event.get("error"):
        return EventClass.ERROR
    return EventClass.OTHER


def _fmt_ts(ts: str) -> str:
    """Extrait HH:MM:SS depuis un timestamp ISO, retourne '?' en cas d'erreur."""
    if not ts:
        return "?"
    try:
        return ts[11:19]  # HH:MM:SS
    except Exception:
        return ts[:8]


def format_event_line(event: dict[str, Any]) -> EventLine:
    """Transforme un dict d'event en EventLine human-readable.

    Ne lève jamais d'exception.
    """
    try:
        return _format_event_line_inner(event)
    except Exception:
        raw = str(event)
        return EventLine(text=f"[?] {raw[:120]}", markup_class=EventClass.OTHER)


def _format_event_line_inner(event: dict[str, Any]) -> EventLine:
    event_type = str(event.get("event", "?"))
    ts = _fmt_ts(str(event.get("ts", "")))
    cls = classify_event(event)

    if event_type == "cycle_started":
        symbols = event.get("symbols_due", [])
        n = len(symbols) if isinstance(symbols, list) else "?"
        dry = " [dry]" if event.get("dry_run") else ""
        text = f"{ts} ▶ cycle démarré{dry} — {n} symboles"

    elif event_type == "cycle_completed":
        done = event.get("decisions_done", "?")
        calls = event.get("model_calls_used", "?")
        text = f"{ts} ■ cycle terminé — {done} décisions, {calls} appels LLM"

    elif event_type == "decision_recorded":
        symbol = str(event.get("symbol", "?"))
        action = str(event.get("action", "?"))
        reason = str(event.get("reason", ""))
        executed = event.get("executed", False)
        exec_flag = " ✓" if executed else ""
        reason_str = f" [{reason}]" if reason and reason not in ("ok", "hold") else ""
        text = f"{ts} {action} {symbol}{exec_flag}{reason_str}"

    elif event_type == "indicator_watch_triggered":
        symbol = str(event.get("symbol", "?"))
        trigger = str(event.get("on_trigger", ""))
        text = f"{ts} watch — {symbol} ({trigger})"

    elif event_type == "learning_consolidated":
        n = event.get("new_raw_count", "?")
        written = event.get("written", False)
        written_str = " → écrit" if written else ""
        text = f"{ts} learnings consolidés — {n} raws{written_str}"

    else:
        extra_keys = [k for k in event if k not in ("ts", "event")][:3]
        parts = [f"{k}={event[k]!r}" for k in extra_keys]
        text = f"{ts} [{event_type}] {', '.join(parts)}"

    return EventLine(text=text, markup_class=cls)


def read_new_lines(path: Path, offset: int) -> tuple[list[dict[str, Any]], int]:
    """Lit les nouvelles lignes JSON depuis `path` en commençant à `offset` bytes.

    Retourne (lignes_dict_valides, nouvel_offset).
    Si le fichier est absent : ([], 0).
    Si le fichier est plus court que offset (troncature) : relit depuis 0.
    Les lignes JSON invalides sont ignorées silencieusement.

    Ne lève jamais d'exception.
    """
    try:
        if not path.exists():
            return [], 0

        size = path.stat().st_size
        if size == 0:
            return [], 0

        # Troncature détectée → resync depuis le début
        read_offset = 0 if size < offset else offset

        with path.open("rb") as fh:
            fh.seek(read_offset)
            raw = fh.read()

        new_offset = read_offset + len(raw)

        results: list[dict[str, Any]] = []
        for raw_line in raw.split(b"\n"):
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    results.append(obj)
            except Exception:
                pass  # ligne invalide ignorée

        return results, new_offset

    except Exception:
        return [], 0
```

- [ ] **Step 4 : Vérifier que les tests passent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader
uv run pytest tests/test_cockpit_events.py -v
```

Expected : tous verts. Si un test échoue, corriger `cockpit_events.py` — ne pas modifier les tests.

---

## Task 3 : App Textual `trader/cockpit.py` — parité complète + logs live

**Files:**
- Create: `trader/cockpit.py`

**Principe de parité :** Les fonctions `_build_*` de `tui.py` retournent des `RenderableType` Rich. Textual accepte ces renderables directement dans `Static.update(renderable)`. On importe ces fonctions telles quelles — aucune logique de dashboard n'est réécrite.

**Layout :**
- En-tête Textual (`Header`) + ligne de statut enrichie (`CockpitStatus`)
- Zone principale : 2 colonnes
  - Gauche (~60%) : `DashboardPane` — contient tout ce que `build_view` affichait (KPIs, équité, positions, attribution, décisions, learnings)
  - Droite (~40%) : `EventsPane` — tail live events.jsonl

- [ ] **Step 1 : Créer `trader/cockpit.py`**

```python
"""cockpit — Cockpit console unifié v1 (Textual).

Fusion COMPLÈTE du dashboard TUI (KPIs, courbe d'équité, positions, attribution,
décisions, learnings, header daemon) et d'un flux de logs live (events.jsonl),
en une seule app 100 % console.

Parité garantie avec tui.py : toutes les fonctions _build_* sont importées et
réutilisées via Static.update(renderable) — aucune logique n'est dupliquée.

Usage :
    uv run python -m trader.cockpit
    make cockpit

Raccourcis :
    q         Quitter
    c         Toggle l'affichage des events cycle_started/cycle_completed
    f         Pause/reprise de l'auto-scroll du panneau logs
    l         Toggle visibilité du panneau logs (plein écran dashboard)
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, RichLog, Static

from trader.cockpit_events import (
    EventClass,
    EventLine,
    format_event_line,
    read_new_lines,
)
from trader.tui import (
    _build_attribution_panel,
    _build_decisions_table,
    _build_equity_panel,
    _build_kpi_band,
    _build_learnings_panel,
    _build_positions_panel,
    _safe_float,
    _safe_list_of_dicts,
    load_runtime_state,
)

# ---------------------------------------------------------------------------
# Chemins
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
_STATE_DIR = _ROOT / "state"
_EVENTS_FILE = _STATE_DIR / "events.jsonl"
_KILL_FILE = _ROOT / "KILL"

# Buffer maximum pour le panneau events
_MAX_EVENT_LINES = 500

# ---------------------------------------------------------------------------
# Couleurs par EventClass (Rich markup style)
# ---------------------------------------------------------------------------
_EVENT_STYLES: dict[EventClass, str] = {
    EventClass.DECISION_EXECUTED: "bold green",
    EventClass.RISK_REJECT:       "bold red",
    EventClass.STALE:             "dim",
    EventClass.HOLD:              "dim white",
    EventClass.WATCH:             "bold cyan",
    EventClass.LEARNING:          "blue",
    EventClass.CYCLE:             "dim grey50",
    EventClass.ERROR:             "bold yellow",
    EventClass.OTHER:             "dim",
}


# ---------------------------------------------------------------------------
# Widget : barre de statut 1 ligne (en plus du Header Textual)
# ---------------------------------------------------------------------------


class CockpitStatus(Static):
    """Ligne d'état compacte : équité, cash, P&L, phase daemon, horloge UTC, kill."""

    DEFAULT_CSS = """
    CockpitStatus {
        height: 2;
        background: $panel;
        border-bottom: solid $primary;
        padding: 0 1;
    }
    """

    def update_state(self, state: dict, kill_active: bool) -> None:
        portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
        kpis = state.get("kpis") if isinstance(state.get("kpis"), dict) else {}
        daemon_status = state.get("daemon_status") if isinstance(state.get("daemon_status"), dict) else {}

        equity = _safe_float(portfolio.get("equity") or kpis.get("equity"), default=0.0) or 0.0
        cash = _safe_float(portfolio.get("cash") or kpis.get("cash"), default=0.0) or 0.0
        starting_cash = _safe_float(state.get("starting_cash"), default=cash) or cash
        pnl = equity - starting_cash
        ret_pct = _safe_float(portfolio.get("total_return_pct"), default=0.0) or 0.0
        phase = str(daemon_status.get("phase", "—"))
        current_symbol = str(daemon_status.get("current_symbol") or "—")
        calls_used = daemon_status.get("model_calls_used")
        calls_max = daemon_status.get("max_model_calls_per_cycle")
        calls_str = f"{calls_used}/{calls_max}" if calls_used is not None and calls_max is not None else "—"
        now_utc = datetime.now(UTC).strftime("%H:%M:%S UTC")
        dry_run = state.get("dry_run", True)

        mode_str = "[bold red]LIVE[/bold red]" if not dry_run else "[bold yellow]DRY-RUN[/bold yellow]"
        kill_str = "[bold red on white] KILL ACTIF [/bold red on white]" if kill_active else "[green]nominal[/green]"
        ret_style = "green" if ret_pct >= 0 else "red"
        pnl_style = "green" if pnl >= 0 else "red"

        text = (
            f"  Équité [bold cyan]${equity:,.2f}[/bold cyan]"
            f"  Cash [cyan]${cash:,.2f}[/cyan]"
            f"  P&L [{ret_style}]{ret_pct:+.2f}%[/{ret_style}]"
            f" [{pnl_style}]({pnl:+,.2f})[/{pnl_style}]"
            f"  Daemon [magenta]{phase}[/magenta]"
            f"  Symbole [cyan]{current_symbol}[/cyan]"
            f"  Appels [cyan]{calls_str}[/cyan]"
            f"  {now_utc}"
            f"  Mode {mode_str}"
            f"  Kill {kill_str}"
        )
        self.update(Text.from_markup(text))


# ---------------------------------------------------------------------------
# Widget : panneau gauche — tout le dashboard TUI
# ---------------------------------------------------------------------------


class DashboardPane(Static):
    """Panneau gauche : reprend intégralement le contenu de tui.build_view.

    Utilise les fonctions _build_* importées de tui.py via Static.update().
    """

    DEFAULT_CSS = """
    DashboardPane {
        width: 60%;
        height: 100%;
        border-right: solid $primary;
        overflow-y: auto;
    }
    DashboardPane Static {
        height: auto;
        margin: 0 0 1 0;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="kpi-band")
        yield Static(id="equity-panel")
        yield Static(id="positions-panel")
        yield Static(id="attribution-panel")
        yield Static(id="decisions-table")
        yield Static(id="learnings-panel")

    def update_state(self, state: dict) -> None:
        """Recharge tous les sous-panneaux avec le dernier état."""
        portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
        kpis = state.get("kpis") if isinstance(state.get("kpis"), dict) else {}
        attribution = state.get("attribution") if isinstance(state.get("attribution"), dict) else {}
        holdings = _safe_list_of_dicts(portfolio.get("holdings"))
        decisions = _safe_list_of_dicts(state.get("decisions"))
        equity_curve_raw = state.get("equity_curve") or []
        equity_curve = [
            v for v in (_safe_float(x, default=None) for x in equity_curve_raw)
            if v is not None
        ]
        learnings = _safe_list_of_dicts(state.get("learnings"))

        self.query_one("#kpi-band", Static).update(_build_kpi_band(kpis))
        self.query_one("#equity-panel", Static).update(_build_equity_panel(equity_curve))
        self.query_one("#positions-panel", Static).update(_build_positions_panel(holdings))
        self.query_one("#attribution-panel", Static).update(_build_attribution_panel(attribution))
        self.query_one("#decisions-table", Static).update(_build_decisions_table(decisions))

        learnings_renderable = _build_learnings_panel(learnings)
        if learnings_renderable is not None:
            self.query_one("#learnings-panel", Static).update(learnings_renderable)
        else:
            self.query_one("#learnings-panel", Static).update(Text(""))


# ---------------------------------------------------------------------------
# Widget : panneau droit — logs live
# ---------------------------------------------------------------------------


class EventsPane(Static):
    """Panneau droit : tail live d'events.jsonl avec auto-scroll et filtres."""

    _offset: int = 0
    _show_cycles: bool = True
    _auto_scroll: bool = True

    DEFAULT_CSS = """
    EventsPane {
        width: 40%;
        height: 100%;
    }
    EventsPane RichLog {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(
            "[bold]Logs live[/bold]  [dim]c:cycles  f:scroll  l:toggle pane[/dim]",
            id="logs-title",
        )
        yield RichLog(
            id="events-log",
            highlight=False,
            markup=False,
            max_lines=_MAX_EVENT_LINES,
        )

    def toggle_cycles(self) -> None:
        self._show_cycles = not self._show_cycles
        status = "visibles" if self._show_cycles else "masqués"
        log: RichLog = self.query_one("#events-log", RichLog)
        log.write(Text(f"[cycles {status}]", style="dim italic"))

    def toggle_scroll(self) -> None:
        self._auto_scroll = not self._auto_scroll
        log: RichLog = self.query_one("#events-log", RichLog)
        status = "repris" if self._auto_scroll else "pausé"
        log.write(Text(f"[scroll {status}]", style="dim italic"))

    def poll_events(self, events_path: Path) -> None:
        """Lit les nouvelles lignes depuis events_path et les ajoute au log."""
        new_dicts, new_offset = read_new_lines(events_path, self._offset)
        self._offset = new_offset

        if not new_dicts:
            return

        log: RichLog = self.query_one("#events-log", RichLog)
        for ev_dict in new_dicts:
            ev_line = format_event_line(ev_dict)
            if ev_line.markup_class == EventClass.CYCLE and not self._show_cycles:
                continue
            style = _EVENT_STYLES.get(ev_line.markup_class, "")
            log.write(Text(ev_line.text, style=style))

        if self._auto_scroll:
            log.scroll_end(animate=False)


# ---------------------------------------------------------------------------
# App principale
# ---------------------------------------------------------------------------


class CockpitApp(App):
    """Cockpit console unifié — dashboard complet + logs live."""

    TITLE = "casys-trader — cockpit"
    CSS = """
    Screen {
        layout: vertical;
    }
    #main-body {
        layout: horizontal;
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quitter"),
        Binding("c", "toggle_cycles", "Toggle cycles"),
        Binding("f", "toggle_scroll", "Pause scroll"),
        Binding("l", "toggle_logs", "Toggle logs"),
    ]

    _logs_visible: bool = True

    def compose(self) -> ComposeResult:
        yield CockpitStatus(id="cockpit-status")
        yield Static(id="main-body")
        yield Footer()

    def on_mount(self) -> None:
        body = self.query_one("#main-body", Static)
        body.mount(DashboardPane(id="dashboard-pane"))
        body.mount(EventsPane(id="events-pane"))
        # Polling état toutes les 2 s
        self.set_interval(2.0, self._refresh_state)
        # Polling events toutes les 1 s
        self.set_interval(1.0, self._poll_events)
        # Charge immédiatement
        self._refresh_state()
        self._poll_events()

    def _refresh_state(self) -> None:
        """Recharge current_report / last_report et met à jour les widgets."""
        try:
            state = load_runtime_state(state_dir=_STATE_DIR)
            kill_active = _KILL_FILE.exists()
            state["kill_switch"] = kill_active

            status: CockpitStatus = self.query_one("#cockpit-status", CockpitStatus)
            status.update_state(state, kill_active)

            dashboard: DashboardPane = self.query_one("#dashboard-pane", DashboardPane)
            dashboard.update_state(state)
        except Exception:
            pass  # tolérant — widgets restent à leur dernier état

    def _poll_events(self) -> None:
        """Lit les nouvelles lignes d'events.jsonl."""
        try:
            events_pane: EventsPane = self.query_one("#events-pane", EventsPane)
            events_pane.poll_events(_EVENTS_FILE)
        except Exception:
            pass

    def action_toggle_cycles(self) -> None:
        try:
            events_pane: EventsPane = self.query_one("#events-pane", EventsPane)
            events_pane.toggle_cycles()
        except Exception:
            pass

    def action_toggle_scroll(self) -> None:
        try:
            events_pane: EventsPane = self.query_one("#events-pane", EventsPane)
            events_pane.toggle_scroll()
        except Exception:
            pass

    def action_toggle_logs(self) -> None:
        """Affiche/masque le panneau logs pour maximiser le dashboard."""
        try:
            events_pane: EventsPane = self.query_one("#events-pane", EventsPane)
            dashboard: DashboardPane = self.query_one("#dashboard-pane", DashboardPane)
            self._logs_visible = not self._logs_visible
            if self._logs_visible:
                events_pane.display = True
                dashboard.styles.width = "60%"
            else:
                events_pane.display = False
                dashboard.styles.width = "100%"
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------


def main() -> None:
    """Lance le cockpit. Quitter avec q ou Ctrl+C."""
    app = CockpitApp()
    app.run()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2 : Vérifier que l'import fonctionne (sans lancer l'UI)**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader
uv run python -c "from trader.cockpit import CockpitApp; print('import OK')"
```

Expected : `import OK` sans erreur. Si l'import échoue avec `ImportError` sur une fonction privée de `tui.py`, vérifier que les noms correspondent exactement à ceux dans `trader/tui.py:300-442`.

---

## Task 4 : Tests de fumée Textual Pilot

**Files:**
- Create: `tests/test_cockpit_smoke.py`

Ces tests utilisent `App.run_test()` (async Textual) pour vérifier que l'app monte correctement. On injecte des `tmp_path` fixtures via monkeypatch pour isoler les fichiers d'état.

- [ ] **Step 1 : Vérifier si `pytest-asyncio` est installé**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader
uv run python -c "import pytest_asyncio; print('pytest-asyncio:', pytest_asyncio.__version__)"
```

Si absent :

```bash
uv add --dev pytest-asyncio
```

Puis ajouter dans `pyproject.toml` section `[tool.pytest.ini_options]` :

```toml
asyncio_mode = "auto"
```

- [ ] **Step 2 : Créer `tests/test_cockpit_smoke.py`**

```python
"""Tests de fumée du cockpit Textual.

Vérifient que :
- L'app monte sans crash (panes existent dans le DOM)
- La touche q quitte proprement
- Les widgets d'état gèrent un fichier absent sans crash

Note : les fichiers state sont mockés via monkeypatch sur les constantes du module.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import trader.cockpit as cockpit_module
from trader.cockpit import CockpitApp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_minimal_state(tmp_path: Path) -> None:
    """Crée un état minimal dans tmp_path pour que load_runtime_state réussisse."""
    (tmp_path / "current_report.json").write_text(
        json.dumps({
            "ts": "2026-06-10T10:00:00+00:00",
            "dry_run": True,
            "portfolio": {"cash": 100000.0, "equity": 100000.0, "holdings": []},
            "decisions": [],
        }),
        encoding="utf-8",
    )
    (tmp_path / "daemon_status.json").write_text(
        json.dumps({
            "phase": "idle",
            "decisions_done": 0,
            "symbols_total": 0,
            "model_calls_used": 0,
            "max_model_calls_per_cycle": 25,
        }),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cockpit_app_monte_avec_les_deux_panes(tmp_path, monkeypatch):
    """L'app Textual monte sans exception et les deux panneaux sont dans le DOM."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        assert app.query_one("#dashboard-pane") is not None
        assert app.query_one("#events-pane") is not None
        assert app.query_one("#cockpit-status") is not None


@pytest.mark.asyncio
async def test_cockpit_touche_q_quitte(tmp_path, monkeypatch):
    """La touche q ferme l'app proprement."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.press("q")
    # Si on sort du context manager sans TimeoutError, le test passe.


@pytest.mark.asyncio
async def test_cockpit_fichiers_absents_ne_crashent_pas(tmp_path, monkeypatch):
    """Aucun fichier d'état → l'app monte quand même (panes vides, pas de crash)."""
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        # Doit monter sans lever d'exception
        assert app.query_one("#dashboard-pane") is not None
```

- [ ] **Step 3 : Vérifier que les tests de fumée passent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader
uv run pytest tests/test_cockpit_smoke.py -v
```

Expected : 3 tests PASSED.

---

## Task 5 : Makefile — cible `cockpit`

**Files:**
- Modify: `Makefile`

- [ ] **Step 1 : Modifier la ligne `.PHONY` pour inclure `cockpit`**

Dans `Makefile`, remplacer :

```makefile
.PHONY: help tui demo daemon live once test stats attrib logs kill unkill
```

par :

```makefile
.PHONY: help tui demo daemon live once test stats attrib logs cockpit kill unkill
```

- [ ] **Step 2 : Ajouter la cible `cockpit` après `logs`**

Dans `Makefile`, après la ligne :

```makefile
logs:  ## Viewer humain des logs machine (live tail, JSONL joli) — toolong
	uvx --from toolong tl state/events.jsonl state/decisions.jsonl
```

Ajouter :

```makefile
cockpit:  ## Cockpit unifié (dashboard + logs live) — Textual
	uv run python -m trader.cockpit
```

- [ ] **Step 3 : Vérifier que `make help` affiche la nouvelle cible**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader
make help
```

Expected : une ligne `cockpit    Cockpit unifié (dashboard + logs live) — Textual` apparaît dans la liste.

---

## Task 6 : Suite finale et vérification de non-régression

- [ ] **Step 1 : Lancer toute la suite pytest**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader
uv run pytest -q
```

Expected : tous les tests (existants + nouveaux) passent. Zéro régression.

- [ ] **Step 2 : Vérifier l'import headless du cockpit**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader
uv run python -c "
from trader.cockpit import CockpitApp
print('CockpitApp importé OK')
from trader.cockpit_events import classify_event, format_event_line, read_new_lines, EventClass
print('cockpit_events importé OK')
"
```

Expected : deux lignes `OK`.

- [ ] **Step 3 : Test de fumée headless (sans TTY)**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader
timeout 3 uv run python -m trader.cockpit 2>&1 || echo "Arrêt attendu (timeout ou pas de TTY)"
```

Note : sans TTY réel, Textual peut lever ou sortir immédiatement — c'est attendu. La preuve fonctionnelle est fournie par les tests Pilot.

---

## Note sur l'asyncio et `pytest-asyncio`

Textual `App.run_test()` retourne un async context manager. Il faut un runner asyncio dans pytest :

1. `uv add --dev pytest-asyncio`
2. Dans `pyproject.toml` `[tool.pytest.ini_options]` ajouter : `asyncio_mode = "auto"`

Sans `asyncio_mode = "auto"`, chaque test async doit porter `@pytest.mark.asyncio`. Avec `asyncio_mode = "auto"`, tout test `async def` est automatiquement reconnu.

---

## Self-Review : couverture de la spec

| Exigence spec | Tâche | Statut |
|---|---|---|
| **PARITÉ COMPLÈTE avec tui.py** | | |
| KPI band (Sharpe, MaxDD, Win rate, Volatilité, Trades) | Task 3 `_build_kpi_band` importé | ✓ |
| Courbe d'équité (plotext/sparkline) | Task 3 `_build_equity_panel` importé | ✓ |
| Positions avec PnL latent et % | Task 3 `_build_positions_panel` importé | ✓ |
| Attribution (calibration confiance + raisons de sortie) | Task 3 `_build_attribution_panel` importé | ✓ |
| Dernières décisions | Task 3 `_build_decisions_table` importé | ✓ |
| Derniers apprentissages | Task 3 `_build_learnings_panel` importé | ✓ |
| Header avec phase daemon, symbole courant, appels | Task 3 `CockpitStatus.update_state` | ✓ |
| **AJOUT logs live** | | |
| Logs live events.jsonl | Task 3 `EventsPane` | ✓ |
| Couleurs par type (vert/rouge/gris/cyan) | Task 2 `EventClass` + `_EVENT_STYLES` | ✓ |
| Auto-scroll + buffer borné 500 lignes | Task 3 `RichLog(max_lines=500)` | ✓ |
| **Raccourcis** | | |
| `q` quitter | Task 3 `BINDINGS` | ✓ |
| `c` toggle cycles | Task 3 `action_toggle_cycles` | ✓ |
| `f` pause scroll | Task 3 `action_toggle_scroll` | ✓ |
| `l` toggle logs pane | Task 3 `action_toggle_logs` (enrichissement) | ✓ |
| **Infrastructure** | | |
| Polling état 2 s, events 1 s | Task 3 `set_interval` | ✓ |
| Tail incrémental (nouvelles lignes seulement) | Task 2 `read_new_lines` | ✓ |
| Fichier tronqué → resync | Task 2 `read_new_lines` | ✓ |
| Fichier absent/JSON cassé → pas de crash | Task 2 + Task 3 try/except | ✓ |
| `uv add textual` | Task 1 | ✓ |
| Makefile cible `cockpit` | Task 5 | ✓ |
| Tests TDD parsing (fonctions pures) | Task 2 `test_cockpit_events.py` | ✓ |
| Tests Pilot de fumée | Task 4 `test_cockpit_smoke.py` | ✓ |
| tui.py non modifié (import only) | Architecture | ✓ |
| Read-only strict (aucune écriture state/) | Tout le module | ✓ |
| `uv run python -m trader.cockpit` | Task 3 `main()` + `__main__` | ✓ |
