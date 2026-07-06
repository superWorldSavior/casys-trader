"""settings — page 8 « Settings » : écritures yaml explicites.

Architecture
───────────
Deux colonnes de panneaux :
  Gauche : RUNTIME (.env READ-ONLY)  ·  CYCLE & BUDGET (portfolio.yaml)
  Droite  : DATA (data_sources.yaml READ-ONLY)  ·  ROTATION (radar.yaml)
            RISK GATE (risk.yaml READ-ONLY, bordure warning #4a3f2a)

Seuls portfolio.yaml et radar.yaml sont éditables depuis cette page.
.env, data_sources.yaml et risk.yaml sont affichés en lecture seule.

Navigation : j / k (ou flèches) = déplace le curseur sur les lignes
éditables (6 au total) · enter = ouvre une saisie en bas de page ·
w = écriture atomique (tempfile.mkstemp + os.replace) · r = revert.

Écriture atomique : même répertoire que le fichier cible, os.replace.
yaml.safe_dump détruit les commentaires → un en-tête statique est
réinjecté dans chaque fichier réécrit.

Écart vs mockup : le mockup montre un toggle mode et un champ ib-port
éditables dans le panel RUNTIME, mais .env n'est jamais réécrit par le
cockpit (load_dotenv est appelée une fois au boot → tout changement
nécessite un restart). Ce panel est affiché en READ-ONLY avec une note
documentant l'écart.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, Static

from trader.interfaces.cockpit.pages._shared import PANEL_CSS
from trader.interfaces.ui.palette import (
    CASYS_ACCENT,
    CASYS_DIM,
    CASYS_FAINT,
    CASYS_FG,
    CASYS_MUTED,
    CASYS_WARNING,
)

# ---------------------------------------------------------------------------
# Chemins — patchables dans les tests.
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parents[4]  # racine du repo
_CONFIG_DIR: Path = _ROOT / "config"

# Couleurs du panel RISK GATE (non-standard — warning-tinted)
_RISK_BORDER = "#4a3f2a"
_RISK_TITLE = CASYS_WARNING

# ---------------------------------------------------------------------------
# Registre des lignes éditables
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SettingRow:
    """Une ligne éditable de la page Settings."""

    key: str        # clé dans le yaml
    label: str      # label affiché (faint, ≤20ch)
    yaml_file: str  # "portfolio" | "radar"
    effect: str     # "next cycle" | "next rotation"
    vtype: str      # "int" | "float" | "bool" | "str"


# Ordre = ordre de navigation j/k.
EDITABLE_ROWS: tuple[SettingRow, ...] = (
    SettingRow("starting_cash", "starting cash", "portfolio", "next cycle", "int"),
    SettingRow("cap_m", "hot-set size", "radar", "next rotation", "int"),
    SettingRow("delta", "delta threshold", "radar", "next rotation", "float"),
    SettingRow("dwell_days", "dwell days", "radar", "next rotation", "int"),
    SettingRow("override_enabled", "llm override", "radar", "next rotation", "bool"),
    SettingRow("preopen_window_minutes", "preopen window min", "radar", "next rotation", "int"),
)

_KEY_TO_ROW: dict[str, SettingRow] = {r.key: r for r in EDITABLE_ROWS}
_N_ROWS = len(EDITABLE_ROWS)

# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _load_yaml_safe(path: Path) -> dict:
    """yaml.safe_load silencieux → dict (jamais d'exception)."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_portfolio_settings(config_dir: Path) -> dict:
    return _load_yaml_safe(config_dir / "portfolio.yaml")


def load_radar_settings(config_dir: Path) -> dict:
    return _load_yaml_safe(config_dir / "radar.yaml")


def load_risk_settings(config_dir: Path) -> dict:
    return _load_yaml_safe(config_dir / "risk.yaml")


def load_data_settings(config_dir: Path) -> dict:
    return _load_yaml_safe(config_dir / "data_sources.yaml")


def load_env_display(root: Path) -> dict[str, str]:
    """Lit .env pour affichage uniquement (masque les clés secrètes). Pas d'exception."""
    result: dict[str, str] = {}
    env_file = root / ".env"
    try:
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Masquer les secrets
            if any(x in key.upper() for x in ("KEY", "SECRET", "PASSWORD", "TOKEN")):
                result[key] = "***"
            else:
                result[key] = value
    except Exception:
        pass
    return result


# En-têtes statiques réinjectés lors de la réécriture (safe_dump détruit
# les commentaires originaux).
_HEADERS: dict[str, str] = {
    "portfolio": "# Capital de depart du portefeuille paper.\n",
    "radar": (
        "# Parametres du radar — edites via le cockpit casys.\n"
        "# Les commentaires originaux ne sont pas preserves (yaml.safe_dump).\n"
    ),
}


def write_yaml_atomic(config_dir: Path, yaml_file: str, updates: dict[str, Any]) -> None:
    """Écrit yaml_file.yaml avec les mises à jour, atomiquement.

    Pattern : tempfile.mkstemp dans le même répertoire + os.replace (POSIX).
    Les commentaires existants sont perdus ; un en-tête statique est réinjecté.
    risk.yaml n'est JAMAIS écrit par cette fonction (vérification défensive).
    """
    if yaml_file == "risk":
        raise ValueError("risk.yaml is read-only — the cockpit must never write it")

    path = config_dir / f"{yaml_file}.yaml"
    data = _load_yaml_safe(path)
    data.update(updates)

    header = _HEADERS.get(yaml_file, "")
    dumped = yaml.safe_dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    content = header + dumped

    fd, tmp = tempfile.mkstemp(dir=config_dir, suffix=".yaml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def coerce_value(raw: str, vtype: str) -> tuple[Any, str | None]:
    """Convertit raw selon vtype → (valeur, erreur). Jamais d'exception."""
    try:
        if vtype == "int":
            return int(raw), None
        if vtype == "float":
            return float(raw), None
        if vtype == "bool":
            if raw.lower() in ("true", "1", "yes", "on"):
                return True, None
            if raw.lower() in ("false", "0", "no", "off"):
                return False, None
            return None, f"expected true/false, got {raw!r}"
        return raw, None
    except ValueError:
        return None, f"expected {vtype}, got {raw!r}"


# ---------------------------------------------------------------------------
# Builders purs — (config_dict, pending, cursor_key) → RenderableType
# ---------------------------------------------------------------------------

_KEY_COL = 20
_EFF_COL = 16


def _grid3() -> Table:
    """Grille 3 colonnes : clé (20ch) | valeur (1fr) | effet (16ch right)."""
    t = Table.grid(padding=(0, 1))
    t.add_column(no_wrap=True, width=_KEY_COL)
    t.add_column(no_wrap=True)
    t.add_column(no_wrap=True, justify="right", width=_EFF_COL)
    return t


def _grid2() -> Table:
    """Grille 2 colonnes : clé (20ch) | valeur."""
    t = Table.grid(padding=(0, 1))
    t.add_column(no_wrap=True, width=_KEY_COL)
    t.add_column(no_wrap=True)
    return t


def _val_text(value: Any, *, cursor: bool, pending: bool, original: Any) -> Text:
    """Texte colonne valeur — bold fg si cursor/pending, muted sinon."""
    display = str(value) if value is not None else "—"
    if cursor or pending:
        t = Text()
        t.append(display, style=f"bold {CASYS_FG}")
        orig_str = str(original) if original is not None else None
        if pending and orig_str is not None and orig_str != display:
            t.append(f"  (was {orig_str})", style=CASYS_FAINT)
        return t
    return Text(display, style=CASYS_MUTED)


def _eff_text(effect: str, *, cursor: bool, pending: bool) -> Text:
    """Texte colonne effet — accent si cursor/pending, faint sinon."""
    if cursor:
        return Text("enter save · esc", style=CASYS_ACCENT, justify="right")
    if pending:
        return Text("● pending", style=CASYS_ACCENT, justify="right")
    return Text(effect, style=CASYS_FAINT, justify="right")


def build_intro() -> Text:
    """Ligne d'introduction dim sous la KPI band."""
    t = Text()
    t.append("settings write to ", style=CASYS_DIM)
    t.append("config/*.yaml", style=CASYS_MUTED)
    t.append(
        " — the daemon reloads them at the next cycle"
        " · nothing here touches state/ or the mandate",
        style=CASYS_DIM,
    )
    return t


def build_runtime_panel(env: dict) -> RenderableType:
    """Panel RUNTIME — .env (READ-ONLY, restart required pour tout changement)."""
    note = Text(
        "env vars are read once at boot — edit .env and restart the daemon",
        style=f"italic {CASYS_FAINT}",
    )
    grid = _grid3()
    ib_host = env.get("CASYS_IB_HOST") or "—"
    ib_cid = env.get("CASYS_IB_CLIENT_ID") or "—"
    ib_port = env.get("CASYS_IB_PORT") or "—"
    log_level = env.get("CASYS_LOG_LEVEL") or "INFO"
    profile = env.get("TRADER_DATA_PROFILE") or "—"

    rows = [
        ("ib host / client id", f"{ib_host} · {ib_cid}"),
        ("ib port", ib_port),
        ("log level", log_level),
        ("data profile", profile),
    ]
    for label, value in rows:
        grid.add_row(
            Text(label, style=CASYS_FAINT, no_wrap=True),
            Text(value, style=CASYS_MUTED, no_wrap=True),
            Text("restart required", style=CASYS_FAINT, no_wrap=True),
        )

    gap = Text(
        "v1 scope: the cockpit never writes .env"
        " — mode toggle from the mockup is deferred (restart required anyway)",
        style=f"italic {CASYS_FAINT}",
    )
    return Group(note, grid, gap)


def build_budget_panel(portfolio: dict, pending: dict, cursor_key: str | None) -> RenderableType:
    """Panel CYCLE & BUDGET — portfolio.yaml (starting_cash éditable)."""
    grid = _grid3()
    row = _KEY_TO_ROW["starting_cash"]
    original = portfolio.get(row.key)
    cur_val = pending.get(row.key, original)
    is_cur = cursor_key == row.key
    is_pend = row.key in pending

    grid.add_row(
        Text(row.label, style=CASYS_FAINT, no_wrap=True),
        _val_text(cur_val, cursor=is_cur, pending=is_pend, original=original),
        _eff_text(row.effect, cursor=is_cur, pending=is_pend),
    )
    return grid


def build_data_panel(data: dict) -> RenderableType:
    """Panel DATA — data_sources.yaml (READ-ONLY, restart required)."""
    grid = _grid3()
    profile = data.get("profile") or "—"
    grid.add_row(
        Text("profile", style=CASYS_FAINT, no_wrap=True),
        Text(profile, style=CASYS_MUTED, no_wrap=True),
        Text("restart required", style=CASYS_FAINT, no_wrap=True),
    )
    profiles = data.get("profiles") or {}
    routes = (profiles.get(profile) or {}).get("routes") or []
    for route in routes[:3]:
        sources = " · ".join(str(s) for s in (route.get("sources") or []))
        syms: list = route.get("symbols") or []
        sym_label = syms[0] if len(syms) == 1 else f"{len(syms)} patterns"
        grid.add_row(
            Text(f"  {sym_label}", style=CASYS_FAINT, no_wrap=True),
            Text(sources, style=CASYS_MUTED, no_wrap=True),
            Text("restart required", style=CASYS_FAINT, no_wrap=True),
        )
    note = Text(
        "data_sources.yaml is parsed once at boot — restart required for any change",
        style=f"italic {CASYS_FAINT}",
    )
    return Group(grid, note)


def build_rotation_panel(radar: dict, pending: dict, cursor_key: str | None) -> RenderableType:
    """Panel ROTATION — radar.yaml (cap_m, delta, dwell_days, override_enabled, preopen_window_minutes)."""
    grid = _grid3()
    for row in EDITABLE_ROWS:
        if row.yaml_file != "radar":
            continue
        original = radar.get(row.key)
        cur_val = pending.get(row.key, original)
        is_cur = cursor_key == row.key
        is_pend = row.key in pending
        grid.add_row(
            Text(row.label, style=CASYS_FAINT, no_wrap=True),
            _val_text(cur_val, cursor=is_cur, pending=is_pend, original=original),
            _eff_text(row.effect, cursor=is_cur, pending=is_pend),
        )
    return grid


def build_risk_panel(risk: dict) -> RenderableType:
    """Panel RISK GATE — risk.yaml (READ-ONLY, non-négociable par design)."""

    def _dollar(v: Any) -> str:
        try:
            return f"${int(float(v)):,}"
        except (TypeError, ValueError):
            return "—"

    def _pct(v: Any) -> str:
        try:
            return f"{float(v) * 100:.1f}% of equity"
        except (TypeError, ValueError):
            return "—"

    grid = _grid2()
    specs: list[tuple[str, str, Any]] = [
        ("max_gross_exposure", "max gross exposure", _dollar),
        ("max_position_value", "per-symbol cap", _dollar),
        ("max_order_value", "max order", _dollar),
        ("max_risk_per_trade_pct", "risk per trade", _pct),
        ("min_equity", "min equity", _dollar),
        ("confidence_gate_enabled", "confidence gate", str),
        ("require_hard_stop", "require hard stop", str),
    ]
    for key, label, fmt in specs:
        raw = risk.get(key)
        display = fmt(raw) if raw is not None else "—"
        grid.add_row(
            Text(label, style=CASYS_FAINT, no_wrap=True),
            Text(display, style=CASYS_MUTED, no_wrap=True),
        )

    footnote = Text(
        "the fuse is non-negotiable by design — edit config/risk.yaml by hand,"
        " in a commit. The cockpit will never write it.",
        style=f"italic {CASYS_FAINT}",
    )
    return Group(grid, footnote)


def build_pending_banner(pending: dict) -> Text | None:
    """None si pas de pending. Texte de la bannière sinon."""
    if not pending:
        return None
    n = len(pending)
    noun = "change" if n == 1 else "changes"
    keys_str = ", ".join(pending.keys())
    t = Text()
    t.append("● ", style=CASYS_WARNING)
    t.append(f"{n} pending {noun} — {keys_str}", style=CASYS_WARNING)
    t.append("    ")
    t.append("w", style=f"bold {CASYS_ACCENT}")
    t.append(" write to config/", style=CASYS_DIM)
    t.append("    ")
    t.append("r", style=f"bold {CASYS_ACCENT}")
    t.append(" revert all", style=CASYS_DIM)
    return t


# ---------------------------------------------------------------------------
# Widget — SettingsPage
# ---------------------------------------------------------------------------

_RISK_CSS = """
.casys-risk-panel {
    border: solid #4a3f2a;
    border-title-color: #e5c07b;
    border-title-style: bold;
    padding: 0 1;
}
"""


class _NonFocusableScroll(VerticalScroll):
    """VerticalScroll sans focus clavier — le focus reste sur SettingsPage."""

    can_focus = False


class SettingsPage(Static):
    """Page 8 — Settings : écritures yaml explicites via w/r."""

    can_focus = True

    DEFAULT_CSS = (
        PANEL_CSS
        + _RISK_CSS
        + """
    SettingsPage {
        layout: vertical;
        height: 100%;
        padding: 0;
    }
    SettingsPage #settings-intro {
        height: auto;
        padding: 1 2 0 2;
    }
    SettingsPage #settings-body {
        height: 1fr;
        padding: 1 2 0 2;
    }
    SettingsPage #settings-left {
        width: 1fr;
        height: 100%;
        margin-right: 1;
    }
    SettingsPage #settings-right {
        width: 1fr;
        height: 100%;
    }
    SettingsPage #runtime-panel { height: auto; margin-bottom: 1; }
    SettingsPage #budget-panel  { height: auto; }
    SettingsPage #data-panel    { height: auto; margin-bottom: 1; }
    SettingsPage #rotation-panel { height: 1fr; margin-bottom: 1; }
    SettingsPage #risk-panel    { height: auto; }
    SettingsPage .casys-panel Static { height: auto; }
    SettingsPage .casys-risk-panel Static { height: auto; }
    SettingsPage #pending-banner {
        height: auto;
        margin: 0 2;
        padding: 0 1;
        display: none;
    }
    SettingsPage #edit-input {
        height: 3;
        margin: 0 2 1 2;
        display: none;
    }
    """
    )

    BINDINGS = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("down", "cursor_down", show=False),
        Binding("up", "cursor_up", show=False),
        Binding("w", "write_settings", show=False),
        Binding("r", "revert_settings", show=False),
    ]

    # État interne (class-level defaults, remplacés par instance en on_mount)
    _portfolio: dict
    _radar: dict
    _risk: dict
    _data: dict
    _env: dict
    _pending: dict
    _cursor_idx: int
    _editing: bool

    def compose(self) -> ComposeResult:
        yield Static(id="settings-intro")
        with Horizontal(id="settings-body"):
            with Vertical(id="settings-left"):
                with _NonFocusableScroll(id="runtime-panel", classes="casys-panel") as p:
                    p.border_title = "RUNTIME — .env"
                    yield Static(id="runtime-body")
                with _NonFocusableScroll(id="budget-panel", classes="casys-panel") as p:
                    p.border_title = "CYCLE & BUDGET — portfolio.yaml"
                    yield Static(id="budget-body")
            with Vertical(id="settings-right"):
                with _NonFocusableScroll(id="data-panel", classes="casys-panel") as p:
                    p.border_title = "DATA — data_sources.yaml"
                    yield Static(id="data-body")
                with _NonFocusableScroll(id="rotation-panel", classes="casys-panel") as p:
                    p.border_title = "ROTATION — radar.yaml"
                    yield Static(id="rotation-body")
                with _NonFocusableScroll(id="risk-panel", classes="casys-risk-panel") as p:
                    p.border_title = "RISK GATE — config/risk.yaml · READ-ONLY"
                    yield Static(id="risk-body")
        yield Static(id="pending-banner")
        yield Input(placeholder="new value", id="edit-input", disabled=True)

    def on_mount(self) -> None:
        self._portfolio = {}
        self._radar = {}
        self._risk = {}
        self._data = {}
        self._env = {}
        self._pending = {}
        self._cursor_idx = 0
        self._editing = False
        self._reload_config()
        self._render_all()

    # ── Public contract ───────────────────────────────────────────────────

    def update_state(self, state: dict) -> None:  # noqa: ARG002
        """Appelé par app.py à chaque refresh. Recharge les yamls depuis le disque."""
        try:
            self._reload_config()
            self._render_all()
        except Exception:
            pass

    # ── Config I/O ────────────────────────────────────────────────────────

    def _reload_config(self) -> None:
        cd = _CONFIG_DIR
        self._portfolio = load_portfolio_settings(cd)
        self._radar = load_radar_settings(cd)
        self._risk = load_risk_settings(cd)
        self._data = load_data_settings(cd)
        self._env = load_env_display(cd.parent)

    def _cursor_key(self) -> str:
        return EDITABLE_ROWS[self._cursor_idx].key

    # ── Rendering ─────────────────────────────────────────────────────────

    def _render_all(self) -> None:
        ck = self._cursor_key() if not self._editing else None
        _safe_update = self._safe_update
        _safe_update("#settings-intro", build_intro())
        _safe_update("#runtime-body", build_runtime_panel(self._env))
        _safe_update("#budget-body", build_budget_panel(self._portfolio, self._pending, ck))
        _safe_update("#data-body", build_data_panel(self._data))
        _safe_update("#rotation-body", build_rotation_panel(self._radar, self._pending, ck))
        _safe_update("#risk-body", build_risk_panel(self._risk))
        self._refresh_pending_banner()

    def _safe_update(self, selector: str, renderable: RenderableType) -> None:
        try:
            self.query_one(selector, Static).update(renderable)
        except Exception:
            pass

    def _refresh_pending_banner(self) -> None:
        try:
            banner = self.query_one("#pending-banner", Static)
            text = build_pending_banner(self._pending)
            if text is not None:
                banner.update(text)
                banner.display = True
            else:
                banner.update(Text(""))
                banner.display = False
        except Exception:
            pass

    # ── Key handling ──────────────────────────────────────────────────────

    def on_key(self, event) -> None:  # type: ignore[override]
        """enter = démarrer édition (si non en édition) · escape = annuler."""
        if event.key == "enter" and not self._editing:
            self._start_edit()
            event.stop()
        elif event.key == "escape" and self._editing:
            self._cancel_edit()
            event.stop()

    # ── Actions ───────────────────────────────────────────────────────────

    def action_cursor_down(self) -> None:
        if self._editing:
            return
        self._cursor_idx = (self._cursor_idx + 1) % _N_ROWS
        self._render_all()

    def action_cursor_up(self) -> None:
        if self._editing:
            return
        self._cursor_idx = (self._cursor_idx - 1) % _N_ROWS
        self._render_all()

    def action_write_settings(self) -> None:
        """Écriture atomique des fichiers yaml modifiés."""
        if self._editing or not self._pending:
            return
        # Regrouper par fichier yaml
        by_file: dict[str, dict] = {}
        for key, value in self._pending.items():
            row = _KEY_TO_ROW.get(key)
            if row is None:
                continue
            by_file.setdefault(row.yaml_file, {})[key] = value
        for yaml_file, updates in by_file.items():
            try:
                write_yaml_atomic(_CONFIG_DIR, yaml_file, updates)
            except Exception:
                pass
        self._pending.clear()
        self._reload_config()
        self._render_all()

    def action_revert_settings(self) -> None:
        """Annule toutes les modifications en attente."""
        if self._editing:
            return
        self._pending.clear()
        self._render_all()

    # ── Inline edit (Input en bas de page) ───────────────────────────────

    def _start_edit(self) -> None:
        row = EDITABLE_ROWS[self._cursor_idx]
        source = self._portfolio if row.yaml_file == "portfolio" else self._radar
        current = self._pending.get(row.key, source.get(row.key))
        inp = self.query_one("#edit-input", Input)
        inp.placeholder = f"{row.label} ({row.vtype})"
        inp.value = str(current) if current is not None else ""
        inp.disabled = False
        inp.display = True
        self._editing = True
        self._render_all()
        inp.focus()

    def _cancel_edit(self) -> None:
        try:
            inp = self.query_one("#edit-input", Input)
            inp.disabled = True
            inp.display = False
        except Exception:
            pass
        self._editing = False
        self._render_all()
        self.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "edit-input":
            return
        raw = event.value.strip()
        if raw:
            row = EDITABLE_ROWS[self._cursor_idx]
            value, error = coerce_value(raw, row.vtype)
            if error is None and value is not None:
                source = self._portfolio if row.yaml_file == "portfolio" else self._radar
                original = source.get(row.key)
                if str(value) != str(original):
                    self._pending[row.key] = value
        self._cancel_edit()
