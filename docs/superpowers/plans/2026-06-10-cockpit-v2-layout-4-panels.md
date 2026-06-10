# Cockpit v2 — Layout 3 colonnes + 4 nouveaux panneaux Rich

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remodeler le cockpit Textual en layout 3 colonnes (gauche/centre/droite) avec 4 nouveaux builders Rich palettisés et ajouter la colonne `data_source` aux décisions récentes, le tout en TDD strict sans aucun commit.

**Architecture:** Tous les builders restent dans `trader/tui.py` (ajout en fin de fichier) ; les nouveaux readers de fichiers (trade_plans, scheduler, daemon_status, decisions tail) s'ajoutent dans `tui.py` juste après les readers existants. Le cockpit `trader/cockpit.py` subit uniquement : (1) les imports des 4 nouveaux builders, (2) le refactoring CSS+layout 3 colonnes, (3) l'intégration des nouvelles données dans `load_runtime_state` (via nouveaux paramètres) et le worker d'état. La palette `trader/palette.py` reçoit 3 nouveaux tokens. Les tests vivent dans `tests/test_tui_builders_v2.py` (nouveaux builders) et `tests/test_cockpit_smoke.py` reçoit les nouveaux smoke tests de layout v2.

**Tech Stack:** Python 3.11+, Rich (Panel, Table, Text, Group), Textual (Static, Vertical, Horizontal), pytest, uv.

---

## Fichiers touchés

| Fichier | Action |
|---------|--------|
| `trader/palette.py` | Modifier — ajouter 3 tokens dans `Palette`, `PALETTE_DARK`, `PALETTE_LIGHT` |
| `trader/tui.py` | Modifier — ajouter 4 readers + 4 builders + amender `_build_decisions_table` (col `data_source`) + amender `load_runtime_state` |
| `trader/cockpit.py` | Modifier — layout 3 colonnes CSS, nouvelles colonnes, imports, worker, toggle d |
| `tests/test_tui_builders_v2.py` | Créer — TDD pour les 4 nouveaux builders + tail decisions + palette |
| `tests/test_cockpit_smoke.py` | Modifier — smoke tests layout v2 |

**Fichiers INTERDITS (ne pas toucher) :**
`trader/daemon.py`, `trader/cockpit_supervisor.py`, `trader/consolidator.py`,
`tests/test_consolidator.py`, `tests/test_cli_semantic.py`, `tests/test_daemon_learnings.py`,
`tests/test_llm.py`, `README.md`, `Makefile`, `docs/**` (sauf ce plan).

---

## Task 1 : Tokens palette pour les 4 nouveaux panneaux

**Files:**
- Modify: `trader/palette.py`
- Test: `tests/test_tui_builders_v2.py` (créer)

- [ ] **Step 1 : Écrire les tests d'existence des nouveaux tokens**

Créer `tests/test_tui_builders_v2.py` :

```python
"""TDD pour les 4 nouveaux builders cockpit v2 et tokens palette associés."""
from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from trader.palette import ALL_PALETTE_KEYS, PALETTE_DARK, PALETTE_LIGHT


def _render(renderable, width: int = 120) -> str:
    console = Console(width=width, highlight=False)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


# ---------------------------------------------------------------------------
# Task 1 — tokens palette
# ---------------------------------------------------------------------------


def test_palette_contient_border_plans() -> None:
    """Les deux palettes exposent le token border_plans."""
    assert "border_plans" in PALETTE_DARK
    assert "border_plans" in PALETTE_LIGHT


def test_palette_contient_border_watches() -> None:
    assert "border_watches" in PALETTE_DARK
    assert "border_watches" in PALETTE_LIGHT


def test_palette_contient_border_llm_activity() -> None:
    assert "border_llm_activity" in PALETTE_DARK
    assert "border_llm_activity" in PALETTE_LIGHT


def test_all_palette_keys_inclut_nouveaux_tokens() -> None:
    for key in ("border_plans", "border_watches", "border_llm_activity"):
        assert key in ALL_PALETTE_KEYS


def test_palette_light_ne_contient_pas_couleurs_dark_brutes_apres_ajout() -> None:
    """PALETTE_LIGHT ne doit pas contenir de valeurs DARK brutes pour les nouveaux tokens."""
    dark_raw = {"cyan", "magenta", "blue", "green", "red",
                "bold green", "bold red", "bold cyan", "dim white",
                "dim grey50", "bold yellow"}
    for key in ("border_plans", "border_watches", "border_llm_activity"):
        assert PALETTE_LIGHT[key] not in dark_raw, \
            f"PALETTE_LIGHT['{key}'] = {PALETTE_LIGHT[key]!r} est une couleur DARK brute"
```

- [ ] **Step 2 : Vérifier que les tests échouent (tokens absents)**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui_builders_v2.py::test_palette_contient_border_plans -v
```

Attendu : `FAILED` — KeyError ou AssertionError.

- [ ] **Step 3 : Ajouter les 3 tokens dans `trader/palette.py`**

Lire le fichier, puis dans la `class Palette(TypedDict)`, après `border_learnings: str`, ajouter :

```python
    # Nouveaux panneaux v2
    border_plans: str       # plans de sortie ouverts
    border_watches: str     # veilles actives
    border_llm_activity: str  # activité LLM
```

Dans `PALETTE_DARK`, après `"border_learnings": "blue"` :

```python
    "border_plans": "cyan",
    "border_watches": "cyan",
    "border_llm_activity": "magenta",
```

Dans `PALETTE_LIGHT`, après `"border_learnings": "#0D7680"` :

```python
    "border_plans": "#0D7680",
    "border_watches": "#0D7680",
    "border_llm_activity": "#0D7680",
```

Dans `ALL_PALETTE_KEYS` — cette constante est `tuple(PALETTE_DARK.keys())` donc elle se met à jour automatiquement.

- [ ] **Step 4 : Vérifier que les tests de palette passent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui_builders_v2.py -k "palette" -v
```

Attendu : 5 PASSED.

- [ ] **Step 5 : Suite complète propre (régression palette existante)**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_palette.py -q
```

Attendu : toutes passent (les tests de palette DARK existants vérifient que les valeurs n'ont pas changé).

---

## Task 2 : Reader `_load_trade_plans_safe` + builder `_build_exit_plans_panel`

**Files:**
- Modify: `trader/tui.py` (ajout reader + builder)
- Modify: `tests/test_tui_builders_v2.py` (ajout tests)

### Sous-tâche 2a : Tests du reader bornée

- [ ] **Step 1 : Écrire les tests reader trade_plans**

Ajouter dans `tests/test_tui_builders_v2.py` :

```python
# ---------------------------------------------------------------------------
# Task 2 — reader _load_trade_plans_safe
# ---------------------------------------------------------------------------


def test_load_trade_plans_safe_retourne_liste_avec_fichier_valide(tmp_path) -> None:
    from trader.tui import _load_trade_plans_safe
    plans_file = tmp_path / "trade_plans.json"
    plans_file.write_text(
        '{"plans": [{"id": "AAPL-2026", "symbol": "AAPL", "side": "LONG",'
        '"entry_price": 170.0, "remaining_quantity": 5.0, "opened_at": "2026-06-10T10:00:00Z",'
        '"hard_stop_price": 160.0, "take_profits": [{"name": "tp1", "price": 185.0, "fraction": 0.5, "quantity": 2.5}],'
        '"max_hold_minutes": 240.0, "quantity": 5.0}]}',
        encoding="utf-8"
    )
    plans = _load_trade_plans_safe(plans_file)
    assert isinstance(plans, list)
    assert len(plans) == 1
    assert plans[0]["symbol"] == "AAPL"


def test_load_trade_plans_safe_retourne_vide_si_fichier_absent(tmp_path) -> None:
    from trader.tui import _load_trade_plans_safe
    result = _load_trade_plans_safe(tmp_path / "nope.json")
    assert result == []


def test_load_trade_plans_safe_retourne_vide_si_json_corrompu(tmp_path) -> None:
    from trader.tui import _load_trade_plans_safe
    bad = tmp_path / "trade_plans.json"
    bad.write_text("{not valid json", encoding="utf-8")
    result = _load_trade_plans_safe(bad)
    assert result == []


def test_load_trade_plans_safe_retourne_vide_si_plans_absent(tmp_path) -> None:
    from trader.tui import _load_trade_plans_safe
    f = tmp_path / "trade_plans.json"
    f.write_text('{"plans": []}', encoding="utf-8")
    result = _load_trade_plans_safe(f)
    assert result == []
```

- [ ] **Step 2 : Vérifier échec**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui_builders_v2.py -k "trade_plans" -v
```

Attendu : ImportError ou AttributeError (symbole absent).

### Sous-tâche 2b : Implémentation reader

- [ ] **Step 3 : Ajouter `_load_trade_plans_safe` dans `trader/tui.py`**

Après la fonction `_load_learnings_safe` (ligne ~141), insérer :

