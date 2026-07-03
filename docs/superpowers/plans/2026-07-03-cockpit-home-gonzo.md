# Cockpit Home « mode Gonzo » — Plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refondre la home du cockpit en dashboard dense 1 écran (grammaire Gonzo) : statut jamais tronqué, ligne « à surveiller », histogramme d'activité, DataTables navigables + modal détail symbole, flux live filtrable, thème sombre par défaut.

**Architecture:** Couche vue uniquement. Nouveaux agrégats **purs** dans `trader/cockpit/aggregates.py` (zéro I/O, zéro import Textual) ; nouveaux widgets/builders dans `trader/cockpit/home.py` ; `app.py` fait le wiring. Le read model (`load_runtime_state`) reste la source unique (1 ajout : `sessions`). Spec : `docs/superpowers/specs/2026-07-03-cockpit-home-gonzo-design.md`.

**Tech Stack:** Python 3.11 · Textual ≥8.2.7 (DataTable, ModalScreen) · Rich · plotext ≥5.2 · pytest (asyncio_mode=auto) · uv.

## Global Constraints

- **Aucune nouvelle dépendance** — textual, rich, plotext, pytest-asyncio sont déjà dans `pyproject.toml`.
- `PALETTE_DARK` ne se modifie **jamais** (`trader/ui/palette.py:76`) — elle sert la gen-1 (`trader/ui/tui.py`), qui reste intouchée.
- Boucle UI : **jamais de raise** — tout builder tolère state vide/partiel (valeurs neutres).
- `trader/cockpit/aggregates.py` : fonctions **pures**, aucun import Textual, aucune lecture disque.
- Tests : `uv run pytest tests/<fichier> -v` **sans pipe** (le pipe masque l'exit code) ; suite cockpit complète avant chaque commit : `uv run pytest tests/test_cockpit_smoke.py tests/test_cockpit_events.py tests/test_cockpit_aggregates.py tests/test_cockpit_home.py tests/test_cockpit_interactions.py -v` (ignorer les fichiers pas encore créés).
- Commits par la **boucle principale uniquement** (jamais de git dans un sous-agent) ; review Codex pré-commit à chaque task (skill `acpx`, session nommée fermée après usage).
- Vérification visuelle : script de capture headless (Task 8, réutilisé ensuite) — regarder réellement les PNG.
- Terminal cible : confortable à 200×50, statut dégradable dès ~120 colonnes.

---

### Task 1: Extraire `decision_status` / `decision_priority` / `select_decision_rows` / `parse_ts` dans `aggregates.py`

**Files:**
- Create: `trader/cockpit/aggregates.py`
- Modify: `trader/cockpit/overview.py:451-489` (déléguer `_decision_status`/`_decision_priority`), `trader/cockpit/overview.py:559-565` (déléguer `_select_decision_rows`)
- Create: `tests/test_cockpit_aggregates.py`

**Interfaces:**
- Consumes: `_safe_float`, `_safe_list_of_dicts` (`trader.read_models.runtime_state`).
- Produces: `decision_status(row: dict) -> str` (valeurs : `exec|risk|stale|armé|plan|veille|quiet|hold|signal`) · `decision_priority(row: dict) -> tuple[int, str]` · `select_decision_rows(decisions: list[dict], recent_decisions: list[dict], *, limit: int) -> list[dict]` · `parse_ts(raw: object) -> datetime | None` (UTC-aware, `Z` accepté).

- [ ] **Step 1: Écrire les tests qui échouent**

```python
# tests/test_cockpit_aggregates.py
"""Tests des agrégats purs de la home cockpit (aucune I/O)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# decision_status / parse_ts / select_decision_rows
# ---------------------------------------------------------------------------

def test_decision_status_priorites():
    from trader.cockpit.aggregates import decision_status

    assert decision_status({"executed": True, "action": "BUY"}) == "exec"
    assert decision_status({"reason": "risk:max_exposure"}) == "risk"
    assert decision_status({"reason": "stale_market_data"}) == "stale"
    assert decision_status({"decision_source": "armed_plan"}) == "armé"
    assert decision_status({"runtime": {"trade_plan_created": True}}) == "plan"
    assert decision_status({"runtime": {"indicator_watch_created": True}}) == "veille"
    assert decision_status({"model_called": False, "reason": "quiet_gate"}) == "quiet"
    assert decision_status({"action": "HOLD"}) == "hold"
    assert decision_status({"action": "BUY"}) == "signal"


def test_decision_status_identique_a_overview():
    """Anti-régression : overview délègue au même code."""
    from trader.cockpit import aggregates, overview

    row = {"reason": "risk:x", "action": "SELL"}
    assert overview._decision_status(row) == aggregates.decision_status(row)


def test_parse_ts_z_et_naif_et_invalide():
    from trader.cockpit.aggregates import parse_ts

    aware = parse_ts("2026-07-03T11:59:00Z")
    assert aware is not None and aware.tzinfo is not None
    naive = parse_ts("2026-07-03T11:59:00")
    assert naive is not None and naive.tzinfo is not None
    assert parse_ts("") is None
    assert parse_ts("pas-une-date") is None
    assert parse_ts(None) is None


def test_select_decision_rows_priorise_exec():
    from trader.cockpit.aggregates import select_decision_rows

    rows = [
        {"symbol": "AAA", "action": "HOLD", "ts": "2026-07-03T11:00:00Z"},
        {"symbol": "BBB", "action": "BUY", "executed": True, "ts": "2026-07-03T10:00:00Z"},
    ]
    selected = select_decision_rows([], rows, limit=1)
    assert selected[0]["symbol"] == "BBB"
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_cockpit_aggregates.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'trader.cockpit.aggregates'`

- [ ] **Step 3: Implémenter `aggregates.py` (extraction fidèle) et déléguer depuis `overview.py`**

```python
# trader/cockpit/aggregates.py
"""Agrégats purs pour la home du cockpit.

Aucune I/O, aucun import Textual — tout est calculé depuis le dict d'état
du read model (`load_runtime_state`). Testable sans UI.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from trader.read_models.runtime_state import _safe_float, _safe_list_of_dicts

UTC = timezone.utc


def parse_ts(raw: object) -> datetime | None:
    """ISO 8601 (suffixe Z accepté) → datetime UTC-aware, sinon None."""
    text = str(raw or "").strip()
    if not text:
        return None
    candidate = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def decision_status(row: dict) -> str:
    """Classe une décision (logique historique de overview._decision_status)."""
    reason = str(row.get("reason") or row.get("rationale") or "").lower()
    runtime = row.get("runtime") if isinstance(row.get("runtime"), dict) else {}
    source = str(row.get("decision_source") or "").lower()
    action = str(row.get("action") or "").upper()
    if row.get("executed") is True:
        return "exec"
    if reason.startswith("risk:") or "risk:" in reason:
        return "risk"
    if "stale" in reason:
        return "stale"
    if source == "armed_plan" or runtime.get("armed_plan_id"):
        return "armé"
    if runtime.get("trade_plan_created"):
        return "plan"
    if runtime.get("indicator_watch_created"):
        return "veille"
    if source == "infra" or (row.get("model_called") is False and "quiet" in reason):
        return "quiet"
    return "hold" if action == "HOLD" else "signal"


def decision_priority(row: dict) -> tuple[int, str]:
    """Score de triage (0 = plus urgent) + ts pour départager."""
    action = str(row.get("action") or "").upper()
    status = decision_status(row)
    score = 90
    if row.get("executed") is True and action in {"BUY", "SELL"}:
        score = 0
    elif action in {"BUY", "SELL"} and status in {"risk", "stale"}:
        score = 10
    elif status in {"risk", "stale"}:
        score = 20
    elif status in {"plan", "veille", "armé"}:
        score = 30
    elif action in {"BUY", "SELL"}:
        score = 40
    elif status == "quiet":
        score = 80
    return score, str(row.get("cycle_ts") or row.get("ts") or "")


def _decision_identity(row: dict) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("cycle_ts") or row.get("ts") or ""),
        str(row.get("sequence") or ""),
        str(row.get("symbol") or ""),
        str(row.get("action") or ""),
        str(row.get("reason") or row.get("rationale") or ""),
    )


def decision_source_rows(decisions: list[dict], recent_decisions: list[dict]) -> list[dict]:
    """Fusionne rapport + tail decisions.jsonl en dédupliquant par identité."""
    rows: list[dict] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for row in [*recent_decisions, *decisions]:
        identity = _decision_identity(row)
        if identity in seen:
            continue
        rows.append(row)
        seen.add(identity)
    return rows


def select_decision_rows(
    decisions: list[dict], recent_decisions: list[dict], *, limit: int
) -> list[dict]:
    """Top-N décisions par priorité (les plus récentes d'abord à score égal)."""
    indexed = list(enumerate(decision_source_rows(decisions, recent_decisions)))
    ranked = sorted(indexed, key=lambda item: (decision_priority(item[1])[0], -item[0]))
    return [row for _, row in ranked[:limit]]
```

Dans `trader/cockpit/overview.py`, remplacer les **corps** de `_decision_status` (l.451-470), `_decision_priority` (l.473-489), `_decision_identity` (l.537-544), `_decision_source_rows` (l.547-556) et `_select_decision_rows` (l.559-565) par des délégations (signatures inchangées — les tests existants continuent de passer) :

```python
from trader.cockpit import aggregates as _aggregates

def _decision_status(row: dict) -> str:
    return _aggregates.decision_status(row)

def _decision_priority(row: dict) -> tuple[int, str]:
    return _aggregates.decision_priority(row)

def _decision_identity(row: dict) -> tuple[str, str, str, str, str]:
    return _aggregates._decision_identity(row)

def _decision_source_rows(decisions: list[dict], recent_decisions: list[dict]) -> list[dict]:
    return _aggregates.decision_source_rows(decisions, recent_decisions)

def _select_decision_rows(decisions: list[dict], recent_decisions: list[dict], *, limit: int) -> list[dict]:
    return _aggregates.select_decision_rows(decisions, recent_decisions, limit=limit)
```

- [ ] **Step 4: Vérifier le passage**

Run: `uv run pytest tests/test_cockpit_aggregates.py tests/test_cockpit_smoke.py -v`
Expected: PASS partout (les smoke tests prouvent que la délégation est iso).

- [ ] **Step 5: Commit**

```bash
git add trader/cockpit/aggregates.py trader/cockpit/overview.py tests/test_cockpit_aggregates.py
git commit -m "refactor(cockpit): extrait la classification décisions dans aggregates purs (A1)"
```

---

### Task 2: `activity_buckets` — l'histogramme d'activité

**Files:**
- Modify: `trader/cockpit/aggregates.py`
- Test: `tests/test_cockpit_aggregates.py`

**Interfaces:**
- Produces: `ACTIVITY_STATES: tuple[str, ...] = ("exec", "veille", "plan", "risk", "stale", "hold")` · `activity_buckets(recent_decisions: list[dict], now: datetime, *, window_min: int = 60, bucket_min: int = 5) -> dict[str, list[int]]` — toutes les clés toujours présentes, listes de longueur `window_min // bucket_min`, ordonnées du plus ancien au plus récent.

- [ ] **Step 1: Tests qui échouent** (invariants spec §8.1)

```python
def test_activity_buckets_fenetre_vide():
    from trader.cockpit.aggregates import ACTIVITY_STATES, activity_buckets

    buckets = activity_buckets([], NOW)
    assert set(buckets) == set(ACTIVITY_STATES)
    assert all(len(v) == 12 and sum(v) == 0 for v in buckets.values())


def test_activity_buckets_placement_et_exclusions():
    from trader.cockpit.aggregates import activity_buckets

    rows = [
        # âge 0 → dernier bucket (bord inclus)
        {"executed": True, "action": "BUY", "ts": NOW.isoformat()},
        # âge 30 min → bucket 12 - 1 - 6 = 5
        {"reason": "risk:x", "ts": (NOW - timedelta(minutes=30)).isoformat()},
        # âge 60 min pile → exclu
        {"action": "HOLD", "ts": (NOW - timedelta(minutes=60)).isoformat()},
        # ts illisible → ignoré
        {"action": "HOLD", "ts": "n/a"},
        # futur → ignoré
        {"action": "HOLD", "ts": (NOW + timedelta(minutes=1)).isoformat()},
    ]
    buckets = activity_buckets(rows, NOW)
    assert buckets["exec"][11] == 1
    assert buckets["risk"][5] == 1
    assert sum(buckets["hold"]) == 0


def test_activity_buckets_mapping_series():
    """armé→plan, quiet/signal→hold (spec §4.3)."""
    from trader.cockpit.aggregates import activity_buckets

    ts = NOW.isoformat()
    rows = [
        {"decision_source": "armed_plan", "ts": ts},
        {"model_called": False, "reason": "quiet_gate", "ts": ts},
        {"action": "BUY", "ts": ts},
    ]
    buckets = activity_buckets(rows, NOW)
    assert buckets["plan"][11] == 1
    assert buckets["hold"][11] == 2
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_cockpit_aggregates.py -v -k activity`
Expected: FAIL — `ImportError: cannot import name 'activity_buckets'`

- [ ] **Step 3: Implémentation**

```python
# à ajouter dans trader/cockpit/aggregates.py
ACTIVITY_STATES: tuple[str, ...] = ("exec", "veille", "plan", "risk", "stale", "hold")

_STATUS_TO_SERIES: dict[str, str] = {
    "exec": "exec",
    "veille": "veille",
    "armé": "plan",
    "plan": "plan",
    "risk": "risk",
    "stale": "stale",
    "quiet": "hold",
    "hold": "hold",
    "signal": "hold",
}


def activity_buckets(
    recent_decisions: list[dict],
    now: datetime,
    *,
    window_min: int = 60,
    bucket_min: int = 5,
) -> dict[str, list[int]]:
    """Compte les décisions par état et tranche de temps.

    Retourne {état: [n_buckets ints]}, du plus ancien au plus récent.
    Décisions sans ts parsable, futures, ou d'âge >= window_min : ignorées.
    """
    n_buckets = max(1, window_min // bucket_min)
    series: dict[str, list[int]] = {state: [0] * n_buckets for state in ACTIVITY_STATES}
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    window_seconds = window_min * 60
    for row in _safe_list_of_dicts(recent_decisions):
        ts = parse_ts(row.get("cycle_ts") or row.get("ts"))
        if ts is None:
            continue
        age = (now - ts).total_seconds()
        if age < 0 or age >= window_seconds:
            continue
        index = n_buckets - 1 - int(age // (bucket_min * 60))
        target = _STATUS_TO_SERIES.get(decision_status(row), "hold")
        series[target][index] += 1
    return series
```

- [ ] **Step 4: Vérifier le passage**

Run: `uv run pytest tests/test_cockpit_aggregates.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/cockpit/aggregates.py tests/test_cockpit_aggregates.py
git commit -m "feat(cockpit): activity_buckets — décisions par état × tranche de 5 min (A1)"
```

---

### Task 3: `risk_at_stops` — risque agrégé aux stops

**Files:**
- Modify: `trader/cockpit/aggregates.py`
- Test: `tests/test_cockpit_aggregates.py`

**Interfaces:**
- Produces: `RiskAtStops` (dataclass gelée : `total_usd: float`, `worst_symbol: str | None`, `worst_usd: float`, `without_stop: tuple[str, ...]`) · `risk_at_stops(holdings: list[dict], trade_plans: list[dict]) -> RiskAtStops`. Convention : `risk_usd = (stop − référence) × qté × direction × fx` — négatif = perte si le stop se déclenche (généralise `overview._stop_risk_for_holding`, `overview.py:260`).

- [ ] **Step 1: Tests qui échouent** (invariants spec §8.2)

```python
def test_risk_at_stops_long_short_fx():
    from trader.cockpit.aggregates import risk_at_stops

    holdings = [
        {"symbol": "AAA", "quantity": 10, "last_price": 100.0, "fx_rate": 1.0},
        {"symbol": "BBB", "quantity": -5, "last_price": 50.0, "fx_rate": 2.0},
    ]
    plans = [
        {"symbol": "AAA", "side": "LONG", "hard_stop_price": 95.0, "remaining_quantity": 10},
        {"symbol": "BBB", "side": "SHORT", "hard_stop_price": 55.0, "remaining_quantity": 5},
    ]
    result = risk_at_stops(holdings, plans)
    # AAA : (95-100)*10*1*1 = -50 ; BBB : (55-50)*5*(-1)*2 = -50
    assert result.total_usd == -100.0
    assert result.worst_usd == -50.0
    assert result.without_stop == ()


def test_risk_at_stops_sans_stop_et_vide():
    from trader.cockpit.aggregates import risk_at_stops

    holdings = [{"symbol": "CCC", "quantity": 3, "last_price": 10.0}]
    result = risk_at_stops(holdings, [])  # aucun plan
    assert result.without_stop == ("CCC",)
    assert result.total_usd == 0.0

    empty = risk_at_stops([], [])
    assert empty.total_usd == 0.0 and empty.worst_symbol is None
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_cockpit_aggregates.py -v -k risk_at_stops`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implémentation**

```python
# à ajouter dans trader/cockpit/aggregates.py
@dataclass(frozen=True)
class RiskAtStops:
    total_usd: float = 0.0
    worst_symbol: str | None = None
    worst_usd: float = 0.0
    without_stop: tuple[str, ...] = ()


def risk_at_stops(holdings: list[dict], trade_plans: list[dict]) -> RiskAtStops:
    """Somme USD du P&L si tous les stops se déclenchent + pire position.

    Position sans plan, sans stop ou sans prix de référence → listée dans
    without_stop, exclue du total. Jamais d'exception.
    """
    plans_by_symbol: dict[str, dict] = {}
    for plan in _safe_list_of_dicts(trade_plans):
        symbol = str(plan.get("symbol") or "")
        if symbol and symbol not in plans_by_symbol:
            plans_by_symbol[symbol] = plan

    total = 0.0
    worst_symbol: str | None = None
    worst_usd = 0.0
    without_stop: list[str] = []
    for holding in _safe_list_of_dicts(holdings):
        symbol = str(holding.get("symbol") or "?")
        plan = plans_by_symbol.get(symbol) or {}
        stop = _safe_float(plan.get("hard_stop_price"), default=None)
        reference = (
            _safe_float(holding.get("last_price"), default=None)
            or _safe_float(holding.get("avg_price"), default=None)
            or _safe_float(plan.get("entry_price"), default=None)
        )
        if stop is None or not reference:
            without_stop.append(symbol)
            continue
        qty = (
            _safe_float(plan.get("remaining_quantity"), default=None)
            or _safe_float(plan.get("quantity"), default=None)
            or abs(_safe_float(holding.get("quantity"), default=0.0) or 0.0)
        )
        direction = -1.0 if str(plan.get("side") or "LONG").upper() == "SHORT" else 1.0
        fx_rate = _safe_float(holding.get("fx_rate"), default=1.0) or 1.0
        risk_usd = (stop - reference) * qty * direction * fx_rate
        total += risk_usd
        if worst_symbol is None or risk_usd < worst_usd:
            worst_symbol, worst_usd = symbol, risk_usd

    return RiskAtStops(
        total_usd=total,
        worst_symbol=worst_symbol,
        worst_usd=worst_usd,
        without_stop=tuple(without_stop),
    )
```

- [ ] **Step 4: Vérifier le passage**

Run: `uv run pytest tests/test_cockpit_aggregates.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/cockpit/aggregates.py tests/test_cockpit_aggregates.py
git commit -m "feat(cockpit): risk_at_stops — perte agrégée au déclenchement des stops (A1)"
```

---

### Task 4: `attention_items` — la ligne « à surveiller »

**Files:**
- Modify: `trader/cockpit/aggregates.py`
- Modify: `docs/superpowers/specs/2026-07-03-cockpit-home-gonzo-design.md` (§6 : ajouter « risque@stops<0 » dans la liste d'anomalies d'`attention_items` — la ligne du mockup l'affiche, le spec §6 l'omettait)
- Test: `tests/test_cockpit_aggregates.py`

**Interfaces:**
- Produces: `AttentionItem` (dataclass gelée : `label: str`, `severity: str` ∈ {"crit", "warn"}) · `attention_items(state: dict, *, kill_active: bool, now: datetime) -> list[AttentionItem]`. Ordre fixe : kill → halted → risque@stops → stale → rejets risk → armés <1h → sans stop. Liste vide = « RAS ».

- [ ] **Step 1: Tests qui échouent** (invariants spec §8.3)

```python
def test_attention_items_ordre_et_kill_premier():
    from trader.cockpit.aggregates import attention_items

    state = {
        "halted": "drawdown",
        "portfolio": {"holdings": [{"symbol": "AAA", "quantity": 10, "last_price": 100.0}]},
        "trade_plans": [{"symbol": "AAA", "side": "LONG", "hard_stop_price": 90.0,
                         "remaining_quantity": 10}],
        "stale_streaks": {"BBB": 3},
        "recent_decisions": [{"reason": "risk:max_exposure"}],
        "armed_plans": [{"expires_at": (NOW + timedelta(minutes=30)).isoformat()}],
    }
    items = attention_items(state, kill_active=True, now=NOW)
    labels = [item.label for item in items]
    assert items[0].label == "KILL actif" and items[0].severity == "crit"
    assert labels[1].startswith("HALT")
    assert any(label.startswith("risque@stops") for label in labels)
    assert any(label.startswith("stale 1") for label in labels)
    assert any("rejets risk" in label for label in labels)
    assert any("expirent <1h" in label for label in labels)


def test_attention_items_nominal_vide():
    from trader.cockpit.aggregates import attention_items

    assert attention_items({}, kill_active=False, now=NOW) == []


def test_attention_items_arme_deja_expire_ignore():
    from trader.cockpit.aggregates import attention_items

    state = {"armed_plans": [{"expires_at": (NOW - timedelta(minutes=5)).isoformat()}]}
    assert attention_items(state, kill_active=False, now=NOW) == []
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_cockpit_aggregates.py -v -k attention`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implémentation**

```python
# à ajouter dans trader/cockpit/aggregates.py
@dataclass(frozen=True)
class AttentionItem:
    label: str
    severity: str  # "crit" | "warn"


def attention_items(state: dict, *, kill_active: bool, now: datetime) -> list[AttentionItem]:
    """Anomalies méritant l'attention, par criticité décroissante. [] = RAS."""
    state = state if isinstance(state, dict) else {}
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    items: list[AttentionItem] = []

    if kill_active:
        items.append(AttentionItem("KILL actif", "crit"))
    halted = state.get("halted")
    if halted:
        items.append(AttentionItem(f"HALT {halted}", "crit"))

    portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
    risk = risk_at_stops(
        _safe_list_of_dicts(portfolio.get("holdings")),
        _safe_list_of_dicts(state.get("trade_plans")),
    )
    if risk.total_usd < 0:
        items.append(AttentionItem(f"risque@stops {risk.total_usd:+,.0f}$", "warn"))

    stale_streaks = (
        state.get("stale_streaks") if isinstance(state.get("stale_streaks"), dict) else {}
    )
    stale = [int(v) for v in stale_streaks.values() if isinstance(v, (int, float)) and v > 0]
    if stale:
        items.append(AttentionItem(f"stale {len(stale)} (max ×{max(stale)})", "warn"))

    rejects = sum(
        1
        for row in _safe_list_of_dicts(state.get("recent_decisions"))
        if str(row.get("reason") or "").startswith("risk:")
    )
    if rejects:
        items.append(AttentionItem(f"{rejects} rejets risk", "warn"))

    expiring = 0
    for watch in _safe_list_of_dicts(state.get("armed_plans")):
        expires_at = parse_ts(watch.get("expires_at"))
        if expires_at is not None and timedelta(0) <= expires_at - now < timedelta(hours=1):
            expiring += 1
    if expiring:
        items.append(AttentionItem(f"{expiring} armé(s) expirent <1h", "warn"))

    if risk.without_stop:
        shown = ", ".join(risk.without_stop[:3])
        extra = f" +{len(risk.without_stop) - 3}" if len(risk.without_stop) > 3 else ""
        items.append(AttentionItem(f"sans stop: {shown}{extra}", "warn"))

    return items
```

Dans le spec §6, ligne `attention_items` : remplacer `(kill, halted, stale N, rejets risk N, armés expirant <1 h, positions sans stop)` par `(kill, halted, risque@stops négatif, stale N, rejets risk N, armés expirant <1 h, positions sans stop)`.

- [ ] **Step 4: Vérifier le passage**

Run: `uv run pytest tests/test_cockpit_aggregates.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/cockpit/aggregates.py tests/test_cockpit_aggregates.py docs/superpowers/specs/2026-07-03-cockpit-home-gonzo-design.md
git commit -m "feat(cockpit): attention_items — anomalies ordonnées pour la ligne à surveiller (A1)"
```

---

### Task 5: `venue_clock` + exposition `sessions` dans le read model

**Files:**
- Modify: `trader/cockpit/aggregates.py`
- Modify: `trader/read_models/runtime_state.py:308-322` (`_load_venue_open_state_safe` retourne aussi `sessions`) et `:479` (clé `"sessions"` dans le dict d'état)
- Modify: `docs/superpowers/specs/2026-07-03-cockpit-home-gonzo-design.md` (§6 : signature `venue_clock(sessions, now)` — `venue_state`/`open_venues_list` retirés, inutiles au calcul)
- Test: `tests/test_cockpit_aggregates.py`

**Interfaces:**
- Consumes: `load_sessions`/`open_venues`/`_close_dt` (`trader.rotation.schedule`) — sessions = `dict[venue, {"open": "HH:MM", "close": "HH:MM"}]` en UTC, week-end fermé, `FX` 24/5.
- Produces: `VenueClock` (dataclass gelée : `open_now: tuple[str, ...]` sans FX, `next_venue: str | None`, `next_kind: str | None` ∈ {"open", "close"}, `next_at: datetime | None`) · `venue_clock(sessions: dict, now: datetime) -> VenueClock` · read model : clé `"sessions"` (dict, `{}` si indisponible).

- [ ] **Step 1: Tests qui échouent** (invariants spec §8.4)

```python
def test_venue_clock_ouverte_et_prochaine_transition():
    from trader.cockpit.aggregates import venue_clock

    sessions = {"TW": {"open": "01:00", "close": "05:30"},
                "EU": {"open": "07:00", "close": "15:30"}}
    # Vendredi 2026-07-03 12:00 UTC : EU ouverte, prochaine transition = close EU 15:30
    clock = venue_clock(sessions, NOW)
    assert clock.open_now == ("EU",)
    assert (clock.next_venue, clock.next_kind) == ("EU", "close")
    assert clock.next_at is not None and clock.next_at.hour == 15


def test_venue_clock_weekend_prochaine_ouverture_lundi():
    from trader.cockpit.aggregates import venue_clock

    saturday = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    clock = venue_clock({"EU": {"open": "07:00", "close": "15:30"}}, saturday)
    assert clock.open_now == ()
    assert clock.next_kind == "open"
    assert clock.next_at is not None and clock.next_at.weekday() == 0  # lundi


def test_venue_clock_sessions_absentes_neutre():
    from trader.cockpit.aggregates import venue_clock

    clock = venue_clock({}, NOW)
    assert clock.open_now == () and clock.next_at is None


def test_read_model_expose_sessions(tmp_path):
    from trader.read_models.runtime_state import load_runtime_state

    state = load_runtime_state(state_dir=tmp_path, config_dir=str(tmp_path))
    assert isinstance(state.get("sessions"), dict)
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_cockpit_aggregates.py -v -k venue`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implémentation**

```python
# à ajouter dans trader/cockpit/aggregates.py
@dataclass(frozen=True)
class VenueClock:
    open_now: tuple[str, ...] = ()
    next_venue: str | None = None
    next_kind: str | None = None  # "open" | "close"
    next_at: datetime | None = None


def venue_clock(sessions: dict, now: datetime) -> VenueClock:
    """Venues actions ouvertes + prochaine transition (open/close), UTC.

    FX (24/5) exclue. Sessions vides/malformées → VenueClock() neutre.
    """
    from trader.rotation.schedule import _close_dt, open_venues

    if not isinstance(sessions, dict) or not sessions:
        return VenueClock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    try:
        open_list = tuple(v for v in open_venues(now.isoformat(), sessions) if v != "FX")
    except Exception:
        return VenueClock()

    best: tuple[datetime, str, str] | None = None
    for venue, hours in sessions.items():
        if venue == "FX" or not isinstance(hours, dict):
            continue
        try:
            if venue in open_list:
                candidate = (_close_dt(now, str(hours["close"])), venue, "close")
            else:
                open_dt = _close_dt(now, str(hours["open"]))
                for _ in range(4):  # saute le week-end
                    if open_dt > now and open_dt.weekday() < 5:
                        break
                    open_dt = _close_dt(open_dt + timedelta(days=1), str(hours["open"]))
                candidate = (open_dt, venue, "open")
        except Exception:
            continue
        if best is None or candidate[0] < best[0]:
            best = candidate

    if best is None:
        return VenueClock(open_now=open_list)
    return VenueClock(open_now=open_list, next_venue=best[1], next_kind=best[2], next_at=best[0])
```

Dans `runtime_state.py`, `_load_venue_open_state_safe` (l.308) retourne un triplet :

```python
def _load_venue_open_state_safe(
    state_dir: Path, config_dir: str
) -> tuple[dict, list[str], dict]:
    """Charge venue_state, open_venues et sessions. ({}, [], {}) si indisponible."""
    try:
        from trader.rotation.venues import load_venue_state as _lvs
        from trader.rotation.schedule import load_sessions as _ls, open_venues as _ov

        venue_state = _lvs(state_dir)
        sessions = _ls(config_dir)
        now_iso = datetime.now(UTC).isoformat()
        ov_list = _ov(now_iso, sessions)
        return venue_state, ov_list, sessions
    except Exception:
        return {}, [], {}
```

Au call-site (l.479) : `venue_state, open_venues_list, sessions = _load_venue_open_state_safe(...)` puis ajouter `"sessions": sessions,` au dict retourné (à côté de `"venue_state"`).

- [ ] **Step 4: Vérifier le passage + non-régression read model**

Run: `uv run pytest tests/test_cockpit_aggregates.py tests/test_tui.py tests/test_cockpit_smoke.py -v`
Expected: PASS (si un test existant dépaquette l'ancien tuple 2 éléments, l'adapter au triplet).

- [ ] **Step 5: Commit**

```bash
git add trader/cockpit/aggregates.py trader/read_models/runtime_state.py tests/test_cockpit_aggregates.py docs/superpowers/specs/2026-07-03-cockpit-home-gonzo-design.md
git commit -m "feat(cockpit): venue_clock + sessions exposées par le read model (A1)"
```

---

### Task 6: Statut distillé responsive (`build_status_line`) branché sur `CockpitStatus`

**Files:**
- Create: `trader/cockpit/home.py`
- Modify: `trader/cockpit/app.py:195-302` (`CockpitStatus` délègue au builder pur ; ajoute `on_resize`)
- Create: `tests/test_cockpit_home.py`

**Interfaces:**
- Consumes: `sparkline` (`trader.ui.rich_panels`), `venue_clock`, `_safe_float`, `daemon_vital_state` (`trader.cockpit.supervisor` — objet à attributs `status`, `battement_old`, `since_seconds`).
- Produces: `build_status_line(state: dict, *, kill_active: bool, palette: Palette, width: int, now: datetime, vital) -> Text` — pur (le widget fournit `vital` et `now`). Ordre de drop : sparkline → venues → P&L USD → cycle → UTC → LLM → P&L % ; **jamais** vital/équité/mode/kill.

- [ ] **Step 1: Tests qui échouent** (invariant spec §8.5)

```python
# tests/test_cockpit_home.py
"""Tests des builders purs de la nouvelle home (home.py)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from trader.ui.palette import PALETTE_LIGHT

UTC = timezone.utc
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)


@dataclass
class FakeVital:
    status: str = "alive"
    battement_old: bool = False
    since_seconds: float | None = 2.0


STATE = {
    "portfolio": {"cash": 62589.0, "equity": 99452.0, "holdings": []},
    "starting_cash": 100000.0,
    "dry_run": True,
    "daemon_status": {"model_calls_used": 2, "max_model_calls_per_cycle": 25,
                      "decisions_done": 3, "symbols_total": 10},
    "equity_curve": [100000.0, 99500.0, 99452.0],
    "sessions": {"EU": {"open": "07:00", "close": "15:30"}},
}


def test_status_line_complet_a_largeur_confortable():
    from trader.cockpit.home import build_status_line

    line = build_status_line(STATE, kill_active=False, palette=PALETTE_LIGHT,
                             width=300, now=NOW, vital=FakeVital())
    plain = line.plain
    for fragment in ("VIVANT", "99,452", "DRY", "kill", "LLM 2/25", "cycle 3/10", "EU"):
        assert fragment in plain, fragment


def test_status_line_etroit_garde_les_criticites():
    """À 80 colonnes : vital/équité/mode/kill présents, sparkline droppée."""
    from trader.cockpit.home import build_status_line

    line = build_status_line(STATE, kill_active=True, palette=PALETTE_LIGHT,
                             width=80, now=NOW, vital=FakeVital())
    plain = line.plain
    assert "VIVANT" in plain and "99,452" in plain
    assert "DRY" in plain and "KILL" in plain
    assert "▁" not in plain and "█" not in plain  # sparkline droppée
    assert line.cell_len <= 80


def test_status_line_cycle_absent_hors_batch():
    from trader.cockpit.home import build_status_line

    state = {**STATE, "daemon_status": {"model_calls_used": 0,
                                        "max_model_calls_per_cycle": 25,
                                        "decisions_done": 0, "symbols_total": 0}}
    line = build_status_line(state, kill_active=False, palette=PALETTE_LIGHT,
                             width=300, now=NOW, vital=FakeVital())
    assert "cycle 0/0" not in line.plain
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_cockpit_home.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'trader.cockpit.home'`

- [ ] **Step 3: Implémentation du builder pur**

```python
# trader/cockpit/home.py
"""Home « mode Gonzo » du cockpit : builders purs + widgets Textual.

Spec : docs/superpowers/specs/2026-07-03-cockpit-home-gonzo-design.md.
Les builders sont purs (state dict → renderable) ; seuls les widgets
touchent Textual. Aucune I/O ici.
"""
from __future__ import annotations

from datetime import datetime, timezone

from rich.text import Text

from trader.cockpit.aggregates import venue_clock
from trader.read_models.runtime_state import _safe_float
from trader.ui.palette import Palette
from trader.ui.rich_panels import sparkline

UTC = timezone.utc

# Ordre de drop quand la largeur manque — les 4 segments critiques
# (vital, équité, mode, kill) ne figurent volontairement pas ici.
_STATUS_DROP_ORDER = ("spark", "venues", "pnl_usd", "cycle", "utc", "llm", "pnl_pct")


def _vital_text(vital, palette: Palette) -> Text:
    if vital.status == "alive":
        if vital.battement_old:
            mins = int(vital.since_seconds or 0) // 60
            secs = int(vital.since_seconds or 0) % 60
            return Text.assemble(("● VIVANT", "bold yellow"),
                                 (f" {mins:02d}:{secs:02d}", palette["dim"]))
        return Text("● VIVANT", style="bold green")
    if vital.status == "stopped":
        return Text("● ARRÊTÉ", style="bold red")
    return Text("● jamais démarré", style=palette["dim"])


def build_status_line(
    state: dict,
    *,
    kill_active: bool,
    palette: Palette,
    width: int,
    now: datetime,
    vital,
) -> Text:
    """Barre de statut distillée. Pur : vital et now sont injectés."""
    state = state if isinstance(state, dict) else {}
    portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
    kpis = state.get("kpis") if isinstance(state.get("kpis"), dict) else {}
    daemon_status = (
        state.get("daemon_status") if isinstance(state.get("daemon_status"), dict) else {}
    )

    equity = _safe_float(portfolio.get("equity") or kpis.get("equity"), default=0.0) or 0.0
    cash = _safe_float(portfolio.get("cash") or kpis.get("cash"), default=0.0) or 0.0
    starting = _safe_float(state.get("starting_cash"), default=cash) or cash
    pnl = equity - starting
    ret_pct = _safe_float(portfolio.get("total_return_pct"), default=None)
    if ret_pct is None:
        ret_pct = (_safe_float(kpis.get("total_return"), default=0.0) or 0.0) * 100.0
    pnl_style = palette["pnl_positive"] if pnl >= 0 else palette["pnl_negative"]

    curve = [
        v for v in (_safe_float(x, default=None) for x in (state.get("equity_curve") or []))
        if v is not None
    ]
    spark = sparkline(curve[-24:]) if len(curve) >= 2 else ""

    used = daemon_status.get("model_calls_used")
    max_calls = daemon_status.get("max_model_calls_per_cycle")
    llm = f"LLM {used}/{max_calls}" if used is not None and max_calls is not None else "LLM —"

    done = daemon_status.get("decisions_done")
    total = daemon_status.get("symbols_total")
    cycle_running = (
        isinstance(total, int) and total > 0 and isinstance(done, int) and done < total
    )

    clock = venue_clock(state.get("sessions") or {}, now)
    venue_bits: list[tuple[str, str]] = []
    for venue in clock.open_now:
        venue_bits.append((f"{venue}●", palette["status_nominal"]))
        venue_bits.append((" ", ""))
    if clock.next_at is not None and clock.next_venue not in clock.open_now:
        venue_bits.append(
            (f"{clock.next_venue} {clock.next_at.strftime('%H:%M')}", palette["dim"])
        )
    venues = Text.assemble(*venue_bits) if venue_bits else None

    mode = Text("LIVE", style="bold red") if not state.get("dry_run", True) else Text(
        "DRY", style="bold yellow"
    )
    kill = (
        Text(" !! KILL !! ", style="bold white on red")
        if kill_active
        else Text.assemble(("kill:", palette["dim"]), ("nominal", palette["status_nominal"]))
    )

    segments: list[tuple[str, Text | None]] = [
        ("vital", _vital_text(vital, palette)),
        ("equity", Text(f"{equity:,.0f}$", style=f"bold {palette['status_equity']}")),
        ("spark", Text(spark, style=palette["equity_line"]) if spark else None),
        ("pnl_pct", Text(f"{ret_pct:+.2f}%", style=pnl_style)),
        ("pnl_usd", Text(f"({pnl:+,.0f})", style=pnl_style)),
        ("mode", mode),
        ("kill", kill),
        ("llm", Text(llm, style=palette["status_accent"])),
        ("cycle", Text(f"cycle {done}/{total}", style=palette["status_accent"])
         if cycle_running else None),
        ("venues", venues),
        ("utc", Text(now.strftime("%H:%M:%SZ"), style=palette["dim"])),
    ]
    kept = [(name, text) for name, text in segments if text is not None]

    def _assemble(parts: list[tuple[str, Text]]) -> Text:
        out = Text("  ")
        for index, (_, piece) in enumerate(parts):
            if index:
                out.append(" · ", style=palette["dim"])
            out.append_text(piece)
        return out

    line = _assemble(kept)
    for drop in _STATUS_DROP_ORDER:
        if line.cell_len <= width:
            break
        kept = [(name, text) for name, text in kept if name != drop]
        line = _assemble(kept)
    return line
```

Dans `app.py`, remplacer le corps de `CockpitStatus.update_state` (l.208-302) et ajouter `on_resize` :

```python
class CockpitStatus(Static):
    """Barre de statut distillée — un seul endroit pour l'état runtime."""

    DEFAULT_CSS = """
    CockpitStatus {
        height: 2;
        background: $panel;
        border-bottom: solid $primary;
        padding: 0 1;
    }
    """

    def update_state(
        self, state: dict, kill_active: bool, *, palette: Palette = PALETTE_DARK
    ) -> None:
        from trader.cockpit.home import build_status_line

        vital = daemon_vital_state(_STATE_DIR / "daemon_status.json")
        width = self.size.width or 200
        self.update(
            build_status_line(
                state,
                kill_active=kill_active,
                palette=palette,
                width=width,
                now=datetime.now(UTC),
                vital=vital,
            )
        )

    def on_resize(self) -> None:
        app = self.app
        state = getattr(app, "_last_state", None)
        if state is not None:
            self.update_state(
                state,
                getattr(app, "_last_kill_active", False),
                palette=app._current_palette(),  # type: ignore[attr-defined]
            )
```

- [ ] **Step 4: Vérifier le passage + suite cockpit**

Run: `uv run pytest tests/test_cockpit_home.py tests/test_cockpit_smoke.py -v`
Expected: PASS. Si un smoke test asserte l'ancien texte de statut (ex. « Équité $ »), l'adapter au nouveau format (`99,452$`, `DRY`, `kill:nominal`).

- [ ] **Step 5: Commit**

```bash
git add trader/cockpit/home.py trader/cockpit/app.py tests/test_cockpit_home.py tests/test_cockpit_smoke.py
git commit -m "feat(cockpit): statut distillé responsive — criticités jamais tronquées (A2)"
```

---

### Task 7: Widget `AttentionLine` + suppression du code mort

**Files:**
- Modify: `trader/cockpit/home.py` (widget), `trader/cockpit/app.py` (compose + `_apply_state` ; **supprimer** `AttentionStrip` l.305-320 et `_build_trades_with_pnl` l.617-687)
- Test: `tests/test_cockpit_home.py`

**Interfaces:**
- Consumes: `attention_items` (Task 4).
- Produces: `AttentionLine(Static)` avec `update_state(state, kill_active, *, palette, now=None)` ; monté entre `CockpitStatus` et `CockpitNav` (id `attention-line`).

- [ ] **Step 1: Tests qui échouent**

```python
def test_attention_line_ras_et_anomalies():
    from trader.cockpit.home import AttentionLine

    line = AttentionLine()
    line.update_state({}, False, palette=PALETTE_LIGHT, now=NOW)
    assert "RAS" in str(line.renderable)

    state = {"stale_streaks": {"AAA": 2}}
    line.update_state(state, True, palette=PALETTE_LIGHT, now=NOW)
    rendered = str(line.renderable)
    assert "KILL" in rendered and "stale" in rendered


def _patch_state_paths(monkeypatch, tmp_path):
    """Monkeypatch des constantes de chemins (même mécanique que test_cockpit_smoke)."""
    import json

    import trader.cockpit.app as cockpit_module
    import trader.read_models.runtime_state as rs

    (tmp_path / "current_report.json").write_text(json.dumps({
        "ts": "2026-07-03T10:00:00+00:00",
        "dry_run": True,
        "portfolio": {"cash": 100000.0, "equity": 100000.0, "holdings": []},
        "decisions": [],
    }), encoding="utf-8")
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")
    monkeypatch.setattr(rs, "_STATE_DIR", tmp_path)


async def test_app_compose_attention_line(tmp_path, monkeypatch):
    from trader.cockpit.home import AttentionLine
    from trader.cockpit import CockpitApp

    _patch_state_paths(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        assert app.query_one("#attention-line", AttentionLine) is not None
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_cockpit_home.py -v -k attention_line`
Expected: FAIL — `ImportError: cannot import name 'AttentionLine'`

- [ ] **Step 3: Implémentation**

```python
# à ajouter dans trader/cockpit/home.py
from textual.widgets import Static

from trader.cockpit.aggregates import attention_items


class AttentionLine(Static):
    """Ligne « à surveiller » : anomalies uniquement, sinon RAS discret."""

    DEFAULT_CSS = """
    AttentionLine {
        height: 1;
        background: $surface;
        padding: 0 1;
    }
    """

    def update_state(
        self,
        state: dict,
        kill_active: bool,
        *,
        palette: Palette,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(UTC)
        items = attention_items(state, kill_active=kill_active, now=now)
        if not items:
            self.update(Text("✓ RAS", style=palette["status_nominal"]))
            return
        text = Text(" ⚠ ", style=f"bold {palette['kpi_vol_warn']}")
        for index, item in enumerate(items):
            if index:
                text.append("  ·  ", style=palette["dim"])
            style = "bold white on red" if item.severity == "crit" else palette["kpi_vol_warn"]
            text.append(item.label, style=style)
        self.update(text)
```

Dans `app.py` :
1. `compose()` : après `yield CockpitStatus(...)`, insérer `yield AttentionLine(id="attention-line")` (import depuis `trader.cockpit.home`).
2. `_apply_state()` : après la mise à jour du statut, ajouter :
```python
attention: AttentionLine = self.query_one("#attention-line", AttentionLine)
attention.update_state(state, kill_active, palette=palette)
```
3. Supprimer la classe `AttentionStrip` (l.305-320) et la fonction `_build_trades_with_pnl` (l.617-687). Garder les alias `_build_attention_line`/`_build_overview_panel` (l.136-137) — utilisés par `tests/test_cockpit_smoke.py`.

- [ ] **Step 4: Vérifier le passage + grep code mort**

Run: `uv run pytest tests/test_cockpit_home.py tests/test_cockpit_smoke.py -v`
Expected: PASS.
Run: `grep -rn "AttentionStrip\|_build_trades_with_pnl" trader/ tests/`
Expected: aucune occurrence hors historique.

- [ ] **Step 5: Commit**

```bash
git add trader/cockpit/home.py trader/cockpit/app.py tests/test_cockpit_home.py
git commit -m "feat(cockpit): ligne à surveiller (AttentionLine) + suppression code mort (A2)"
```

---

### Task 8: Builders des tuiles — portefeuille (chart palette + risque fusionné), activité, plans

**Files:**
- Modify: `trader/cockpit/home.py`
- Modify: `trader/ui/rich_panels.py:175-187` (`_build_equity_panel` : couleur/thème plotext paramétrés par palette — fix hardcode)
- Test: `tests/test_cockpit_home.py`

**Interfaces:**
- Consumes: helpers privés de `trader/cockpit/overview.py` (import explicite) : `_holding_symbol`, `_holding_currency`, `_holding_notional`, `_holding_pnl`, `_holding_native_price`, `_stop_risk_for_holding`, `_plan_for_symbol`, `_metric_cell`, `_fmt_compact_float`, `_fmt_signed_compact_float`, `_bar`, `_signed_bar`, `_extract_equity_curve`, `_armed_order_label`, `_armed_stop_label`, `_condition_summary`, `_relative_expiry`, `_plan_qty_label`, `_exit_plan_risk_label`, `_next_tp_label`, `_plan_state_label`, `_price_for_symbol`, `_symbol_is_stale` ; `aggregates.activity_buckets`, `ACTIVITY_STATES`.
- Produces: `_hex_to_rgb(color: str) -> tuple[int, int, int] | str` · `styled_bar(value: float, total: float, *, width: int, style: str, signed: bool = False) -> Text` · `build_equity_chart(values: list[float], *, palette: Palette, width: int = 58, height: int = 9) -> RenderableType` · `build_portfolio_tile(state: dict, *, palette: Palette) -> RenderableType` · `build_activity_tile(buckets: dict[str, list[int]], *, palette: Palette) -> RenderableType` · `build_plans_tile(state: dict, *, palette: Palette, now: datetime | None = None) -> RenderableType`.

- [ ] **Step 1: Tests qui échouent**

```python
def _console_render(renderable) -> str:
    from rich.console import Console

    console = Console(width=100, record=True)
    console.print(renderable)
    return console.export_text()


def test_build_activity_tile_series_et_compteurs():
    from trader.cockpit.home import build_activity_tile

    buckets = {"exec": [0] * 11 + [2], "veille": [0] * 12, "plan": [0] * 12,
               "risk": [1] + [0] * 11, "stale": [0] * 12, "hold": [3] * 12}
    rendered = _console_render(build_activity_tile(buckets, palette=PALETTE_LIGHT))
    assert "exec" in rendered and "n=2" in rendered
    assert "risk" in rendered and "n=1" in rendered
    assert "n=36" in rendered  # hold


def test_build_portfolio_tile_risque_fusionne():
    from trader.cockpit.home import build_portfolio_tile

    state = {
        "portfolio": {"cash": 50000.0, "equity": 100000.0, "holdings": [
            {"symbol": "AAA", "quantity": 10, "avg_price": 90.0,
             "last_price": 100.0, "fx_rate": 1.0, "unrealized_pnl": 100.0},
        ]},
        "trade_plans": [{"symbol": "AAA", "side": "LONG", "hard_stop_price": 95.0,
                         "remaining_quantity": 10}],
        "equity_curve": [100000.0, 100100.0],
        "attribution": {},
        "kpis": {},
        "starting_cash": 100000.0,
    }
    rendered = _console_render(build_portfolio_tile(state, palette=PALETTE_LIGHT))
    assert "AAA" in rendered
    assert "-50" in rendered  # perte@stop (95-100)*10 — colonne fusionnée
    assert "Risque sorties" not in rendered  # plus de table séparée clippée


def test_build_plans_tile_ordre_types():
    from trader.cockpit.home import build_plans_tile

    state = {
        "armed_plans": [{"symbol": "ARM.TW", "order": {"intent": "OPEN_LONG", "qty": 4},
                         "conditions": [], "expires_at": (NOW + timedelta(hours=2)).isoformat()}],
        "trade_plans": [{"symbol": "EXI.TW", "side": "LONG", "hard_stop_price": 1.0,
                         "entry_price": 2.0, "remaining_quantity": 1}],
        "indicator_watches": [{"symbol": "WCH.TW", "conditions": [],
                               "expires_at": (NOW + timedelta(hours=3)).isoformat()}],
    }
    rendered = _console_render(build_plans_tile(state, palette=PALETTE_LIGHT, now=NOW))
    assert rendered.index("ARM.TW") < rendered.index("EXI.TW") < rendered.index("WCH.TW")
    assert "armé" in rendered and "sortie" in rendered and "veille" in rendered


def test_hex_to_rgb():
    from trader.cockpit.home import _hex_to_rgb

    assert _hex_to_rgb("#0D7680") == (13, 118, 128)
    assert _hex_to_rgb("cyan") == "cyan"
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_cockpit_home.py -v -k "tile or hex"`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implémentation des builders**

```python
# à ajouter dans trader/cockpit/home.py
from rich.console import Group, RenderableType
from rich.table import Table

from trader.cockpit.aggregates import ACTIVITY_STATES, activity_buckets  # noqa: F401
from trader.cockpit.overview import (
    _armed_order_label,
    _armed_stop_label,
    _bar,
    _condition_summary,
    _exit_plan_risk_label,
    _extract_equity_curve,
    _fmt_compact_float,
    _fmt_signed_compact_float,
    _holding_currency,
    _holding_native_price,
    _holding_notional,
    _holding_pnl,
    _holding_symbol,
    _metric_cell,
    _next_tp_label,
    _plan_for_symbol,
    _plan_qty_label,
    _plan_state_label,
    _price_for_symbol,
    _relative_expiry,
    _signed_bar,
    _stop_risk_for_holding,
    _symbol_is_stale,
)
from trader.read_models.runtime_state import _safe_list_of_dicts


def _hex_to_rgb(color: str) -> tuple[int, int, int] | str:
    """"#RRGGBB" → tuple RGB pour plotext ; nom de couleur → inchangé."""
    text = color.strip()
    if text.startswith("#") and len(text) == 7:
        return (int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16))
    return text


def styled_bar(
    value: float, total: float, *, width: int, style: str, signed: bool = False
) -> Text:
    """Barre unicode STYLÉE (fix des barres noires : _bar retourne une str nue)."""
    raw = _signed_bar(value, total, width=width) if signed else _bar(value, total, width=width)
    return Text(raw, style=style)


def build_equity_chart(
    values: list[float], *, palette: Palette, width: int = 58, height: int = 9
) -> RenderableType:
    """Courbe d'équité en braille plotext, couleur palette, fenêtre ~200 pts."""
    series = [v for v in values if v == v][-200:]
    if len(series) < 2:
        return Text("courbe indisponible (<2 points)", style=palette["dim"])
    try:
        import plotext as plt

        plt.clear_figure()
        plt.theme("clear")
        plt.plotsize(width, height)
        plt.plot(list(range(len(series))), series, marker="braille",
                 color=_hex_to_rgb(palette["equity_line"]))
        return Text.from_ansi(plt.build())
    except Exception:
        return Text(sparkline(series[-width:]), style=f"bold {palette['equity_line']}")


def build_portfolio_tile(state: dict, *, palette: Palette) -> RenderableType:
    """Métriques portefeuille + courbe + top 5 positions AVEC risque@stop fusionné."""
    state = state if isinstance(state, dict) else {}
    portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
    kpis = state.get("kpis") if isinstance(state.get("kpis"), dict) else {}
    attribution = state.get("attribution") if isinstance(state.get("attribution"), dict) else {}
    trade_plans = _safe_list_of_dicts(state.get("trade_plans"))
    holdings = sorted(
        _safe_list_of_dicts(portfolio.get("holdings")),
        key=lambda item: max(abs(_holding_pnl(item)), _holding_notional(item)),
        reverse=True,
    )

    cash = _safe_float(portfolio.get("cash") or kpis.get("cash"), default=0.0) or 0.0
    equity = _safe_float(portfolio.get("equity") or kpis.get("equity"), default=0.0) or 0.0
    starting = _safe_float(state.get("starting_cash"), default=cash) or cash
    pnl = equity - starting
    ret_pct = _safe_float(portfolio.get("total_return_pct"), default=None)
    if ret_pct is None:
        ret_pct = (_safe_float(kpis.get("total_return"), default=0.0) or 0.0) * 100.0
    unrealized = sum(_holding_pnl(h) for h in holdings)
    realized = _safe_float(attribution.get("realized_pnl"), default=None)
    fees = _safe_float(attribution.get("total_commissions"), default=None)

    header = Table.grid(expand=True)
    for _ in range(4):
        header.add_column(ratio=1)
    ret_style = palette["pnl_positive"] if ret_pct >= 0 else palette["pnl_negative"]
    header.add_row(
        _metric_cell("Équité", f"{equity:,.0f}", value_style=f"bold {palette['status_equity']}", palette=palette),
        _metric_cell("Cash", f"{cash:,.0f} ({cash / equity * 100.0 if equity else 0.0:.0f}%)", value_style=palette["kpi_default"], palette=palette),
        _metric_cell("P&L total", f"{ret_pct:+.2f}% {pnl:+,.0f}", value_style=ret_style, palette=palette),
        _metric_cell(
            "Latent / Réalisé / Frais",
            f"{_fmt_signed_compact_float(unrealized, decimals=0)} / "
            f"{_fmt_signed_compact_float(realized, decimals=0)} / "
            f"{_fmt_compact_float(fees, decimals=0)}",
            value_style=palette["kpi_default"],
            palette=palette,
        ),
    )

    total_notional = sum(_holding_notional(h) for h in holdings)
    max_abs_pnl = max((abs(_holding_pnl(h)) for h in holdings[:4]), default=0.0)
    side_tables = Table.grid(expand=True)
    side_tables.add_column(ratio=3)
    side_tables.add_column(ratio=2)
    alloc = Table(title="Allocation", show_header=False, expand=True, box=None, pad_edge=False)
    alloc.add_column("sym", no_wrap=True)
    alloc.add_column("bar", overflow="fold")
    for holding in holdings[:5]:
        notional = _holding_notional(holding)
        pct = notional / total_notional * 100.0 if total_notional else 0.0
        alloc.add_row(
            _holding_symbol(holding),
            Text.assemble(
                styled_bar(notional, total_notional, width=12, style=palette["status_accent"]),
                (f" {pct:>4.1f}%", palette["dim"]),
            ),
        )
    contrib = Table(title="Contrib PnL", show_header=False, expand=True, box=None, pad_edge=False)
    contrib.add_column("sym", no_wrap=True)
    contrib.add_column("bar", overflow="fold")
    for holding in sorted(holdings, key=lambda h: abs(_holding_pnl(h)), reverse=True)[:4]:
        value = _holding_pnl(holding)
        style = palette["pnl_positive"] if value >= 0 else palette["pnl_negative"]
        contrib.add_row(
            _holding_symbol(holding),
            Text.assemble(
                styled_bar(value, max_abs_pnl, width=10, style=style, signed=True),
                (f" {_fmt_signed_compact_float(value, decimals=0)}", style),
            ),
        )
    side_tables.add_row(alloc, contrib)

    charts = Table.grid(expand=True)
    charts.add_column(ratio=3)
    charts.add_column(ratio=2)
    charts.add_row(build_equity_chart(_extract_equity_curve(state), palette=palette), side_tables)

    positions = Table(show_header=True, expand=True, box=None, pad_edge=False)
    for column, justify in (
        ("Sym", "left"), ("Dev.", "left"), ("Qté", "right"), ("Dernier", "right"),
        ("Expo USD", "right"), ("PnL USD", "right"), ("Stop", "right"), ("Perte@stop", "right"),
    ):
        positions.add_column(column, justify=justify, no_wrap=True)
    for holding in holdings[:5]:
        symbol = _holding_symbol(holding)
        pnl_value = _holding_pnl(holding)
        pnl_style = palette["pnl_positive"] if pnl_value >= 0 else palette["pnl_negative"]
        stop_label, dist_label, loss_label, state_label = _stop_risk_for_holding(
            holding, _plan_for_symbol(trade_plans, symbol)
        )
        loss_style = palette["pnl_negative"] if state_label == "risque" else palette["dim"]
        positions.add_row(
            Text(symbol, style="bold"),
            _holding_currency(holding),
            _fmt_compact_float(holding.get("quantity"), decimals=2),
            _fmt_compact_float(_holding_native_price(holding), decimals=2),
            _fmt_compact_float(_holding_notional(holding), decimals=0),
            Text(_fmt_signed_compact_float(pnl_value, decimals=0), style=pnl_style),
            f"{stop_label} {dist_label}",
            Text(loss_label, style=loss_style),
        )
    if not holdings:
        positions.add_row("—", "—", "—", "—", "—", "—", "—", "—")

    return Group(header, charts, positions)


_SERIES_STYLE_KEYS: dict[str, str] = {
    "exec": "pnl_positive",
    "veille": "status_accent",
    "plan": "status_accent",
    "risk": "pnl_negative",
    "stale": "kpi_vol_warn",
    "hold": "dim",
}


def build_activity_tile(buckets: dict[str, list[int]], *, palette: Palette) -> RenderableType:
    """Une sparkline par état + compteur — la vue temporelle façon Gonzo."""
    table = Table(show_header=False, expand=True, box=None, pad_edge=False)
    table.add_column("état", no_wrap=True)
    table.add_column("60min", no_wrap=True)
    table.add_column("n", justify="right", no_wrap=True)
    for state_name in ACTIVITY_STATES:
        counts = buckets.get(state_name) or []
        total = sum(counts)
        style = palette[_SERIES_STYLE_KEYS[state_name]]
        spark = sparkline([float(c) for c in counts]) if counts else ""
        table.add_row(
            Text(state_name, style=style if total else palette["dim"]),
            Text(spark, style=style if total else palette["dim"]),
            Text(f"n={total}", style=style if total else palette["dim"]),
        )
    return table


def build_plans_tile(
    state: dict, *, palette: Palette, now: datetime | None = None
) -> RenderableType:
    """Table unifiée armés → sorties (par risque) → veilles (par expiration)."""
    now = now or datetime.now(UTC)
    state = state if isinstance(state, dict) else {}
    armed = _safe_list_of_dicts(state.get("armed_plans"))
    trade_plans = _safe_list_of_dicts(state.get("trade_plans"))
    armed_ids = {id(w) for w in armed}
    watches = [w for w in _safe_list_of_dicts(state.get("indicator_watches"))
               if id(w) not in armed_ids]

    table = Table(show_header=True, expand=True, box=None, pad_edge=False)
    for column in ("Type", "Sym", "Détail", "Déclencheur / Risque", "Exp."):
        table.add_column(column, no_wrap=(column != "Déclencheur / Risque"),
                         overflow="fold" if column == "Déclencheur / Risque" else "ellipsis")

    for watch in armed:
        table.add_row(
            Text("armé", style=f"bold {palette['status_accent']}"),
            Text(str(watch.get("symbol") or "—"), style="bold"),
            f"{_armed_order_label(watch)} {_armed_stop_label(watch)}",
            _condition_summary(watch.get("conditions"), watch.get("logic"), max_items=2),
            _relative_expiry(watch.get("expires_at"), now=now),
        )

    exit_rows = []
    for plan in trade_plans:
        symbol = str(plan.get("symbol") or "—")
        price = _price_for_symbol(state, symbol)
        stale = _symbol_is_stale(state, symbol)
        exit_rows.append((0 if stale else 1, symbol, plan, price, stale))
    for _, symbol, plan, price, stale in sorted(exit_rows, key=lambda r: (r[0], r[1]))[:8]:
        risk_label = _exit_plan_risk_label(plan, price, stale)
        table.add_row(
            Text("sortie", style=palette["kpi_default"]),
            Text(symbol, style="bold"),
            f"{_plan_qty_label(plan)} · {_plan_state_label(plan)}",
            Text(f"{risk_label} · {_next_tp_label(plan, price)}",
                 style=palette["kpi_vol_warn"] if stale else palette["dim"]),
            "—",
        )
    if len(exit_rows) > 8:
        table.add_row(Text("sortie", style=palette["dim"]), Text(f"+{len(exit_rows) - 8}",
                      style=palette["dim"]), "…", "", "")

    for watch in sorted(watches, key=lambda w: str(w.get("expires_at") or ""))[:5]:
        table.add_row(
            Text("veille", style=palette["dim"]),
            Text(str(watch.get("symbol") or "—"), style="bold"),
            "WAKE",
            _condition_summary(watch.get("conditions"), watch.get("logic"), max_items=2),
            _relative_expiry(watch.get("expires_at"), now=now),
        )
    if len(watches) > 5:
        table.add_row(Text("veille", style=palette["dim"]),
                      Text(f"+{len(watches) - 5}", style=palette["dim"]), "…", "", "")
    if not (armed or exit_rows or watches):
        table.add_row("—", "—", "aucun plan", "", "")
    return table
```

Dans `rich_panels.py`, `_build_equity_panel` (l.175-187) : remplacer `plt.theme("pro")` par `plt.theme("clear")` et `color="cyan"` par une couleur dérivée de la palette (`color=_rgb_from_palette(palette["equity_line"])` avec un petit helper local identique à `_hex_to_rgb`) — la page 2 profite du fix.

- [ ] **Step 4: Vérifier le passage**

Run: `uv run pytest tests/test_cockpit_home.py tests/test_cockpit_smoke.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/cockpit/home.py trader/ui/rich_panels.py tests/test_cockpit_home.py
git commit -m "feat(cockpit): builders tuiles home — portefeuille+risque, activité 60min, plans unifiés (A3)"
```

---

### Task 9: `HomePane` + `FluxPane` — la nouvelle grille branchée dans l'app

**Files:**
- Modify: `trader/cockpit/home.py` (widgets `HomePane`, `FluxPane`), `trader/cockpit/app.py` (LogsPane paramétrable, compose, `_apply_state`, `_propagate_palette`, `_poll_events`, toggles c/f)
- Create: `scripts/cockpit_screenshots.py`
- Test: `tests/test_cockpit_home.py`

**Interfaces:**
- Consumes: builders Task 8, `LogsPane` (`app.py:695`), `select_decision_rows`, `_decision_time_label`/`_decision_effect_label` (overview).
- Produces: `HomePane(Static)` id `overview-page` (remplace `OverviewPane` dans l'app — `overview.py` reste intact pour ses tests) avec `update_state(state, kill_active)` et `_current_palette` ; `FluxPane(LogsPane)` **définie dans `app.py`** juste après `LogsPane` (même module → pas de cycle d'import), id `home-flux`, RichLog id `flux-log` ; `LogsPane` gagne les attrs de classe `log_widget_id: str = "events-log"` et `pane_title: str`. `HomePane.compose()` importe `FluxPane` tardivement (`from trader.cockpit.app import FluxPane` DANS compose — jamais au niveau module de `home.py`, sinon import circulaire avec `app.py` qui importe `home` en tête).

- [ ] **Step 1: Tests qui échouent**

```python
async def test_home_pane_remplace_overview(tmp_path, monkeypatch):
    from trader.cockpit.app import FluxPane
    from trader.cockpit.home import HomePane
    from trader.cockpit import CockpitApp

    _patch_state_paths(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        home = app.query_one("#overview-page", HomePane)
        assert home is not None
        assert app.query_one("#home-flux", FluxPane) is not None
        # les tuiles existent
        for tile_id in ("#home-portfolio", "#home-activity", "#home-decisions", "#home-plans"):
            assert home.query_one(tile_id) is not None


def test_home_tuiles_sans_champs_runtime():
    """Anti-redondance (spec §8.6) : phase/LLM/cycle/source vivent dans le statut,
    plus jamais dans les tuiles."""
    from datetime import timedelta

    from trader.cockpit.home import build_activity_tile, build_plans_tile, build_portfolio_tile
    from trader.cockpit.aggregates import activity_buckets

    state = {
        "portfolio": {"cash": 1.0, "equity": 1.0, "holdings": []},
        "daemon_status": {"phase": "analyzing_batch", "model_calls_used": 9,
                          "max_model_calls_per_cycle": 25},
        "source": "current_report",
        "ts": "2026-07-03T10:00:00Z",
        "armed_plans": [], "trade_plans": [], "indicator_watches": [],
        "attribution": {}, "kpis": {}, "equity_curve": [],
    }
    rendered = "".join(
        _console_render(build)
        for build in (
            build_portfolio_tile(state, palette=PALETTE_LIGHT),
            build_activity_tile(activity_buckets([], NOW), palette=PALETTE_LIGHT),
            build_plans_tile(state, palette=PALETTE_LIGHT, now=NOW),
        )
    )
    assert "analyzing_batch" not in rendered
    assert "current_report" not in rendered
    assert "9/25" not in rendered


async def test_home_flux_recoit_les_events(tmp_path, monkeypatch):
    import json

    from trader.cockpit import CockpitApp

    _patch_state_paths(monkeypatch, tmp_path)
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps({"ts": "2026-07-03T01:00:00Z", "event": "decision_recorded",
                                  "symbol": "AAA", "action": "BUY", "executed": True}) + "\n",
                      encoding="utf-8")
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        from textual.widgets import RichLog

        flux_log = app.query_one("#home-flux").query_one(RichLog)
        assert flux_log.line_count > 0
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_cockpit_home.py -v -k home_pane`
Expected: FAIL — `ImportError: cannot import name 'HomePane'`

- [ ] **Step 3: Implémentation**

1. `app.py` — paramétrer `LogsPane` (l.695-796) : remplacer les ids codés en dur par les attrs de classe :

```python
class LogsPane(Static):
    log_widget_id: str = "events-log"
    pane_title: str = "[bold]Logs live[/bold]  [dim]c:cycles  f:scroll  l:toggle pane[/dim]"

    def compose(self) -> ComposeResult:
        yield Static(self.pane_title, id=f"{self.log_widget_id}-title")
        yield RichLog(id=self.log_widget_id, highlight=False, markup=False,
                      max_lines=_MAX_EVENT_LINES)
```
…et dans `toggle_cycles`, `toggle_scroll`, `poll_events` remplacer `"#events-log"` par `f"#{self.log_widget_id}"`.

2. `home.py` — la grille :

```python
from textual.app import ComposeResult
from textual.containers import Horizontal

from trader.cockpit.overview import _decision_effect_label, _decision_time_label
from trader.cockpit.aggregates import select_decision_rows
from trader.ui.palette import PALETTE_LIGHT


def build_decisions_tile(
    decisions: list[dict], recent_decisions: list[dict], *, palette: Palette
) -> RenderableType:
    """Triage statique (remplacé par DataTable en Task 10)."""
    table = Table(show_header=True, expand=True, box=None, pad_edge=False)
    for column in ("UTC", "Sym", "Act", "État", "Conf", "Suite"):
        table.add_column(column, no_wrap=(column != "Suite"),
                         overflow="fold" if column == "Suite" else "ellipsis")
    from trader.cockpit.aggregates import decision_status

    for row in select_decision_rows(decisions, recent_decisions, limit=8):
        action = str(row.get("action") or "—").upper()
        status = decision_status(row)
        confidence = _safe_float(row.get("confidence"), default=None)
        status_style = {
            "exec": palette["status_nominal"], "risk": palette["pnl_negative"],
            "stale": palette["kpi_vol_warn"], "armé": palette["status_accent"],
            "plan": palette["status_accent"], "veille": palette["status_accent"],
            "quiet": palette["dim"],
        }.get(status, palette["kpi_default"])
        action_style = (palette["action_buy"] if action == "BUY"
                        else palette["action_sell"] if action == "SELL" else palette["dim"])
        table.add_row(
            _decision_time_label(row),
            Text(str(row.get("symbol") or "—"), style="bold"),
            Text(action, style=action_style),
            Text(status, style=status_style),
            f"{confidence:.2f}" if confidence is not None else "—",
            _decision_effect_label(row),
        )
    if not table.rows:
        table.add_row("—", "—", "—", "—", "—", "—")
    return table


class HomePane(Static):
    """Home dense 1 écran — grammaire Gonzo (spec §4)."""

    DEFAULT_CSS = """
    HomePane {
        height: 100%;
        width: 100%;
        overflow-y: hidden;
        layout: vertical;
        padding: 0 1;
    }
    #home-top-row { height: 3fr; width: 100%; layout: horizontal; }
    #home-bottom-row { height: 2fr; width: 100%; layout: horizontal; }
    #home-portfolio { width: 2fr; height: 100%; border: solid $primary; padding: 0 1; }
    #home-activity { width: 1fr; height: 100%; border: solid $primary; padding: 0 1; }
    #home-decisions { width: 2fr; height: 100%; border: solid $primary; padding: 0 1; }
    #home-plans { width: 2fr; height: 100%; border: solid $primary; padding: 0 1; }
    #home-flux { width: 3fr; height: 100%; }
    """

    _current_palette: Palette = PALETTE_LIGHT

    def compose(self) -> ComposeResult:
        # Import tardif OBLIGATOIRE : app.py importe home.py en tête de module,
        # un import module-level de app ici créerait un cycle.
        from trader.cockpit.app import FluxPane

        with Horizontal(id="home-top-row"):
            yield Static(id="home-portfolio")
            yield Static(id="home-activity")
            yield Static(id="home-decisions")
        with Horizontal(id="home-bottom-row"):
            yield Static(id="home-plans")
            yield FluxPane(id="home-flux")

    def update_state(self, state: dict, kill_active: bool) -> None:
        palette = self._current_palette
        now = datetime.now(UTC)
        recent = state.get("recent_decisions") if isinstance(state.get("recent_decisions"), list) else []
        decisions = _safe_list_of_dicts(state.get("decisions"))
        self.query_one("#home-portfolio", Static).update(
            build_portfolio_tile(state, palette=palette)
        )
        self.query_one("#home-activity", Static).update(
            build_activity_tile(activity_buckets(recent, now), palette=palette)
        )
        self.query_one("#home-decisions", Static).update(
            build_decisions_tile(decisions, recent, palette=palette)
        )
        self.query_one("#home-plans", Static).update(
            build_plans_tile(state, palette=palette, now=now)
        )
```

Et dans `app.py`, juste après la classe `LogsPane` (même module → aucun cycle) :

```python
class FluxPane(LogsPane):
    """Tuile flux de la home — même moteur que la page Logs, ids distincts."""

    log_widget_id = "flux-log"
    pane_title = "[bold]Flux live[/bold]  [dim]f:scroll  F:classes  /:regex[/dim]"
```

3. `app.py` — wiring :
- `compose()` : remplacer `yield OverviewPane(id="overview-page", classes="cockpit-page")` par `yield HomePane(id="overview-page", classes="cockpit-page")` (import `from trader.cockpit.home import AttentionLine, HomePane`).
- `_apply_state()` : remplacer le bloc `overview` par le même appel sur `HomePane`.
- `_propagate_palette()` : remplacer l'entrée `("#overview-page", OverviewPane)` par `("#overview-page", HomePane)` et ajouter la propagation au `#home-flux` (comme `#logs-pane`).
- `_poll_events()` : après le poll de `#logs-pane`, ajouter :
```python
try:
    self.query_one("#home-flux").poll_events(_EVENTS_FILE)
except Exception:
    pass
```
- `action_toggle_scroll`/`action_toggle_cycles` : appliquer aux deux panes (`#logs-pane` et `#home-flux`).

4. `scripts/cockpit_screenshots.py` — outil de vérification visuelle réutilisable :

```python
"""Capture headless des pages du cockpit → SVG + PNG (qlmanage).

Usage : uv run python scripts/cockpit_screenshots.py [outdir]
"""
import asyncio
import subprocess
import sys
from pathlib import Path

from trader.cockpit.app import CockpitApp

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "state/screenshots")
OUT.mkdir(parents=True, exist_ok=True)


async def main() -> None:
    app = CockpitApp()
    async with app.run_test(size=(200, 52)) as pilot:
        await pilot.pause()
        await asyncio.sleep(3.0)
        await pilot.press("escape")
        await asyncio.sleep(1.0)
        app.save_screenshot(filename="home.svg", path=str(OUT))
        for key, name in [("2", "portfolio"), ("3", "decisions"), ("4", "plans"),
                          ("5", "observability"), ("6", "logs")]:
            await pilot.press(key)
            await asyncio.sleep(0.8)
            app.save_screenshot(filename=f"{name}.svg", path=str(OUT))
        await pilot.press("1")
        await pilot.press("d")
        await asyncio.sleep(2.0)
        app.save_screenshot(filename="home-alt-theme.svg", path=str(OUT))


asyncio.run(main())
for svg in OUT.glob("*.svg"):
    subprocess.run(["qlmanage", "-t", "-s", "2000", "-o", str(OUT), str(svg)],
                   capture_output=True)
print(f"captures dans {OUT}")
```

- [ ] **Step 4: Vérifier le passage + regarder les captures**

Run: `uv run pytest tests/test_cockpit_home.py tests/test_cockpit_smoke.py tests/test_cockpit_events.py -v`
Expected: PASS (adapter les smoke tests qui montaient `OverviewPane` : ils doivent monter `HomePane`).
Run: `uv run python scripts/cockpit_screenshots.py` puis **ouvrir/regarder** `state/screenshots/home.svg.png` — vérifier : pas de contenu clippé, barres colorées, histogramme visible, flux rempli.

- [ ] **Step 5: Commit**

```bash
git add trader/cockpit/home.py trader/cockpit/app.py scripts/cockpit_screenshots.py tests/
git commit -m "feat(cockpit): HomePane — grille Gonzo 1 écran avec flux live intégré (A3)"
```

---

### Task 10: DataTables navigables (décisions, plans, positions)

**Files:**
- Modify: `trader/cockpit/home.py`
- Create: `tests/test_cockpit_interactions.py`

**Interfaces:**
- Consumes: `select_decision_rows`, builders Task 8, `DataTable` (Textual).
- Produces: `DecisionsTable(DataTable)` / `PlansTable(DataTable)` / `PositionsTable(DataTable)` avec `refresh_rows(...)` idempotent (clear + re-add) ; **clé de ligne = `"<symbol>|<discriminant>"`** ; message Textual custom `SymbolChosen(Message)` avec attribut `symbol: str` posté sur `DataTable.RowSelected` (Enter). `HomePane` remplace les Static décisions/plans par ces tables et la table positions du portefeuille par `PositionsTable`.

- [ ] **Step 1: Tests qui échouent**

```python
# tests/test_cockpit_interactions.py
"""Interactions de la home : DataTables, focus, modal symbole, filtres."""
from __future__ import annotations

import json
from pathlib import Path


def _patch_state_paths(monkeypatch, tmp_path: Path) -> None:
    """Même mécanique que tests/test_cockpit_smoke.py : monkeypatch des constantes."""
    import trader.cockpit.app as cockpit_module

    (tmp_path / "current_report.json").write_text(json.dumps({
        "ts": "2026-07-03T10:00:00+00:00",
        "dry_run": True,
        "portfolio": {"cash": 100000.0, "equity": 100000.0, "holdings": [
            {"symbol": "AAA.TW", "quantity": 10, "avg_price": 90.0, "last_price": 100.0},
        ]},
        "decisions": [{"symbol": "AAA.TW", "action": "BUY", "executed": True,
                       "confidence": 0.7, "ts": "2026-07-03T09:59:00Z"}],
    }), encoding="utf-8")
    monkeypatch.setattr(cockpit_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(cockpit_module, "_EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(cockpit_module, "_KILL_FILE", tmp_path / "KILL")
    import trader.read_models.runtime_state as rs
    monkeypatch.setattr(rs, "_STATE_DIR", tmp_path)


async def test_decisions_table_montee_et_peuplee(tmp_path, monkeypatch):
    from trader.cockpit.home import DecisionsTable
    from trader.cockpit import CockpitApp

    _patch_state_paths(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        app._schedule_refresh_state()
        await pilot.pause(1.0)
        table = app.query_one(DecisionsTable)
        assert table.row_count >= 1
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_cockpit_interactions.py -v`
Expected: FAIL — `ImportError: cannot import name 'DecisionsTable'`

- [ ] **Step 3: Implémentation**

```python
# à ajouter dans trader/cockpit/home.py
from textual.message import Message
from textual.widgets import DataTable


class SymbolChosen(Message):
    """Une ligne portant un symbole a été validée (Enter)."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        super().__init__()


class _SymbolTable(DataTable):
    """Base : cursor row + Enter → SymbolChosen(symbol extrait de la row key)."""

    def on_mount(self) -> None:
        self.cursor_type = "row"

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        raw = str(event.row_key.value or "")
        symbol = raw.split("|", 1)[0]
        if symbol and symbol != "—":
            self.post_message(SymbolChosen(symbol))


class DecisionsTable(_SymbolTable):
    def on_mount(self) -> None:
        super().on_mount()
        self.add_columns("UTC", "Sym", "Act", "État", "Conf", "Suite")

    def refresh_rows(
        self, decisions: list[dict], recent_decisions: list[dict], *, palette: Palette
    ) -> None:
        from trader.cockpit.aggregates import decision_status

        self.clear()
        for index, row in enumerate(select_decision_rows(decisions, recent_decisions, limit=8)):
            symbol = str(row.get("symbol") or "—")
            action = str(row.get("action") or "—").upper()
            status = decision_status(row)
            confidence = _safe_float(row.get("confidence"), default=None)
            action_style = (palette["action_buy"] if action == "BUY"
                            else palette["action_sell"] if action == "SELL" else palette["dim"])
            status_style = {
                "exec": palette["status_nominal"], "risk": palette["pnl_negative"],
                "stale": palette["kpi_vol_warn"], "quiet": palette["dim"],
            }.get(status, palette["status_accent"])
            self.add_row(
                _decision_time_label(row),
                Text(symbol, style="bold"),
                Text(action, style=action_style),
                Text(status, style=status_style),
                f"{confidence:.2f}" if confidence is not None else "—",
                _decision_effect_label(row),
                key=f"{symbol}|{row.get('cycle_ts') or row.get('ts') or ''}|{index}",
            )


class PlansTable(_SymbolTable):
    def on_mount(self) -> None:
        super().on_mount()
        self.add_columns("Type", "Sym", "Détail", "Déclencheur / Risque", "Exp.")

    def refresh_rows(self, state: dict, *, palette: Palette, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self.clear()
        armed = _safe_list_of_dicts(state.get("armed_plans"))
        trade_plans = _safe_list_of_dicts(state.get("trade_plans"))
        armed_ids = {id(w) for w in armed}
        watches = [w for w in _safe_list_of_dicts(state.get("indicator_watches"))
                   if id(w) not in armed_ids]
        index = 0
        for watch in armed:
            symbol = str(watch.get("symbol") or "—")
            self.add_row(
                Text("armé", style=f"bold {palette['status_accent']}"),
                Text(symbol, style="bold"),
                f"{_armed_order_label(watch)} {_armed_stop_label(watch)}",
                _condition_summary(watch.get("conditions"), watch.get("logic"), max_items=2),
                _relative_expiry(watch.get("expires_at"), now=now),
                key=f"{symbol}|armed|{index}",
            ); index += 1
        exit_rows = []
        for plan in trade_plans:
            symbol = str(plan.get("symbol") or "—")
            price = _price_for_symbol(state, symbol)
            stale = _symbol_is_stale(state, symbol)
            exit_rows.append((0 if stale else 1, symbol, plan, price, stale))
        for _, symbol, plan, price, stale in sorted(exit_rows, key=lambda r: (r[0], r[1]))[:8]:
            self.add_row(
                Text("sortie", style=palette["kpi_default"]),
                Text(symbol, style="bold"),
                f"{_plan_qty_label(plan)} · {_plan_state_label(plan)}",
                Text(f"{_exit_plan_risk_label(plan, price, stale)} · {_next_tp_label(plan, price)}",
                     style=palette["kpi_vol_warn"] if stale else palette["dim"]),
                "—",
                key=f"{symbol}|exit|{index}",
            ); index += 1
        for watch in sorted(watches, key=lambda w: str(w.get("expires_at") or ""))[:5]:
            symbol = str(watch.get("symbol") or "—")
            self.add_row(
                Text("veille", style=palette["dim"]),
                Text(symbol, style="bold"),
                "WAKE",
                _condition_summary(watch.get("conditions"), watch.get("logic"), max_items=2),
                _relative_expiry(watch.get("expires_at"), now=now),
                key=f"{symbol}|watch|{index}",
            ); index += 1


class PositionsTable(_SymbolTable):
    def on_mount(self) -> None:
        super().on_mount()
        self.add_columns("Sym", "Dev.", "Qté", "Dernier", "Expo USD", "PnL USD",
                         "Stop", "Perte@stop")

    def refresh_rows(self, state: dict, *, palette: Palette) -> None:
        self.clear()
        portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
        trade_plans = _safe_list_of_dicts(state.get("trade_plans"))
        holdings = sorted(
            _safe_list_of_dicts(portfolio.get("holdings")),
            key=lambda item: max(abs(_holding_pnl(item)), _holding_notional(item)),
            reverse=True,
        )
        for holding in holdings[:5]:
            symbol = _holding_symbol(holding)
            pnl_value = _holding_pnl(holding)
            pnl_style = palette["pnl_positive"] if pnl_value >= 0 else palette["pnl_negative"]
            stop_label, dist_label, loss_label, state_label = _stop_risk_for_holding(
                holding, _plan_for_symbol(trade_plans, symbol)
            )
            self.add_row(
                Text(symbol, style="bold"),
                _holding_currency(holding),
                _fmt_compact_float(holding.get("quantity"), decimals=2),
                _fmt_compact_float(_holding_native_price(holding), decimals=2),
                _fmt_compact_float(_holding_notional(holding), decimals=0),
                Text(_fmt_signed_compact_float(pnl_value, decimals=0), style=pnl_style),
                f"{stop_label} {dist_label}",
                Text(loss_label, style=palette["pnl_negative"]
                     if state_label == "risque" else palette["dim"]),
                key=symbol,
            )
```

`HomePane` : dans `compose()`, remplacer `Static(id="home-decisions")` par `DecisionsTable(id="home-decisions")`, `Static(id="home-plans")` par `PlansTable(id="home-plans")` ; la tuile portefeuille devient un conteneur Vertical `#home-portfolio` avec `Static(id="home-portfolio-summary")` et `PositionsTable(id="home-positions")`. Renommer `build_portfolio_tile` → `build_portfolio_summary(state: dict, *, palette: Palette) -> RenderableType` (même code amputé de sa table positions, désormais portée par `PositionsTable`). Dans `update_state`, appeler `refresh_rows(...)` sur les trois tables. `build_decisions_tile` (version Static de Task 9) est supprimée ; adapter `test_build_portfolio_tile_risque_fusionne` : le rendu du risque fusionné se vérifie sur une instance `PositionsTable` montée (via `run_test` d'une mini-app de test) ou en gardant l'assertion « pas de table Risque sorties séparée » sur `build_portfolio_summary`.

DEFAULT_CSS de `HomePane` : ajouter `#home-portfolio Static { height: auto; }` et pour les DataTables `#home-decisions, #home-plans { border: solid $primary; }`.

- [ ] **Step 4: Vérifier le passage**

Run: `uv run pytest tests/test_cockpit_interactions.py tests/test_cockpit_home.py tests/test_cockpit_smoke.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/cockpit/home.py tests/
git commit -m "feat(cockpit): DataTables navigables décisions/plans/positions + SymbolChosen (A4)"
```

---

### Task 11: `SymbolDetailScreen` (Enter → contexte complet du symbole) + Tab réservé au focus

**Files:**
- Modify: `trader/cockpit/home.py` (builder + modal), `trader/cockpit/app.py` (handler `SymbolChosen`, retrait des bindings pages tab/flèches l.1033-1036, hint nav `CockpitNav.update_page` l.335-348)
- Modify: `tests/test_cockpit_smoke.py:505-520` (navigation par chiffres au lieu de tab/flèches)
- Test: `tests/test_cockpit_interactions.py`

**Interfaces:**
- Consumes: `SymbolChosen`, builders overview filtrés, `_build_exit_plans_enriched`/`_build_learnings_panel` (`trader.ui.rich_panels`).
- Produces: `build_symbol_detail(state: dict, symbol: str, *, palette: Palette) -> RenderableType` (pur) · `SymbolDetailScreen(ModalScreen[None])` (`__init__(symbol: str)`, Esc ferme) · `CockpitApp.on_symbol_chosen(message)` → `push_screen`.

- [ ] **Step 1: Tests qui échouent**

```python
def test_build_symbol_detail_sections():
    from trader.cockpit.home import build_symbol_detail
    from trader.ui.palette import PALETTE_LIGHT

    state = {
        "portfolio": {"holdings": [{"symbol": "AAA.TW", "quantity": 10,
                                    "avg_price": 90.0, "last_price": 100.0}]},
        "trade_plans": [{"symbol": "AAA.TW", "side": "LONG", "hard_stop_price": 95.0,
                         "entry_price": 90.0, "remaining_quantity": 10}],
        "recent_decisions": [{"symbol": "AAA.TW", "action": "BUY", "executed": True,
                              "ts": "2026-07-03T09:00:00Z"},
                             {"symbol": "BBB.TW", "action": "HOLD",
                              "ts": "2026-07-03T09:00:00Z"}],
        "learnings": [{"symbol": "AAA.TW", "note": "gap à l'open fréquent",
                       "ts": "2026-07-01T00:00:00Z"}],
        "stale_streaks": {"AAA.TW": 2},
    }
    rendered = _console_render(build_symbol_detail(state, "AAA.TW", palette=PALETTE_LIGHT))
    assert "AAA.TW" in rendered
    assert "95" in rendered                    # plan de sortie
    assert "gap à l'open" in rendered          # learning du symbole
    assert "BBB.TW" not in rendered            # filtré par symbole
    assert "stale" in rendered.lower()         # santé data


async def test_enter_ouvre_modal_et_esc_ferme(tmp_path, monkeypatch):
    from trader.cockpit.home import DecisionsTable, SymbolDetailScreen
    from trader.cockpit import CockpitApp

    _patch_state_paths(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        app._schedule_refresh_state()
        await pilot.pause(1.0)
        table = app.query_one(DecisionsTable)
        table.focus()
        await pilot.press("enter")
        assert isinstance(app.screen, SymbolDetailScreen)
        await pilot.press("escape")
        assert not isinstance(app.screen, SymbolDetailScreen)


async def test_pages_par_chiffres_tab_ne_change_plus_de_page(tmp_path, monkeypatch):
    from trader.cockpit import CockpitApp

    _patch_state_paths(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        await pilot.press("3")
        assert app._active_page_key == "decisions"
        await pilot.press("tab")
        assert app._active_page_key == "decisions"  # tab = focus, plus de changement de page


async def test_tab_focus_une_datatable(tmp_path, monkeypatch):
    """Tab (libéré des pages) donne le focus à une DataTable de la home."""
    from textual.widgets import DataTable
    from trader.cockpit import CockpitApp

    _patch_state_paths(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        await pilot.press("tab")
        assert isinstance(app.focused, DataTable)
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_cockpit_interactions.py -v -k "symbol_detail or modal or chiffres"`
Expected: FAIL — `ImportError: cannot import name 'build_symbol_detail'`

- [ ] **Step 3: Implémentation**

```python
# à ajouter dans trader/cockpit/home.py
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen

from trader.ui.rich_panels import _build_exit_plans_enriched, _build_learnings_panel


def build_symbol_detail(state: dict, symbol: str, *, palette: Palette) -> RenderableType:
    """Contexte complet d'un symbole — pur filtrage du read model en mémoire."""
    state = state if isinstance(state, dict) else {}
    portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
    holdings = [h for h in _safe_list_of_dicts(portfolio.get("holdings"))
                if _holding_symbol(h) == symbol]
    plans = [p for p in _safe_list_of_dicts(state.get("trade_plans"))
             if str(p.get("symbol") or "") == symbol]
    armed = [w for w in _safe_list_of_dicts(state.get("armed_plans"))
             if str(w.get("symbol") or "") == symbol]
    decisions = [d for d in _safe_list_of_dicts(state.get("recent_decisions"))
                 if str(d.get("symbol") or "") == symbol][-8:]
    learnings = [l for l in _safe_list_of_dicts(state.get("learnings"))
                 if str(l.get("symbol") or "") == symbol]
    streak = (state.get("stale_streaks") or {}).get(symbol, 0)

    parts: list[RenderableType] = [Text(symbol, style=f"bold {palette['status_equity']}")]

    if holdings:
        holding = holdings[0]
        pnl_value = _holding_pnl(holding)
        pnl_style = palette["pnl_positive"] if pnl_value >= 0 else palette["pnl_negative"]
        parts.append(Text.assemble(
            ("Position : ", palette["dim"]),
            (f"{_fmt_compact_float(holding.get('quantity'), decimals=2)} @ "
             f"{_fmt_compact_float(_holding_native_price(holding), decimals=2)} "
             f"{_holding_currency(holding)}", "bold"),
            ("  PnL ", palette["dim"]),
            (_fmt_signed_compact_float(pnl_value, decimals=0), pnl_style),
        ))
    else:
        parts.append(Text("Aucune position ouverte", style=palette["dim"]))

    if plans:
        parts.append(_build_exit_plans_enriched(plans, palette=palette))
    if armed:
        parts.append(Text(f"{len(armed)} plan(s) armé(s) : "
                          + ", ".join(_armed_order_label(w) for w in armed),
                          style=palette["status_accent"]))

    table = Table(title="Décisions récentes", show_header=True, expand=True, box=None)
    for column in ("UTC", "Act", "État", "Conf", "Suite"):
        table.add_column(column, overflow="fold" if column == "Suite" else "ellipsis")
    from trader.cockpit.aggregates import decision_status

    for row in decisions:
        confidence = _safe_float(row.get("confidence"), default=None)
        table.add_row(
            _decision_time_label(row),
            str(row.get("action") or "—").upper(),
            decision_status(row),
            f"{confidence:.2f}" if confidence is not None else "—",
            _decision_effect_label(row),
        )
    if not decisions:
        table.add_row("—", "—", "—", "—", "aucune décision récente")
    parts.append(table)

    learnings_panel = _build_learnings_panel(learnings, palette=palette)
    if learnings_panel is not None:
        parts.append(learnings_panel)

    health = Text.assemble(("Santé data : ", palette["dim"]))
    if streak:
        health.append(f"stale ×{int(streak)}", style=palette["kpi_vol_warn"])
    else:
        health.append("OK", style=palette["status_nominal"])
    parts.append(health)

    return Group(*parts)


class SymbolDetailScreen(ModalScreen[None]):
    """Drill-down symbole (Enter depuis une table de la home). Esc ferme."""

    BINDINGS = [Binding("escape", "close_detail", "Fermer")]
    DEFAULT_CSS = """
    SymbolDetailScreen { align: center middle; }
    SymbolDetailScreen > VerticalScroll {
        width: 90%;
        height: 90%;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, symbol: str, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._symbol = symbol

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(id="symbol-detail-body")

    def on_mount(self) -> None:
        app = self.app
        state = getattr(app, "_last_state", None) or {}
        palette = app._current_palette()  # type: ignore[attr-defined]
        self.query_one("#symbol-detail-body", Static).update(
            build_symbol_detail(state, self._symbol, palette=palette)
        )

    def action_close_detail(self) -> None:
        self.dismiss(None)
```

Dans `app.py` :
1. Handler (méthode de `CockpitApp`) :
```python
def on_symbol_chosen(self, message: "SymbolChosen") -> None:
    from trader.cockpit.home import SymbolDetailScreen

    self.push_screen(SymbolDetailScreen(message.symbol))
```
(import `SymbolChosen` en tête de module pour le type ; le nom du handler suit la convention Textual `on_<namespace>_<message>` — avec une classe `SymbolChosen` définie dans `home.py`, le handler est `on_home_symbol_chosen`… **Textual dérive le namespace du nom de classe** : `SymbolChosen` → `on_symbol_chosen`. Garder `on_symbol_chosen` et vérifier via le test `test_enter_ouvre_modal_et_esc_ferme`.)
2. BINDINGS : supprimer les 4 lignes tab/right/left/shift+tab (l.1033-1036). Conserver `action_next_page`/`action_previous_page` (inoffensives, utilisées par d'anciens tests si besoin).
3. `CockpitNav.update_page` : remplacer le préfixe `"Tab/←/→ vue"` par `"1-6 vues · Tab focus · Enter détail"`.
4. `tests/test_cockpit_smoke.py:505-520` : remplacer `press("tab")`/`press("right")`/`press("left")` par `press("2")`/`press("3")`/`press("2")` avec les mêmes assertions de page active.

- [ ] **Step 4: Vérifier le passage**

Run: `uv run pytest tests/test_cockpit_interactions.py tests/test_cockpit_smoke.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/cockpit/home.py trader/cockpit/app.py tests/
git commit -m "feat(cockpit): modal détail symbole (Enter) + Tab réservé au focus (A4)"
```

---

### Task 12: Filtres du flux — classes d'événements (`F`) et regex (`/`)

**Files:**
- Modify: `trader/cockpit/app.py` (`LogsPane` : buffer + filtres ; `CockpitApp` : bindings + modals)
- Test: `tests/test_cockpit_interactions.py`

**Interfaces:**
- Consumes: `EventClass`, `EventLine`, `format_event_line` (`trader.cockpit.events`).
- Produces: `LogsPane.set_filters(classes: set[EventClass] | None, regex_text: str | None) -> None` (None = pas de filtre ; regex invalide ignorée silencieusement) ; buffer interne `deque[EventLine]` maxlen 500 ; re-rendu complet à chaque changement de filtre. `ClassFilterModal(ModalScreen[set[EventClass] | None])` (une Checkbox par classe) · `RegexModal(ModalScreen[str | None])` (un Input). Bindings app : `F` → classes, `slash` → regex (appliqués au pane visible : `#logs-pane` si page logs, sinon `#home-flux`).

- [ ] **Step 1: Tests qui échouent**

```python
async def test_flux_filtre_regex_et_classes(tmp_path, monkeypatch):
    import json

    from textual.widgets import RichLog
    from trader.cockpit.events import EventClass
    from trader.cockpit import CockpitApp

    _patch_state_paths(monkeypatch, tmp_path)
    events = tmp_path / "events.jsonl"
    lines = [
        {"ts": "2026-07-03T01:00:00Z", "event": "decision_recorded", "symbol": "AAA.TW",
         "action": "BUY", "executed": True},
        {"ts": "2026-07-03T01:00:01Z", "event": "decision_recorded", "symbol": "BBB.TW",
         "action": "HOLD"},
        {"ts": "2026-07-03T01:00:02Z", "event": "cycle_completed", "decisions_done": 0},
    ]
    events.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")

    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        pane = app.query_one("#logs-pane")
        pane.poll_events(events)
        base_count = pane.query_one(RichLog).line_count

        pane.set_filters(None, "AAA")
        assert pane.query_one(RichLog).line_count < base_count

        pane.set_filters({EventClass.DECISION_EXECUTED}, None)
        rendered_count = pane.query_one(RichLog).line_count
        assert rendered_count >= 1  # le BUY exécuté passe

        pane.set_filters(None, None)  # reset
        assert pane.query_one(RichLog).line_count >= base_count - 1


async def test_binding_slash_ouvre_regex_modal(tmp_path, monkeypatch):
    from trader.cockpit.app import RegexModal
    from trader.cockpit import CockpitApp

    _patch_state_paths(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        await pilot.press("slash")
        assert isinstance(app.screen, RegexModal)
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_cockpit_interactions.py -v -k "flux or slash"`
Expected: FAIL — `AttributeError: 'LogsPane' object has no attribute 'set_filters'`

- [ ] **Step 3: Implémentation**

`LogsPane` (`app.py`) — ajouter le buffer + les filtres :

```python
import re
from collections import deque

class LogsPane(Static):
    # ... attrs existants ...
    _class_filter: set[EventClass] | None = None
    _regex: "re.Pattern[str] | None" = None

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._buffer: deque[EventLine] = deque(maxlen=_MAX_EVENT_LINES)

    def _passes(self, line: EventLine) -> bool:
        if line.markup_class == EventClass.CYCLE and not self._show_cycles:
            return False
        if self._class_filter is not None and line.markup_class not in self._class_filter:
            return False
        if self._regex is not None and not self._regex.search(line.text):
            return False
        return True

    def _write_line(self, log: RichLog, line: EventLine) -> None:
        style = _event_styles_for_palette(self._current_palette).get(line.markup_class, "")
        log.write(Text(line.text, style=style))

    def set_filters(self, classes: "set[EventClass] | None", regex_text: str | None) -> None:
        """Applique les filtres et re-rend tout le buffer. Regex invalide → ignorée."""
        self._class_filter = classes
        self._regex_text = regex_text or None  # mémorisé pour pré-remplir les modals
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
```

Dans `poll_events`, remplacer la boucle d'écriture par :

```python
for ev_dict in new_dicts:
    ev_line = format_event_line(ev_dict)
    self._buffer.append(ev_line)
    if self._passes(ev_line):
        self._write_line(log, ev_line)
```
Déclarer l'attribut de classe `_regex_text: str | None = None` à côté de `_class_filter`, et réécrire `toggle_cycles` pour re-rendre via le même chemin :

```python
    def toggle_cycles(self) -> None:
        self._show_cycles = not self._show_cycles
        self.set_filters(self._class_filter, self._regex_text)
```

Modals (`app.py`) :

```python
from textual.widgets import Checkbox, Input


class ClassFilterModal(ModalScreen["set[EventClass] | None"]):
    """Toggles par classe d'événement (à la Ctrl+F de Gonzo)."""

    BINDINGS = [Binding("escape", "cancel", "Annuler", show=False)]
    DEFAULT_CSS = _confirm_modal_css("ClassFilterModal", border="$primary", width=44)

    def __init__(self, active: "set[EventClass] | None", **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._active = active

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Classes d'événements affichées")
            for event_class in EventClass:
                yield Checkbox(
                    event_class.name.lower(),
                    value=self._active is None or event_class in self._active,
                    id=f"class-{event_class.name}",
                )
            with Horizontal():
                yield Button("Appliquer", id="class-filter-apply", variant="primary")
                yield Button("Tout", id="class-filter-all", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "class-filter-all":
            self.dismiss(None)
            return
        selected = {
            event_class
            for event_class in EventClass
            if self.query_one(f"#class-{event_class.name}", Checkbox).value
        }
        self.dismiss(selected if len(selected) < len(list(EventClass)) else None)

    def action_cancel(self) -> None:
        self.dismiss(self._active)


class RegexModal(ModalScreen["str | None"]):
    """Filtre regex sur le texte des lignes du flux."""

    BINDINGS = [Binding("escape", "cancel", "Annuler", show=False)]
    DEFAULT_CSS = _confirm_modal_css("RegexModal", border="$primary", width=60)

    def __init__(self, current: str | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._current = current or ""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Filtre regex (vide = aucun)")
            yield Input(value=self._current, placeholder="ex. 2303|SELL", id="regex-input")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value or None)

    def action_cancel(self) -> None:
        self.dismiss(self._current or None)
```

`CockpitApp` :
```python
BINDINGS = [
    # ... existants ...
    Binding("F", "filter_classes", "Filtre classes"),
    Binding("slash", "filter_regex", "Filtre regex"),
]

def _visible_flux(self) -> LogsPane:
    pane_id = "#logs-pane" if self._active_page_key == "logs" else "#home-flux"
    return self.query_one(pane_id)  # type: ignore[return-value]

def action_filter_classes(self) -> None:
    pane = self._visible_flux()

    def _apply(classes: "set[EventClass] | None") -> None:
        pane.set_filters(classes, pane._regex_text)

    self.push_screen(ClassFilterModal(pane._class_filter), _apply)

def action_filter_regex(self) -> None:
    pane = self._visible_flux()

    def _apply(regex_text: str | None) -> None:
        pane.set_filters(pane._class_filter, regex_text)

    self.push_screen(RegexModal(pane._regex_text), _apply)
```

- [ ] **Step 4: Vérifier le passage**

Run: `uv run pytest tests/test_cockpit_interactions.py tests/test_cockpit_events.py tests/test_cockpit_smoke.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/cockpit/app.py tests/test_cockpit_interactions.py
git commit -m "feat(cockpit): flux filtrable — classes d'événements (F) et regex (/) façon Gonzo (A5)"
```

---

### Task 13: Thème sombre par défaut (`casys-ink` gruvbox) + `PALETTE_INK`

**Files:**
- Modify: `trader/ui/palette.py` (ajouter `PALETTE_INK` — `PALETTE_DARK` intouchée), `trader/cockpit/app.py:114-133` (`_THEME_INK` gruvbox, `_THEME_PALETTE`, thème par défaut)
- Modify: `tests/test_cockpit_smoke.py:107-135` (défaut = ink, `d` bascule vers salmon)
- Test: `tests/test_cockpit_home.py`

**Interfaces:**
- Produces: `PALETTE_INK: Palette` (tokens gruvbox, mêmes clés que `ALL_PALETTE_KEYS`) ; thème Textual `casys-ink` re-teinté ; défaut app = `casys-ink`.

- [ ] **Step 1: Tests qui échouent**

```python
def test_palette_ink_complete():
    from trader.ui.palette import ALL_PALETTE_KEYS, PALETTE_INK

    assert set(PALETTE_INK.keys()) == set(ALL_PALETTE_KEYS)


async def test_theme_defaut_est_ink(tmp_path, monkeypatch):
    from trader.cockpit import CockpitApp

    _patch_state_paths(monkeypatch, tmp_path)
    app = CockpitApp()
    async with app.run_test(size=(200, 50)) as _:
        assert app.theme == "casys-ink"
```

Adapter dans `tests/test_cockpit_smoke.py` : `test_cockpit_theme_defaut_est_casys_salmon` → renommer `test_cockpit_theme_defaut_est_casys_ink` (asserte `"casys-ink"`) ; `test_cockpit_binding_d_bascule_theme` : `d` → `"casys-salmon"`, `d` à nouveau → `"casys-ink"`.

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_cockpit_home.py -v -k "ink" `
Expected: FAIL — `ImportError: cannot import name 'PALETTE_INK'`

- [ ] **Step 3: Implémentation**

```python
# à ajouter dans trader/ui/palette.py — après PALETTE_LIGHT
# ---------------------------------------------------------------------------
# PALETTE_INK — sombre gruvbox (cockpit « mode Gonzo », thème par défaut).
# PALETTE_DARK reste la palette historique de tui.py : NE PAS Y TOUCHER.
# ---------------------------------------------------------------------------
PALETTE_INK: Palette = {
    "border_default": "#665c54",
    "border_attribution": "#665c54",
    "border_learnings": "#665c54",
    "border_plans": "#665c54",
    "border_watches": "#665c54",
    "border_llm_activity": "#665c54",
    "equity_line": "#8ec07c",
    "equity_dim": "#928374",
    "kpi_sharpe_ok": "#b8bb26",
    "kpi_sharpe_bad": "#fb4934",
    "kpi_default": "#83a598",
    "kpi_vol_warn": "#d79921",
    "pnl_positive": "#b8bb26",
    "pnl_negative": "#fb4934",
    "symbol_bold": "bold #83a598",
    "action_buy": "#b8bb26",
    "action_sell": "#fb4934",
    "action_hold": "#928374",
    "learning_symbol": "bold #83a598",
    "dim": "#928374",
    "status_equity": "bold #ebdbb2",
    "status_accent": "#83a598",
    "status_phase": "#d3869b",
    "status_nominal": "#b8bb26",
    "event_decision_exec": "bold #b8bb26",
    "event_risk_reject": "bold #fb4934",
    "event_stale": "#928374",
    "event_hold": "#a89984",
    "event_watch": "bold #8ec07c",
    "event_learning": "#83a598",
    "event_cycle": "#7c6f64",
    "event_error": "bold #d79921",
    "event_other": "#928374",
}
```

Dans `app.py` :

```python
_THEME_INK = Theme(
    name="casys-ink",
    dark=True,
    primary="#83a598",
    secondary="#928374",
    warning="#d79921",
    error="#fb4934",
    success="#b8bb26",
    accent="#8ec07c",
    foreground="#ebdbb2",
    background="#1d2021",
    surface="#282828",
    panel="#32302f",
)

_THEME_PALETTE: dict[str, Palette] = {
    "casys-salmon": PALETTE_LIGHT,
    "casys-ink": PALETTE_INK,
}
```
…et dans `on_mount` (l.1074) : `self.theme = "casys-ink"` (import `PALETTE_INK` depuis `trader.ui.palette`). `_current_palette` : fallback `PALETTE_INK` au lieu de `PALETTE_DARK`.

- [ ] **Step 4: Vérifier le passage + captures finales**

Run: `uv run pytest tests/test_cockpit_home.py tests/test_cockpit_smoke.py tests/test_cockpit_interactions.py tests/test_cockpit_aggregates.py tests/test_cockpit_events.py -v`
Expected: PASS.
Run: `uv run python scripts/cockpit_screenshots.py` — **regarder** `home.svg.png` (ink) et `home-alt-theme.svg.png` (salmon) : lisibilité, contrastes, aucun élément noir/cyan hardcodé résiduel.

- [ ] **Step 5: Commit**

```bash
git add trader/ui/palette.py trader/cockpit/app.py tests/
git commit -m "feat(cockpit): thème sombre gruvbox par défaut (casys-ink) + PALETTE_INK (A5)"
```

---

### Task 14: Vérification finale de bout en bout

**Files:**
- Aucun nouveau fichier — validation.

- [ ] **Step 1: Suite complète**

Run: `uv run pytest -v`
Expected: PASS (zéro régression sur les ~39 tests smoke + suites tui/events/aggregates/home/interactions).

- [ ] **Step 2: Lancer le vrai cockpit sur les données live**

Run: `make watch` (daemon vivant) — vérifier à la main :
1. statut : kill/mode visibles, rétrécir le terminal → les criticités restent ;
2. ligne à surveiller cohérente avec l'état ;
3. Tab circule entre les 3 DataTables, Enter ouvre le détail d'un symbole réel, Esc ferme ;
4. `F` et `/` filtrent le flux ; `f` pause ;
5. `d` bascule ink ↔ salmon sans artefact ;
6. pages 2-6 inchangées et fonctionnelles.

- [ ] **Step 3: Captures avant/après pour le journal du chantier**

Run: `uv run python scripts/cockpit_screenshots.py docs/assets/cockpit-gonzo-after` puis comparer aux captures « avant » de l'analyse. Commit final :

```bash
git add docs/assets/cockpit-gonzo-after
git commit -m "docs(cockpit): captures après refonte home mode Gonzo (A1-A5 livrées)"
```