```python
def _load_trade_plans_safe(plans_path: Path) -> list[dict]:
    """Lit state/trade_plans.json, retourne la liste des plans ouverts.

    Tolérant : retourne [] si fichier absent, corrompu ou sans clé 'plans'.
    """
    try:
        raw = json.loads(plans_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return []
        plans = raw.get("plans", [])
        if not isinstance(plans, list):
            return []
        return [p for p in plans if isinstance(p, dict)]
    except Exception:
        return []
```

- [ ] **Step 4 : Vérifier que les tests reader passent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui_builders_v2.py -k "trade_plans" -v
```

Attendu : 4 PASSED.

### Sous-tâche 2c : Tests du builder

- [ ] **Step 5 : Écrire les tests du builder `_build_exit_plans_panel`**

Ajouter dans `tests/test_tui_builders_v2.py` :

```python
# ---------------------------------------------------------------------------
# Task 2 — builder _build_exit_plans_panel
# ---------------------------------------------------------------------------


def test_build_exit_plans_panel_avec_un_plan_contient_symbol() -> None:
    from trader.tui import _build_exit_plans_panel
    plans = [{
        "id": "AAPL-2026", "symbol": "AAPL", "side": "LONG",
        "entry_price": 170.0, "remaining_quantity": 5.0,
        "opened_at": "2026-06-10T10:00:00Z",
        "hard_stop_price": 160.0,
        "take_profits": [{"name": "tp1", "price": 185.0, "fraction": 0.5, "quantity": 2.5}],
        "max_hold_minutes": 240.0, "quantity": 5.0,
    }]
    result = _build_exit_plans_panel(plans)
    output = _render(result)
    assert "AAPL" in output


def test_build_exit_plans_panel_avec_plan_affiche_stop_distance() -> None:
    from trader.tui import _build_exit_plans_panel
    plans = [{
        "id": "AAPL-2026", "symbol": "AAPL", "side": "LONG",
        "entry_price": 200.0, "remaining_quantity": 5.0,
        "opened_at": "2026-06-10T10:00:00Z",
        "hard_stop_price": 180.0,
        "take_profits": [],
        "max_hold_minutes": None, "quantity": 5.0,
    }]
    result = _build_exit_plans_panel(plans)
    output = _render(result)
    # stop distance = (200-180)/200 = 10%
    assert "10.0" in output or "10%" in output or "180" in output


def test_build_exit_plans_panel_vide_affiche_aucun_plan() -> None:
    from trader.tui import _build_exit_plans_panel
    result = _build_exit_plans_panel([])
    output = _render(result)
    assert "aucun" in output.lower()


def test_build_exit_plans_panel_avec_light_ne_plante_pas() -> None:
    from trader.tui import _build_exit_plans_panel
    result = _build_exit_plans_panel([], palette=PALETTE_LIGHT)
    output = _render(result)
    assert output is not None


def test_build_exit_plans_panel_retourne_panel() -> None:
    from trader.tui import _build_exit_plans_panel
    result = _build_exit_plans_panel([])
    assert isinstance(result, Panel)


def test_build_exit_plans_panel_absent_ne_crashe_pas() -> None:
    """Simule fichier absent : plans=[] → Panel sans exception."""
    from trader.tui import _build_exit_plans_panel
    # plans=[] représente le cas fichier absent (reader retourne [])
    result = _build_exit_plans_panel([])
    assert result is not None
```

- [ ] **Step 6 : Vérifier échec**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui_builders_v2.py -k "exit_plans" -v
```

Attendu : ImportError ou AttributeError.

### Sous-tâche 2d : Implémentation builder

- [ ] **Step 7 : Ajouter `_build_exit_plans_panel` dans `trader/tui.py`**

Après `_build_learnings_panel`, avant `build_view`, ajouter :

```python
def _build_exit_plans_panel(
    plans: list[dict], *, palette: Palette = PALETTE_DARK
) -> Panel:
    """Plans de sortie ouverts (colonne gauche, sous positions).

    Source : state/trade_plans.json — liste brute de dicts.
    """
    if not plans:
        return Panel(
            Text("aucun plan ouvert", style=palette["dim"]),
            title="[bold]Plans sortie[/bold]",
            border_style=palette["border_plans"],
            expand=True,
        )

    lines: list[RenderableType] = []
    for plan in plans:
        symbol = str(plan.get("symbol", "?"))
        side = str(plan.get("side", "?"))
        entry = _safe_float(plan.get("entry_price"), default=None)
        stop = _safe_float(plan.get("hard_stop_price"), default=None)
        remaining = _safe_float(plan.get("remaining_quantity"), default=0.0) or 0.0
        max_hold = _safe_float(plan.get("max_hold_minutes"), default=None)
        take_profits = _safe_list_of_dicts(plan.get("take_profits") or [])
        opened_at = str(plan.get("opened_at") or "")

        side_style = (
            palette["action_buy"] if side == "LONG" else palette["action_sell"]
        )

        # Ligne principale : symbole side  entrée → stop (dist%)
        if entry is not None and stop is not None and entry > 0:
            dist_pct = abs(entry - stop) / entry * 100.0
            stop_str = f"${stop:,.2f} ({dist_pct:.1f}%)"
        elif stop is not None:
            stop_str = f"${stop:,.2f}"
        else:
            stop_str = "—"

        entry_str = f"${entry:,.2f}" if entry is not None else "—"

        header = Text.assemble(
            (symbol, f"bold {palette['kpi_default']}"),
            ("  ", ""),
            (side, side_style),
            ("  entrée:", palette["dim"]),
            (f" {entry_str}", "bold"),
            ("  stop:", palette["dim"]),
            (f" {stop_str}", palette["pnl_negative"] if stop else palette["dim"]),
            ("  qté:", palette["dim"]),
            (f" {remaining:,.4f}", "bold"),
        )
        lines.append(header)

        # Take-profits sur une ligne compacte
        if take_profits:
            tp_parts: list[tuple[str, str]] = []
            for tp in take_profits:
                tp_price = _safe_float(tp.get("price"), default=None)
                tp_name = str(tp.get("name") or "tp?")
                if tp_price is not None:
                    tp_parts.append((f"{tp_name}@${tp_price:,.2f}", palette["pnl_positive"]))
                    tp_parts.append(("  ", ""))
            if tp_parts:
                tp_line = Text.assemble(("  TPs: ", palette["dim"]), *tp_parts)
                lines.append(tp_line)

        # max_hold
        if max_hold is not None:
            lines.append(Text.assemble(
                ("  max hold:", palette["dim"]),
                (f" {int(max_hold)}min", "bold"),
            ))

        lines.append(Text(""))  # séparateur

    # Retire le dernier séparateur vide
    if lines and isinstance(lines[-1], Text) and lines[-1].plain == "":
        lines.pop()

    return Panel(
        Group(*lines),
        title="[bold]Plans sortie[/bold]",
        border_style=palette["border_plans"],
        expand=True,
    )
```

- [ ] **Step 8 : Vérifier que les tests builder passent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui_builders_v2.py -k "exit_plans" -v
```

Attendu : 6 PASSED.

---

## Task 3 : Reader `_load_indicator_watches_safe` + builder `_build_watches_panel`

**Files:**
- Modify: `trader/tui.py`
- Modify: `tests/test_tui_builders_v2.py`

### Sous-tâche 3a : Tests reader

- [ ] **Step 1 : Écrire les tests reader watches**

Ajouter dans `tests/test_tui_builders_v2.py` :

```python
# ---------------------------------------------------------------------------
# Task 3 — reader _load_indicator_watches_safe
# ---------------------------------------------------------------------------


def test_load_indicator_watches_safe_retourne_liste_avec_fichier_valide(tmp_path) -> None:
    from trader.tui import _load_indicator_watches_safe
    sched = {
        "indicator_watches": {
            "AAPL:abc123": {
                "id": "AAPL:abc123",
                "symbol": "AAPL",
                "created_at": "2026-06-10T01:00:00Z",
                "expires_at": "2026-06-10T05:00:00Z",
                "logic": "any",
                "conditions": [{"indicator": "z_score", "op": "abs>=", "value": 2.0, "timeframe": "1h"}],
            }
        },
        "stale_streaks": {},
    }
    f = tmp_path / "scheduler.json"
    import json as _json
    f.write_text(_json.dumps(sched), encoding="utf-8")
    watches = _load_indicator_watches_safe(f)
    assert isinstance(watches, list)
    assert len(watches) == 1
    assert watches[0]["symbol"] == "AAPL"


def test_load_indicator_watches_safe_retourne_vide_si_absent(tmp_path) -> None:
    from trader.tui import _load_indicator_watches_safe
    result = _load_indicator_watches_safe(tmp_path / "nope.json")
    assert result == []


def test_load_indicator_watches_safe_retourne_vide_si_corrompu(tmp_path) -> None:
    from trader.tui import _load_indicator_watches_safe
    bad = tmp_path / "scheduler.json"
    bad.write_text("{bad json", encoding="utf-8")
    result = _load_indicator_watches_safe(bad)
    assert result == []


def test_load_indicator_watches_safe_charge_stale_streaks(tmp_path) -> None:
    """La fonction doit retourner les stale_streaks via le second return value."""
    from trader.tui import _load_scheduler_data_safe
    import json as _json
    sched = {"indicator_watches": {}, "stale_streaks": {"SPY": 3}}
    f = tmp_path / "scheduler.json"
    f.write_text(_json.dumps(sched), encoding="utf-8")
    watches, streaks = _load_scheduler_data_safe(f)
    assert streaks == {"SPY": 3}
```

- [ ] **Step 2 : Vérifier échec**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui_builders_v2.py -k "indicator_watches or stale_streaks or scheduler_data" -v
```

Attendu : ImportError.

### Sous-tâche 3b : Implémentation readers scheduler

- [ ] **Step 3 : Ajouter `_load_scheduler_data_safe` et `_load_indicator_watches_safe` dans `trader/tui.py`**

Après `_load_trade_plans_safe` :

```python
def _load_scheduler_data_safe(scheduler_path: Path) -> tuple[list[dict], dict]:
    """Lit scheduler.json, retourne (indicator_watches, stale_streaks).

    indicator_watches : liste de dicts (valeurs du dict indicator_watches).
    stale_streaks     : dict {symbol: int}.
    Tolérant : retourne ([], {}) si absent/corrompu.
    """
    try:
        raw = json.loads(scheduler_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return [], {}
        watches_raw = raw.get("indicator_watches") or {}
        if isinstance(watches_raw, dict):
            watches = [v for v in watches_raw.values() if isinstance(v, dict)]
        elif isinstance(watches_raw, list):
            watches = [v for v in watches_raw if isinstance(v, dict)]
        else:
            watches = []
        streaks = raw.get("stale_streaks") or {}
        if not isinstance(streaks, dict):
            streaks = {}
        return watches, streaks
    except Exception:
        return [], {}


def _load_indicator_watches_safe(scheduler_path: Path) -> list[dict]:
    """Raccourci : ne retourne que les watches."""
    watches, _ = _load_scheduler_data_safe(scheduler_path)
    return watches
```

- [ ] **Step 4 : Vérifier que les tests reader passent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui_builders_v2.py -k "indicator_watches or scheduler_data" -v
```

Attendu : 4 PASSED.

### Sous-tâche 3c : Tests + builder watches

- [ ] **Step 5 : Écrire les tests du builder watches**

Ajouter dans `tests/test_tui_builders_v2.py` :

```python
# ---------------------------------------------------------------------------
# Task 3 — builder _build_watches_panel
# ---------------------------------------------------------------------------


def test_build_watches_panel_avec_watches_contient_symbol() -> None:
    from trader.tui import _build_watches_panel
    watches = [{
        "id": "AAPL:abc", "symbol": "AAPL",
        "expires_at": "2026-06-10T14:00:00Z",
        "logic": "any",
        "conditions": [{"indicator": "z_score", "op": "abs>=", "value": 2.0, "timeframe": "1h"}],
    }]
    result = _build_watches_panel(watches)
    output = _render(result)
    assert "AAPL" in output


def test_build_watches_panel_affiche_condition_compacte() -> None:
    from trader.tui import _build_watches_panel
    watches = [{
        "id": "X:1", "symbol": "X",
        "expires_at": "2026-06-10T12:00:00Z",
        "logic": "any",
        "conditions": [{"indicator": "rsi", "op": ">=", "value": 70.0, "timeframe": "4h"}],
    }]
    output = _render(_build_watches_panel(watches))
    # La condition doit être présente sous forme compacte
    assert "rsi" in output
    assert "70" in output


def test_build_watches_panel_vide_affiche_dim() -> None:
    from trader.tui import _build_watches_panel
    result = _build_watches_panel([])
    output = _render(result)
    # Pas de crash, et un message dim
    assert output is not None
    assert len(output.strip()) > 0


def test_build_watches_panel_avec_light_ne_plante_pas() -> None:
    from trader.tui import _build_watches_panel
    result = _build_watches_panel([], palette=PALETTE_LIGHT)
    output = _render(result)
    assert output is not None


def test_build_watches_panel_retourne_panel() -> None:
    from trader.tui import _build_watches_panel
    assert isinstance(_build_watches_panel([]), Panel)
```

- [ ] **Step 6 : Vérifier échec**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui_builders_v2.py -k "watches_panel" -v
```

Attendu : ImportError.

### Sous-tâche 3d : Implémentation builder watches

- [ ] **Step 7 : Ajouter `_build_watches_panel` dans `trader/tui.py`**

```python
def _build_watches_panel(
    watches: list[dict], *, palette: Palette = PALETTE_DARK
) -> Panel:
    """Veilles actives (colonne droite, sous logs).

    Source : _load_indicator_watches_safe(state/scheduler.json).
    """
    if not watches:
        return Panel(
            Text("aucune veille active", style=palette["dim"]),
            title="[bold]Veilles[/bold]",
            border_style=palette["border_watches"],
            expand=True,
        )

    now_utc = datetime.now(UTC)
    lines: list[Text] = []
    for watch in watches:
        symbol = str(watch.get("symbol", "?"))
        expires_raw = str(watch.get("expires_at") or "")
        logic = str(watch.get("logic", "any"))
        conditions = _safe_list_of_dicts(watch.get("conditions") or [])

        # Expiration relative
        expire_str = "?"
        try:
            candidate = (
                f"{expires_raw[:-1]}+00:00"
                if expires_raw.endswith("Z")
                else expires_raw
            )
            exp_dt = datetime.fromisoformat(candidate)
            if exp_dt.tzinfo is None:
                from datetime import timezone as _tz
                exp_dt = exp_dt.replace(tzinfo=_tz.utc)
            delta = exp_dt - now_utc
            total_secs = int(delta.total_seconds())
            if total_secs < 0:
                expire_str = "expiré"
            else:
                hours, rem = divmod(total_secs, 3600)
                minutes = rem // 60
                expire_str = f"{hours}h{minutes:02d}" if hours > 0 else f"{minutes}min"
            expire_str = f"dans {expire_str}"
        except Exception:
            expire_str = "?"

        # Conditions compactes
        cond_parts: list[str] = []
        for cond in conditions[:3]:  # max 3 conditions affichées
            ind = str(cond.get("indicator") or "?")
            op = str(cond.get("op") or "?")
            val = cond.get("value")
            tf = str(cond.get("timeframe") or cond.get("interval") or "?")
            val_str = f"{val}" if val is not None else "?"
            cond_parts.append(f"{ind}{op}{val_str}@{tf}")
        cond_str = f" [{logic}] ".join(cond_parts) if cond_parts else "?"

        line = Text.assemble(
            (symbol, f"bold {palette['kpi_default']}"),
            ("  ", ""),
            (cond_str, palette["dim"]),
            ("  ", ""),
            (expire_str, palette["kpi_vol_warn"]),
        )
        lines.append(line)

    return Panel(
        Group(*lines),
        title="[bold]Veilles[/bold]",
        border_style=palette["border_watches"],
        expand=True,
    )
```

- [ ] **Step 8 : Vérifier que les tests passent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui_builders_v2.py -k "watches" -v
```

Attendu : 5 PASSED.

---

## Task 4 : Reader `_tail_decisions_safe` + builder `_build_data_health_panel`

**Files:**
- Modify: `trader/tui.py`
- Modify: `tests/test_tui_builders_v2.py`

### Sous-tâche 4a : Tests reader tail decisions borné

- [ ] **Step 1 : Écrire les tests tail decisions**

```python
# ---------------------------------------------------------------------------
# Task 4 — reader _tail_decisions_safe (borné)
# ---------------------------------------------------------------------------


def test_tail_decisions_safe_lit_les_n_dernieres_lignes(tmp_path) -> None:
    import json as _json
    from trader.tui import _tail_decisions_safe
    f = tmp_path / "decisions.jsonl"
    rows = [{"symbol": f"SYM{i}", "action": "HOLD", "runtime": {}} for i in range(100)]
    f.write_text("\n".join(_json.dumps(r) for r in rows), encoding="utf-8")
    result = _tail_decisions_safe(f, n=10)
    assert len(result) == 10
    # Les 10 dernières → SYM90 à SYM99
    symbols = [r["symbol"] for r in result]
    assert "SYM99" in symbols
    assert "SYM0" not in symbols


def test_tail_decisions_safe_gros_fichier_lit_n_lignes(tmp_path) -> None:
    """500 lignes → seules 50 dernières lues."""
    import json as _json
    from trader.tui import _tail_decisions_safe
    f = tmp_path / "decisions.jsonl"
    rows = [{"symbol": f"S{i}", "runtime": {"data_source": f"src{i}"}} for i in range(500)]
    f.write_text("\n".join(_json.dumps(r) for r in rows), encoding="utf-8")
    result = _tail_decisions_safe(f, n=50)
    assert len(result) == 50
    assert result[-1]["symbol"] == "S499"
    assert result[0]["symbol"] == "S450"


def test_tail_decisions_safe_fichier_absent_retourne_vide(tmp_path) -> None:
    from trader.tui import _tail_decisions_safe
    result = _tail_decisions_safe(tmp_path / "nope.jsonl")
    assert result == []


def test_tail_decisions_safe_lignes_corrompues_ignorees(tmp_path) -> None:
    import json as _json
    from trader.tui import _tail_decisions_safe
    f = tmp_path / "decisions.jsonl"
    lines = [
        _json.dumps({"symbol": "A", "runtime": {}}),
        "not json",
        _json.dumps({"symbol": "B", "runtime": {"data_source": "live"}}),
    ]
    f.write_text("\n".join(lines), encoding="utf-8")
    result = _tail_decisions_safe(f)
    symbols = [r["symbol"] for r in result]
    assert "A" in symbols
    assert "B" in symbols
    assert len(result) == 2
```

- [ ] **Step 2 : Vérifier échec**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui_builders_v2.py -k "tail_decisions" -v
```

Attendu : ImportError.

### Sous-tâche 4b : Implémentation reader tail borné

- [ ] **Step 3 : Ajouter `_tail_decisions_safe` dans `trader/tui.py`**

Après `_load_indicator_watches_safe` :

```python
def _tail_decisions_safe(decisions_path: Path, *, n: int = 50) -> list[dict]:
    """Lit les n dernières lignes de decisions.jsonl sans tout charger.

    Algorithme tail : lit par blocs de 8192 octets depuis la fin, s'arrête
    quand n lignes valides collectées. Jamais d'exception.
    """
    try:
        if not decisions_path.exists():
            return []
        size = decisions_path.stat().st_size
        if size == 0:
            return []
        chunk_size = 8192
        collected: list[str] = []
        with decisions_path.open("rb") as fh:
            pos = size
            remainder = b""
            while pos > 0 and len(collected) < n:
                read_size = min(chunk_size, pos)
                pos -= read_size
                fh.seek(pos)
                chunk = fh.read(read_size) + remainder
                lines_raw = chunk.split(b"\n")
                remainder = lines_raw[0]
                for line_bytes in reversed(lines_raw[1:]):
                    stripped = line_bytes.strip()
                    if stripped:
                        collected.append(stripped.decode("utf-8", errors="replace"))
                        if len(collected) >= n:
                            break
            # Dernier remainder
            if len(collected) < n and remainder.strip():
                collected.append(remainder.strip().decode("utf-8", errors="replace"))
        # collected est en ordre inversé
        result: list[dict] = []
        for raw_line in reversed(collected[:n]):
            try:
                obj = json.loads(raw_line)
                if isinstance(obj, dict):
                    result.append(obj)
            except Exception:
                continue
        return result
    except Exception:
        return []
```

- [ ] **Step 4 : Vérifier que les tests passent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui_builders_v2.py -k "tail_decisions" -v
```

Attendu : 4 PASSED.

### Sous-tâche 4c : Tests + builder `_build_data_health_panel`

- [ ] **Step 5 : Écrire les tests du builder**

```python
# ---------------------------------------------------------------------------
# Task 4 — builder _build_data_health_panel
# ---------------------------------------------------------------------------


def test_build_data_health_panel_avec_streaks_affiche_symbole() -> None:
    from trader.tui import _build_data_health_panel
    stale_streaks = {"SPY": 3, "QQQ": 1}
    recent_decisions = [
        {"symbol": "SPY", "runtime": {"data_source": "backfill"}},
        {"symbol": "QQQ", "runtime": {"data_source": "live"}},
    ]
    result = _build_data_health_panel(recent_decisions, stale_streaks)
    output = _render(result)
    assert "SPY" in output


def test_build_data_health_panel_streak_positif_affiche_backoff() -> None:
    from trader.tui import _build_data_health_panel
    stale_streaks = {"AAPL": 2}
    recent_decisions = [{"symbol": "AAPL", "runtime": {"data_source": "backfill"}}]
    result = _build_data_health_panel(recent_decisions, stale_streaks)
    output = _render(result)
    # Doit mentionner le backoff
    assert "backoff" in output.lower() or "×2" in output or "2" in output


def test_build_data_health_panel_vide_retourne_panel() -> None:
    from trader.tui import _build_data_health_panel
    result = _build_data_health_panel([], {})
    assert isinstance(result, Panel)


def test_build_data_health_panel_avec_light_ne_plante_pas() -> None:
    from trader.tui import _build_data_health_panel
    result = _build_data_health_panel([], {}, palette=PALETTE_LIGHT)
    output = _render(result)
    assert output is not None
```

- [ ] **Step 6 : Vérifier échec**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui_builders_v2.py -k "data_health" -v
```

Attendu : ImportError.

### Sous-tâche 4d : Implémentation builder data_health

- [ ] **Step 7 : Ajouter `_build_data_health_panel` dans `trader/tui.py`**

```python
def _build_data_health_panel(
    recent_decisions: list[dict],
    stale_streaks: dict,
    *,
    palette: Palette = PALETTE_DARK,
) -> Panel:
    """Santé data (droite, compact).

    Par symbole avec streaks > 0 ou data_source non-None dans les décisions
    récentes : une ligne {symbol} {data_source} [backoff ×N si streak > 0].
    """
    # Collecter data_source par symbole depuis les décisions récentes (dernier vu)
    ds_by_symbol: dict[str, str | None] = {}
    for dec in recent_decisions:
        sym = str(dec.get("symbol") or "")
        if not sym:
            continue
        runtime = dec.get("runtime") if isinstance(dec.get("runtime"), dict) else {}
        ds = runtime.get("data_source") if isinstance(runtime, dict) else None
        ds_by_symbol[sym] = str(ds) if ds is not None else None

    # Tous les symboles concernés = union(streaks > 0, ds non-None)
    symbols_concerned: set[str] = set()
    for sym, streak in (stale_streaks or {}).items():
        try:
            if int(streak) > 0:
                symbols_concerned.add(sym)
        except (TypeError, ValueError):
            pass
    for sym, ds in ds_by_symbol.items():
        if ds is not None:
            symbols_concerned.add(sym)

    if not symbols_concerned:
        return Panel(
            Text("—", style=palette["dim"]),
            title="[bold]Santé data[/bold]",
            border_style=palette["border_default"],
            expand=True,
        )

    table = Table(box=None, show_header=False, expand=True, pad_edge=False)
    table.add_column("sym", style="bold", no_wrap=True)
    table.add_column("source", no_wrap=True)
    table.add_column("streak", justify="right", no_wrap=True)

    for sym in sorted(symbols_concerned):
        ds = ds_by_symbol.get(sym)
        ds_str = str(ds) if ds is not None else "—"
        streak = stale_streaks.get(sym, 0)
        try:
            streak_int = int(streak)
        except (TypeError, ValueError):
            streak_int = 0
        if streak_int > 0:
            streak_cell = Text(f"backoff ×{streak_int}", style=palette["kpi_vol_warn"])
        else:
            streak_cell = Text("ok", style=palette["pnl_positive"])
        table.add_row(sym, ds_str, streak_cell)

    return Panel(
        table,
        title="[bold]Santé data[/bold]",
        border_style=palette["border_default"],
        expand=True,
    )
```

- [ ] **Step 8 : Vérifier que les tests passent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui_builders_v2.py -k "data_health" -v
```

Attendu : 4 PASSED.

---

## Task 5 : Builder `_build_llm_activity_panel`

**Files:**
- Modify: `trader/tui.py`
- Modify: `tests/test_tui_builders_v2.py`

- [ ] **Step 1 : Écrire les tests**

```python
# ---------------------------------------------------------------------------
# Task 5 — builder _build_llm_activity_panel
# ---------------------------------------------------------------------------


def test_build_llm_activity_panel_affiche_appels_modele() -> None:
    from trader.tui import _build_llm_activity_panel
    daemon_status = {"model_calls_used": 5, "max_model_calls_per_cycle": 25}
    result = _build_llm_activity_panel(daemon_status, 12, None)
    output = _render(result)
    assert "5" in output
    assert "25" in output


def test_build_llm_activity_panel_affiche_learnings_en_attente() -> None:
    from trader.tui import _build_llm_activity_panel
    result = _build_llm_activity_panel({}, 42, None)
    output = _render(result)
    assert "42" in output


def test_build_llm_activity_panel_affiche_consolidation_status(tmp_path) -> None:
    from trader.tui import _build_llm_activity_panel
    import json as _json
    consolidation = {"status": "success", "ts": "2026-06-10T05:00:00Z", "count": 3}
    result = _build_llm_activity_panel({}, 0, consolidation)
    output = _render(result)
    assert "success" in output or "3" in output or "ok" in output.lower()


def test_build_llm_activity_panel_sans_donnees_ne_crashe_pas() -> None:
    from trader.tui import _build_llm_activity_panel
    result = _build_llm_activity_panel({}, 0, None)
    assert isinstance(result, Panel)


def test_build_llm_activity_panel_avec_light_ne_plante_pas() -> None:
    from trader.tui import _build_llm_activity_panel
    result = _build_llm_activity_panel({}, 0, None, palette=PALETTE_LIGHT)
    output = _render(result)
    assert output is not None
```

- [ ] **Step 2 : Vérifier échec**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui_builders_v2.py -k "llm_activity" -v
```

Attendu : ImportError.

- [ ] **Step 3 : Ajouter `_load_consolidation_status_safe` + `_build_llm_activity_panel` dans `trader/tui.py`**

```python
def _load_consolidation_status_safe(status_path: Path) -> dict | None:
    """Lit learnings_consolidation_status.json. Retourne None si absent/corrompu."""
    try:
        raw = json.loads(status_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except Exception:
        return None


def _count_learnings_safe(learnings_path: Path, *, limit: int = 200) -> int:
    """Compte les lignes non vides de learnings.jsonl sans tout charger (limité)."""
    try:
        if not learnings_path.exists():
            return 0
        count = 0
        with learnings_path.open(encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i >= limit:
                    return limit  # tronqué
                if line.strip():
                    count += 1
        return count
    except Exception:
        return 0


def _build_llm_activity_panel(
    daemon_status: dict,
    learnings_pending: int,
    consolidation_status: dict | None,
    *,
    palette: Palette = PALETTE_DARK,
) -> Panel:
    """Activité LLM (droite, compact).

    Affiche : appels modèle du cycle, learnings bruts en attente,
    dernier état de consolidation.
    """
    used = daemon_status.get("model_calls_used")
    max_calls = daemon_status.get("max_model_calls_per_cycle")
    calls_str = (
        f"{used}/{max_calls}"
        if used is not None and max_calls is not None
        else (str(used) if used is not None else "—")
    )

    # Consolidation status
    if consolidation_status is not None:
        consol_status = str(consolidation_status.get("status") or "?")
        consol_ts = _format_datetime(consolidation_status.get("ts"))
        consol_count = consolidation_status.get("count")
        consol_str = f"{consol_status}"
        if consol_count is not None:
            consol_str += f" ({consol_count} entrées)"
        consol_style = (
            palette["pnl_positive"]
            if "success" in consol_status.lower()
            else (
                palette["pnl_negative"]
                if "fail" in consol_status.lower() or "error" in consol_status.lower()
                else palette["dim"]
            )
        )
    else:
        consol_str = "—"
        consol_ts = "—"
        consol_style = palette["dim"]

    content = Text.assemble(
        ("Appels cycle: ", palette["dim"]),
        (calls_str, f"bold {palette['kpi_default']}"),
        ("  Learnings bruts: ", palette["dim"]),
        (str(learnings_pending), f"bold {palette['kpi_default']}"),
        "\n",
        ("Consolidation: ", palette["dim"]),
        (consol_str, consol_style),
        ("  ", ""),
        (consol_ts, palette["dim"]),
    )

    return Panel(
        content,
        title="[bold]LLM[/bold]",
        border_style=palette["border_llm_activity"],
        expand=True,
    )
```

- [ ] **Step 4 : Vérifier que les tests passent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui_builders_v2.py -k "llm_activity" -v
```

Attendu : 5 PASSED.

---

## Task 6 : Colonne `data_source` dans `_build_decisions_table`

**Files:**
- Modify: `trader/tui.py` — amender `_build_decisions_table`
- Modify: `tests/test_tui_builders_v2.py`

La spec demande d'ajouter la colonne `data_source` (champ `runtime.data_source`) aux décisions récentes, avec `—` si absent.

> **Important :** le code existant dans `_build_decisions_table` itère sur `decisions` qui sont des dicts provenant de `state["decisions"]`. Ces dicts sont les **décisions du rapport courant** (current_report.json, structure `{symbol, action, qty, rationale, confidence, ...}`) et non les lignes de decisions.jsonl. Le champ `runtime.data_source` vit dans decisions.jsonl. Il faut donc enrichir les dicts de décisions avec `data_source` depuis les décisions récentes chargées par `_tail_decisions_safe`.

L'approche : ajouter une fonction `_enrich_decisions_with_data_source(decisions, recent)` qui, pour chaque décision du rapport, cherche la dernière entrée correspondante dans `recent` (par symbole) et injecte `data_source`.

- [ ] **Step 1 : Écrire les tests**

```python
# ---------------------------------------------------------------------------
# Task 6 — colonne data_source dans _build_decisions_table
# ---------------------------------------------------------------------------


def test_build_decisions_table_avec_data_source_affiche_colonne() -> None:
    from trader.tui import _build_decisions_table
    decisions = [{
        "symbol": "AAPL", "action": "BUY", "qty": 2.0,
        "rationale": "test", "confidence": 0.8,
        "data_source": "live",
    }]
    from rich.console import Console
    console = Console(width=160)
    with console.capture() as cap:
        console.print(_build_decisions_table(decisions))
    output = cap.get()
    assert "live" in output


def test_build_decisions_table_sans_data_source_affiche_tiret() -> None:
    from trader.tui import _build_decisions_table
    decisions = [{
        "symbol": "TSLA", "action": "HOLD", "qty": 0.0,
        "rationale": "wait", "confidence": 0.5,
    }]
    from rich.console import Console
    console = Console(width=160)
    with console.capture() as cap:
        console.print(_build_decisions_table(decisions))
    output = cap.get()
    # data_source absent → tiret dans la colonne
    assert "—" in output


def test_enrich_decisions_avec_recent_injecte_data_source() -> None:
    from trader.tui import _enrich_decisions_with_data_source
    decisions = [{"symbol": "SPY", "action": "BUY"}]
    recent = [
        {"symbol": "SPY", "runtime": {"data_source": "backfill"}},
        {"symbol": "QQQ", "runtime": {"data_source": "live"}},
    ]
    enriched = _enrich_decisions_with_data_source(decisions, recent)
    assert enriched[0].get("data_source") == "backfill"


def test_enrich_decisions_sans_match_laisse_data_source_none() -> None:
    from trader.tui import _enrich_decisions_with_data_source
    decisions = [{"symbol": "UNKNOWN", "action": "HOLD"}]
    recent = [{"symbol": "SPY", "runtime": {"data_source": "live"}}]
    enriched = _enrich_decisions_with_data_source(decisions, recent)
    assert enriched[0].get("data_source") is None
```

- [ ] **Step 2 : Vérifier échec**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui_builders_v2.py -k "data_source" -v
```

Attendu : ImportError pour `_enrich_decisions_with_data_source`; le test `_build_decisions_table` peut passer ou non selon si la colonne existe déjà.

- [ ] **Step 3 : Ajouter `_enrich_decisions_with_data_source` dans `trader/tui.py`**

Après `_tail_decisions_safe` :

```python
def _enrich_decisions_with_data_source(
    decisions: list[dict], recent_decisions: list[dict]
) -> list[dict]:
    """Injecte 'data_source' dans chaque décision du rapport depuis les décisions récentes.

    Pour chaque décision du rapport, cherche la dernière entrée dans
    recent_decisions ayant le même symbole et injecte runtime.data_source.
    Retourne toujours une nouvelle liste (pas de mutation).
    """
    # Index : symbole → data_source le plus récent (dernier dans la liste = le plus récent)
    ds_index: dict[str, str | None] = {}
    for dec in recent_decisions:
        sym = str(dec.get("symbol") or "")
        if not sym:
            continue
        runtime = dec.get("runtime") if isinstance(dec.get("runtime"), dict) else {}
        ds = runtime.get("data_source") if isinstance(runtime, dict) else None
        ds_index[sym] = str(ds) if ds is not None else None

    enriched: list[dict] = []
    for dec in decisions:
        sym = str(dec.get("symbol") or "")
        copy = {**dec}
        if "data_source" not in copy:
            copy["data_source"] = ds_index.get(sym)
        enriched.append(copy)
    return enriched
```

- [ ] **Step 4 : Amender `_build_decisions_table` pour ajouter la colonne `data_source`**

Dans `trader/tui.py`, dans `_build_decisions_table`, après `dec_table.add_column("Confiance", justify="right")`, ajouter :

```python
    dec_table.add_column("Source", style=palette["dim"])
```

Et dans la boucle `for d in decisions`, après `f"{confidence:.2f}"` dans `dec_table.add_row(...)`, ajouter :

```python
            str(d.get("data_source") or "—"),
```

Et dans le bloc `if not decisions`, changer `dec_table.add_row("—", "—", "—", "—", "—")` en :

```python
        dec_table.add_row("—", "—", "—", "—", "—", "—")
```

Lecture du code exact avant édition pour repérer les lignes précises :

La signature actuelle de `_build_decisions_table` :
- Ligne ~492 : `def _build_decisions_table(`
- L'`add_column("Confiance"...)` est la dernière colonne
- Le `add_row` dans la boucle a 5 arguments
- Le `add_row` vide a 5 "—"

Après modification, `_build_decisions_table` accepte le nouveau champ `data_source` dans chaque dict de décision.

- [ ] **Step 5 : Vérifier que les tests passent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui_builders_v2.py -k "data_source" -v
```

Attendu : 4 PASSED.

- [ ] **Step 6 : Vérifier que les tests TUI existants n'ont pas régressé**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui.py tests/test_tui_dashboard.py tests/test_palette.py -q
```

Attendu : toutes passent. Les décisions existantes n'ont pas de clé `data_source` → affichent `—`, ce qui est correct.

---

## Task 7 : Amender `load_runtime_state` pour les nouvelles données

**Files:**
- Modify: `trader/tui.py` — `load_runtime_state`
- Modify: `tests/test_tui_builders_v2.py`

`load_runtime_state` doit charger les nouvelles données et les injecter dans le dict de retour sous les clés :
- `"trade_plans"` → `list[dict]` (from `trade_plans.json`)
- `"indicator_watches"` → `list[dict]` (from `scheduler.json`)
- `"stale_streaks"` → `dict` (from `scheduler.json`)
- `"recent_decisions"` → `list[dict]` (tail de `decisions.jsonl`, n=50)
- `"consolidation_status"` → `dict | None`
- `"learnings_pending_count"` → `int`

Ces lectures se font dans le thread worker existant (pas de nouveau thread).

- [ ] **Step 1 : Écrire les tests**

```python
# ---------------------------------------------------------------------------
# Task 7 — load_runtime_state enrichi
# ---------------------------------------------------------------------------


def test_load_runtime_state_inclut_trade_plans(tmp_path) -> None:
    import json as _json
    from trader.tui import load_runtime_state
    (tmp_path / "trade_plans.json").write_text(
        _json.dumps({"plans": [{"symbol": "AAPL", "id": "x", "side": "LONG",
                                "entry_price": 100.0, "remaining_quantity": 1.0,
                                "quantity": 1.0, "opened_at": "2026-06-10T10:00:00Z"}]}),
        encoding="utf-8"
    )
    state = load_runtime_state(state_dir=tmp_path)
    assert "trade_plans" in state
    assert isinstance(state["trade_plans"], list)
    assert len(state["trade_plans"]) == 1


def test_load_runtime_state_inclut_indicator_watches(tmp_path) -> None:
    import json as _json
    from trader.tui import load_runtime_state
    sched = {"indicator_watches": {"A:1": {"id": "A:1", "symbol": "A",
             "expires_at": "2026-06-10T12:00:00Z", "logic": "any",
             "conditions": []}}, "stale_streaks": {"X": 2}}
    (tmp_path / "scheduler.json").write_text(_json.dumps(sched), encoding="utf-8")
    state = load_runtime_state(state_dir=tmp_path)
    assert "indicator_watches" in state
    assert isinstance(state["indicator_watches"], list)
    assert "stale_streaks" in state
    assert state["stale_streaks"].get("X") == 2


def test_load_runtime_state_inclut_recent_decisions(tmp_path) -> None:
    import json as _json
    from trader.tui import load_runtime_state
    rows = [{"symbol": "SPY", "action": "HOLD", "runtime": {"data_source": "live"}}] * 5
    (tmp_path / "decisions.jsonl").write_text(
        "\n".join(_json.dumps(r) for r in rows), encoding="utf-8"
    )
    state = load_runtime_state(state_dir=tmp_path)
    assert "recent_decisions" in state
    assert isinstance(state["recent_decisions"], list)
    assert len(state["recent_decisions"]) == 5


def test_load_runtime_state_trade_plans_absent_retourne_vide(tmp_path) -> None:
    from trader.tui import load_runtime_state
    state = load_runtime_state(state_dir=tmp_path)
    assert state.get("trade_plans") == []
    assert state.get("indicator_watches") == []
    assert state.get("stale_streaks") == {}
    assert state.get("recent_decisions") == []
    assert state.get("learnings_pending_count") == 0
    assert state.get("consolidation_status") is None
```

- [ ] **Step 2 : Vérifier échec**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui_builders_v2.py -k "load_runtime_state" -v
```

Attendu : FAILED (clés absentes).

- [ ] **Step 3 : Amender `load_runtime_state` dans `trader/tui.py`**

Ajouter dans le corps de `load_runtime_state`, juste après `learnings = _load_learnings_safe(state_dir_path)` :

```python
    trade_plans = _load_trade_plans_safe(state_dir_path / "trade_plans.json")
    indicator_watches, stale_streaks = _load_scheduler_data_safe(
        state_dir_path / "scheduler.json"
    )
    recent_decisions = _tail_decisions_safe(
        state_dir_path / "decisions.jsonl", n=50
    )
    consolidation_status = _load_consolidation_status_safe(
        state_dir_path / "learnings_consolidation_status.json"
    )
    learnings_pending_count = _count_learnings_safe(
        state_dir_path / "learnings.jsonl"
    )
```

Et dans le `return { ... }`, ajouter :

```python
        "trade_plans": trade_plans,
        "indicator_watches": indicator_watches,
        "stale_streaks": stale_streaks,
        "recent_decisions": recent_decisions,
        "consolidation_status": consolidation_status,
        "learnings_pending_count": learnings_pending_count,
```

- [ ] **Step 4 : Vérifier que les tests passent**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui_builders_v2.py -k "load_runtime_state" -v
```

Attendu : 4 PASSED.

- [ ] **Step 5 : Suite TUI complète — pas de régression**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_tui.py tests/test_tui_dashboard.py tests/test_palette.py tests/test_tui_builders_v2.py -q
```

Attendu : toutes passent.

---

## Task 8 : Refactoring cockpit.py — layout 3 colonnes

**Files:**
- Modify: `trader/cockpit.py`
- Modify: `tests/test_cockpit_smoke.py`

**Contraintes strictes :**
- Bindings `q/c/f/l/d/s/X/k` INCHANGÉS.
- `l` toggle toujours la colonne logs entière (EventsPane droite).
- `d` re-rend les nouveaux panneaux aussi.
- Parties interdites du fichier : ne pas toucher aux modals `ConfirmStop`/`ConfirmKill`, aux thèmes Textual, aux actions `action_start_daemon`, `action_stop_daemon_confirm`, `action_toggle_kill`, ni à `CockpitStatus`.

### Architecture CSS 3 colonnes

```
Screen (vertical)
└── CockpitStatus           ← inchangé
└── #main-body (horizontal)
    ├── LeftPane (25%)       ← nouveau widget (remplace partie DashboardPane)
    │   ├── #equity-panel
    │   ├── #positions-panel
    │   ├── #exit-plans-panel  ← nouveau
    │   └── #learnings-panel
    ├── CenterPane (40%)     ← nouveau widget
    │   ├── #kpi-band
    │   ├── #decisions-table   ← grand, panneau roi
    │   └── #attribution-panel
    └── RightPane (35%)      ← contient EventsPane + panneaux compacts bas
        ├── EventsPane (60% de la hauteur de RightPane)
        ├── #watches-panel     ← nouveau
        ├── #data-health-panel ← nouveau
        └── #llm-activity-panel ← nouveau
Footer
```

### Sous-tâche 8a : Smoke tests v2

- [ ] **Step 1 : Ajouter des smoke tests v2 dans `tests/test_cockpit_smoke.py`**

Ajouter à la fin du fichier :

```python
# ---------------------------------------------------------------------------
# Layout v2 — 3 colonnes
# ---------------------------------------------------------------------------


async def test_cockpit_v2_pane_left_existe(tmp_path, monkeypatch):
    """Le layout v2 expose un pane gauche avec le panneau equity."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as _:
        assert app.query_one("#left-pane") is not None


async def test_cockpit_v2_pane_center_existe(tmp_path, monkeypatch):
    """Le layout v2 expose un pane central."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as _:
        assert app.query_one("#center-pane") is not None


async def test_cockpit_v2_pane_right_existe(tmp_path, monkeypatch):
    """Le layout v2 expose un pane droit."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as _:
        assert app.query_one("#right-pane") is not None


async def test_cockpit_v2_toggle_l_masque_right_pane(tmp_path, monkeypatch):
    """Le toggle l masque le pane droit (logs + panneaux compacts)."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        right = app.query_one("#right-pane")
        assert right.display is True
        await pilot.press("l")
        assert right.display is False
        await pilot.press("l")
        assert right.display is True


async def test_cockpit_v2_exit_plans_panel_existe(tmp_path, monkeypatch):
    """Le panneau #exit-plans-panel est dans le DOM."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as _:
        assert app.query_one("#exit-plans-panel") is not None


async def test_cockpit_v2_toggle_d_rerender_nouveaux_panneaux(tmp_path, monkeypatch):
    """La touche d bascule le thème sans crash avec les nouveaux panneaux."""
    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        await pilot.press("d")
        assert app.theme == "casys-ink"
        await pilot.press("d")
        assert app.theme == "casys-salmon"
```

- [ ] **Step 2 : Vérifier échec (panes v2 absents)**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_cockpit_smoke.py -k "v2" -v
```

Attendu : FAILED (NoMatches — les panes v2 n'existent pas encore).

### Sous-tâche 8b : Implémentation layout 3 colonnes dans cockpit.py

- [ ] **Step 3 : Remplacer `DashboardPane` par 3 nouveaux widgets dans `trader/cockpit.py`**

**Imports à ajouter** (en haut de `trader/cockpit.py`, après les imports existants de tui) :

```python
from trader.tui import (
    # ... imports existants ...
    _build_exit_plans_panel,
    _build_watches_panel,
    _build_data_health_panel,
    _build_llm_activity_panel,
    _enrich_decisions_with_data_source,
    _load_trade_plans_safe,
    _load_scheduler_data_safe,
    _tail_decisions_safe,
    _load_consolidation_status_safe,
    _count_learnings_safe,
)
```

**Remplacer `DashboardPane`** par trois classes `LeftPane`, `CenterPane`, `RightPane`.

`LeftPane` (25%) — équité, positions, exit-plans, learnings :

```python
class LeftPane(Static):
    DEFAULT_CSS = """
    LeftPane {
        width: 25%;
        height: 100%;
        border-right: solid $primary;
        overflow-y: auto;
    }
    """
    _current_palette: Palette = PALETTE_LIGHT

    def compose(self) -> ComposeResult:
        yield Static(id="equity-panel")
        yield Static(id="positions-panel")
        yield Static(id="exit-plans-panel")
        yield Static(id="learnings-panel")

    def update_state(self, state: dict) -> None:
        palette = self._current_palette
        portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
        equity_curve_raw = state.get("equity_curve") or []
        equity_curve = [v for v in (_safe_float(x, default=None) for x in equity_curve_raw) if v is not None]
        holdings = _safe_list_of_dicts(portfolio.get("holdings"))
        learnings = _safe_list_of_dicts(state.get("learnings"))
        trade_plans = state.get("trade_plans") if isinstance(state.get("trade_plans"), list) else []

        self.query_one("#equity-panel", Static).update(_build_equity_panel(equity_curve, palette=palette))
        self.query_one("#positions-panel", Static).update(_build_positions_panel(holdings, palette=palette))
        self.query_one("#exit-plans-panel", Static).update(_build_exit_plans_panel(trade_plans, palette=palette))
        learnings_renderable = _build_learnings_panel(learnings, palette=palette)
        self.query_one("#learnings-panel", Static).update(learnings_renderable if learnings_renderable is not None else Text(""))
```

`CenterPane` (40%) — kpi-band, decisions-table (grand), attribution :

```python
class CenterPane(Static):
    DEFAULT_CSS = """
    CenterPane {
        width: 40%;
        height: 100%;
        border-right: solid $primary;
        overflow-y: auto;
    }
    """
    _current_palette: Palette = PALETTE_LIGHT

    def compose(self) -> ComposeResult:
        yield Static(id="kpi-band")
        yield Static(id="decisions-table")
        yield Static(id="attribution-panel")

    def update_state(self, state: dict) -> None:
        palette = self._current_palette
        kpis = state.get("kpis") if isinstance(state.get("kpis"), dict) else {}
        attribution = state.get("attribution") if isinstance(state.get("attribution"), dict) else {}
        decisions_raw = _safe_list_of_dicts(state.get("decisions"))
        recent_decisions = state.get("recent_decisions") if isinstance(state.get("recent_decisions"), list) else []
        decisions = _enrich_decisions_with_data_source(decisions_raw, recent_decisions)

        self.query_one("#kpi-band", Static).update(_build_kpi_band(kpis, palette=palette))
        self.query_one("#decisions-table", Static).update(_build_decisions_table(decisions, palette=palette))
        self.query_one("#attribution-panel", Static).update(_build_attribution_panel(attribution, palette=palette))
```

`RightPane` (35%) — EventsPane en haut + panneaux compacts bas :

```python
class RightPane(Static):
    DEFAULT_CSS = """
    RightPane {
        width: 35%;
        height: 100%;
        layout: vertical;
    }
    RightPane EventsPane {
        height: 60%;
    }
    RightPane #compact-bottom {
        height: 40%;
        overflow-y: auto;
        layout: vertical;
    }
    """
    _current_palette: Palette = PALETTE_LIGHT

    def compose(self) -> ComposeResult:
        yield EventsPane(id="events-pane")
        with Vertical(id="compact-bottom"):
            yield Static(id="watches-panel")
            yield Static(id="data-health-panel")
            yield Static(id="llm-activity-panel")

    def update_state(self, state: dict) -> None:
        palette = self._current_palette
        indicator_watches = state.get("indicator_watches") if isinstance(state.get("indicator_watches"), list) else []
        stale_streaks = state.get("stale_streaks") if isinstance(state.get("stale_streaks"), dict) else {}
        recent_decisions = state.get("recent_decisions") if isinstance(state.get("recent_decisions"), list) else []
        daemon_status = state.get("daemon_status") if isinstance(state.get("daemon_status"), dict) else {}
        learnings_pending = state.get("learnings_pending_count") or 0
        consolidation_status = state.get("consolidation_status")

        self.query_one("#watches-panel", Static).update(_build_watches_panel(indicator_watches, palette=palette))
        self.query_one("#data-health-panel", Static).update(_build_data_health_panel(recent_decisions, stale_streaks, palette=palette))
        self.query_one("#llm-activity-panel", Static).update(_build_llm_activity_panel(daemon_status, learnings_pending, consolidation_status, palette=palette))
```

- [ ] **Step 4 : Mettre à jour `CockpitApp` pour le layout 3 colonnes**

Remplacer les méthodes `compose`, `on_mount`, `_propagate_palette`, `_apply_state`, `_poll_events`, `action_toggle_cycles`, `action_toggle_scroll`, `action_toggle_logs`, `action_toggle_theme` dans `CockpitApp`.

**`compose`** — inchangé sauf que le `#main-body` monte 3 panes :

```python
    def compose(self) -> ComposeResult:
        yield CockpitStatus(id="cockpit-status")
        yield Static(id="main-body")
        yield Footer()
```

**`on_mount`** :

```python
    def on_mount(self) -> None:
        self.register_theme(_THEME_SALMON)
        self.register_theme(_THEME_INK)
        self.theme = "casys-salmon"

        body = self.query_one("#main-body", Static)
        body.mount(LeftPane(id="left-pane"))
        body.mount(CenterPane(id="center-pane"))
        body.mount(RightPane(id="right-pane"))

        self.set_interval(2.0, self._schedule_refresh_state)
        self.set_interval(1.0, self._poll_events)
        self._schedule_refresh_state()
        self._poll_events()
```

**`_propagate_palette`** :

```python
    def _propagate_palette(self) -> None:
        palette = self._current_palette()
        for pane_id, cls in (
            ("#left-pane", LeftPane),
            ("#center-pane", CenterPane),
            ("#right-pane", RightPane),
        ):
            try:
                pane = self.query_one(pane_id, cls)
                pane._current_palette = palette
            except Exception:
                pass
        try:
            right: RightPane = self.query_one("#right-pane", RightPane)
            events_pane: EventsPane = right.query_one("#events-pane", EventsPane)
            events_pane._current_palette = palette
        except Exception:
            pass
```

**`_apply_state`** :

```python
    def _apply_state(self, state: dict, kill_active: bool) -> None:
        self._last_state = state
        self._last_kill_active = kill_active
        try:
            palette = self._current_palette()
            status: CockpitStatus = self.query_one("#cockpit-status", CockpitStatus)
            status.update_state(state, kill_active, palette=palette)

            left: LeftPane = self.query_one("#left-pane", LeftPane)
            left._current_palette = palette
            left.update_state(state)

            center: CenterPane = self.query_one("#center-pane", CenterPane)
            center._current_palette = palette
            center.update_state(state)

            right: RightPane = self.query_one("#right-pane", RightPane)
            right._current_palette = palette
            right.update_state(state)
        except Exception:
            pass
```

**`_poll_events`** :

```python
    def _poll_events(self) -> None:
        try:
            right: RightPane = self.query_one("#right-pane", RightPane)
            events_pane: EventsPane = right.query_one("#events-pane", EventsPane)
            events_pane.poll_events(_EVENTS_FILE)
        except Exception:
            pass
```

**`action_toggle_cycles`** et **`action_toggle_scroll`** — cherchent EventsPane via RightPane :

```python
    def action_toggle_cycles(self) -> None:
        try:
            right: RightPane = self.query_one("#right-pane", RightPane)
            right.query_one("#events-pane", EventsPane).toggle_cycles()
        except Exception:
            pass

    def action_toggle_scroll(self) -> None:
        try:
            right: RightPane = self.query_one("#right-pane", RightPane)
            right.query_one("#events-pane", EventsPane).toggle_scroll()
        except Exception:
            pass
```

**`action_toggle_logs`** — masque/affiche RightPane entier (logs + panneaux compacts) :

```python
    def action_toggle_logs(self) -> None:
        try:
            right: RightPane = self.query_one("#right-pane", RightPane)
            left: LeftPane = self.query_one("#left-pane", LeftPane)
            center: CenterPane = self.query_one("#center-pane", CenterPane)
            self._logs_visible = not self._logs_visible
            if self._logs_visible:
                right.display = True
                left.styles.width = "25%"
                center.styles.width = "40%"
            else:
                right.display = False
                left.styles.width = "30%"
                center.styles.width = "70%"
        except Exception:
            pass
```

**`action_toggle_theme`** — inchangé dans sa logique, juste appel `_apply_state` :

```python
    def action_toggle_theme(self) -> None:
        if self.theme == "casys-salmon":
            self.theme = "casys-ink"
        else:
            self.theme = "casys-salmon"
        self._propagate_palette()
        if self._last_state is not None:
            self._apply_state(self._last_state, self._last_kill_active)
```

- [ ] **Step 5 : Supprimer l'ancien `DashboardPane` de `trader/cockpit.py`**

La classe `DashboardPane` est remplacée par `LeftPane`/`CenterPane`/`RightPane`. La supprimer entièrement.

> **Attention :** Vérifier qu'il n'y a aucun autre usage de `DashboardPane` dans le codebase (hormis les imports de tui.py) avant de supprimer.

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  grep -rn "DashboardPane" --include="*.py" .
```

Attendu : seuls `trader/cockpit.py` et `tests/test_cockpit_smoke.py`. Mettre à jour les tests smoke qui importent `DashboardPane` en les migrant vers `LeftPane`/`CenterPane`.

- [ ] **Step 6 : Mettre à jour les imports dans les tests smoke**

Dans `tests/test_cockpit_smoke.py`, le test `test_cockpit_toggle_theme_propage_palette_dashboard_immediatement` importe `DashboardPane`. Mettre à jour :

```python
async def test_cockpit_toggle_theme_propage_palette_dashboard_immediatement(
    tmp_path, monkeypatch
):
    """Après toggle d, LeftPane et CenterPane utilisent la nouvelle palette."""
    from trader.cockpit import LeftPane, CenterPane
    from trader.palette import PALETTE_DARK, PALETTE_LIGHT

    _make_minimal_state(tmp_path)
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")

    app = CockpitApp()
    async with app.run_test(size=(220, 60)) as pilot:
        left = app.query_one("#left-pane", LeftPane)
        assert left._current_palette is PALETTE_LIGHT
        await pilot.press("d")
        assert left._current_palette is PALETTE_DARK
        await pilot.press("d")
        assert left._current_palette is PALETTE_LIGHT
```

- [ ] **Step 7 : Exécuter tous les tests cockpit smoke**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest tests/test_cockpit_smoke.py -q
```

Attendu : toutes passent (les anciens tests + les 6 nouveaux v2).

---

## Task 9 : Vérification finale — suite complète + ruff

- [ ] **Step 1 : Suite complète**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest -q --no-header 2>&1 | tail -5
```

Attendu : `667+ passed, ... in < 15s` (ou plus avec les nouveaux tests).

- [ ] **Step 2 : Ruff propre**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run ruff check trader/tui.py trader/cockpit.py trader/palette.py \
  tests/test_tui_builders_v2.py tests/test_cockpit_smoke.py
```

Attendu : aucune erreur.

- [ ] **Step 3 : Ruff format check**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run ruff format --check trader/tui.py trader/cockpit.py trader/palette.py \
  tests/test_tui_builders_v2.py tests/test_cockpit_smoke.py
```

Si des erreurs de format, les corriger :

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run ruff format trader/tui.py trader/cockpit.py trader/palette.py \
  tests/test_tui_builders_v2.py tests/test_cockpit_smoke.py
```

- [ ] **Step 4 : Vérifier parité `make tui`**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  python -c "
from trader.tui import build_view
from rich.console import Console
c = Console(width=120)
with c.capture() as cap:
    c.print(build_view({}))
out = cap.get()
assert 'Équité' in out or 'quit' in out, 'build_view vide produit du contenu'
print('OK — parité build_view préservée')
"
```

Attendu : `OK — parité build_view préservée`.

- [ ] **Step 5 : Re-run suite complète après format**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader && \
  uv run pytest -q --no-header 2>&1 | tail -5
```

Attendu : toujours vert.

---

## Self-review checklist

### Spec coverage

| Exigence spec | Task |
|---|---|
| Layout 3 colonnes (gauche 25%, centre 40%, droite 35%) | Task 8 |
| Panel roi décisions récentes au centre | Task 8 |
| Attribution sous décisions au centre | Task 8 |
| Équité sparkline colonne gauche | Task 8 |
| Positions+PnL colonne gauche | Task 8 |
| Apprentissages colonne gauche | Task 8 |
| Logs live colonne droite ≥60% | Task 8 |
| Panneau plans de sortie | Tasks 2 + 8 |
| Panneau veilles actives | Tasks 3 + 8 |
| Panneau santé data | Tasks 4 + 8 |
| Panneau activité LLM | Tasks 5 + 8 |
| Colonne data_source dans décisions | Task 6 |
| Tokens palette DARK/LIGHT nouveaux | Task 1 |
| Lecture bornée decisions.jsonl (tail) | Task 4 |
| Lecture tolérante (absent → dim) | Tasks 2-7 |
| Toggle `l` masque colonne droite entière | Task 8 |
| Toggle `d` re-rend nouveaux panneaux | Task 8 |
| Bindings existants inchangés | Task 8 |
| Builders palettisés DARK+LIGHT | Tasks 2-5 |
| Tests builder : données réalistes, vides, absentes | Tasks 2-5 |
| Smoke pilot : app monte, panes existent | Task 8 |
| Suite pytest ≥667 verte | Task 9 |
| Ruff propre | Task 9 |

### Fichiers interdits — non touchés

- `trader/daemon.py` ✓
- `trader/cockpit_supervisor.py` ✓
- `trader/consolidator.py` ✓
- `tests/test_consolidator.py` ✓
- `tests/test_cli_semantic.py` ✓
- `tests/test_daemon_learnings.py` ✓
- `tests/test_llm.py` ✓
- `README.md` ✓
- `Makefile` ✓

### Cohérence des types

- `_load_trade_plans_safe(Path) -> list[dict]` — utilisé dans `load_runtime_state` et `LeftPane.update_state`
- `_load_scheduler_data_safe(Path) -> tuple[list[dict], dict]` — utilisé dans `load_runtime_state` et `RightPane.update_state`
- `_tail_decisions_safe(Path, n=50) -> list[dict]` — utilisé dans `load_runtime_state` et `CenterPane.update_state` (via enrichissement)
- `_enrich_decisions_with_data_source(list[dict], list[dict]) -> list[dict]` — utilisé dans `CenterPane.update_state`
- `_build_exit_plans_panel(list[dict], *, palette) -> Panel` — importé dans `cockpit.py`, utilisé dans `LeftPane`
- `_build_watches_panel(list[dict], *, palette) -> Panel` — importé dans `cockpit.py`, utilisé dans `RightPane`
- `_build_data_health_panel(list[dict], dict, *, palette) -> Panel` — importé dans `cockpit.py`, utilisé dans `RightPane`
- `_build_llm_activity_panel(dict, int, dict|None, *, palette) -> Panel` — importé dans `cockpit.py`, utilisé dans `RightPane`

### Gaps / limitations notés

1. **`learnings_pending_count`** : `_count_learnings_safe` est borné à 200 lignes. Au-delà → renvoie 200 (valeur tronquée). Acceptable pour l'affichage.
2. **`data_source` en production** : les décisions actuelles ont `runtime.data_source = None`. Le panneau affichera `—` partout jusqu'à ce que le daemon popule le champ.
3. **`learnings_consolidation_status.json`** : fichier absent en production → panneau LLM affiche `—` pour la consolidation. Comportement correct selon la spec.
4. **`build_view` (tui.py)** : non modifiée, parité garantie. Les nouveaux builders ne sont PAS inclus dans `build_view` (qui est le rendu TUI standalone) — ils sont exclusivement dans le cockpit Textual. Ceci est conforme à la spec ("mets-les dans tui.py à côté des autres builders — ils profitent du pattern partagé et la TUI standalone POURRAIT les afficher plus tard").
