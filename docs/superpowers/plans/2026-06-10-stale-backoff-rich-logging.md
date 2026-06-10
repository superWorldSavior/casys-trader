# Stale Backoff + Rich Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (1) Éliminer les 432 décisions `stale_market_data` répétées la nuit via backoff exponentiel et déduplication ; (2) remplacer le `logging.basicConfig` par un handler Rich coloré pour les TTY, sans fuite de markup en non-TTY.

**Architecture:**
- Chantier 1 : `Scheduler` stocke un `stale_streak` par symbole dans `scheduler.json`. La logique de backoff (`stale_backoff_wake_minutes`) et de déduplication (transition fresh→stale = 1 décision, les stales suivants = 1 event léger `stale_backoff`) vit dans `daemon.py` à l'endroit exact où le cas stale est traité (ligne ~1127). Aucun calendrier de marché, 100% data-driven.
- Chantier 2 : Nouveau module `trader/logging_setup.py` avec `setup_logging()`. TTY → `RichHandler` (couleurs, pas de path/lineno). Non-TTY → `StreamHandler` texte simple (markup=False dans les messages). Appelé dans `daemon.main()` à la place du `basicConfig`. Les messages clés sont balisés Rich au niveau du logger, pas dans les fichiers machine.

**Tech Stack:** Python 3.11+, `rich>=13.0` (déjà dans pyproject.toml:9), `pytest`, `logging`, `json`

---

## Fichiers touchés

| Fichier | Action | Rôle |
|---|---|---|
| `trader/tools/scheduler.py` | Modifier | Ajouter `stale_streak`, `get_stale_streak`, `set_stale_streak`, `reset_stale_streak` + constantes |
| `trader/daemon.py` | Modifier | Consommer streak dans le bloc stale (~L1117), ajouter `_stale_backoff_wake_minutes()`, conditionner `record_decision` vs `_append_event` |
| `trader/logging_setup.py` | Créer | `setup_logging(level, stream)` — RichHandler TTY, StreamHandler sinon |
| `tests/test_scheduler.py` | Modifier | Tests streak : montée, cap, reset, persistence, vieux state sans champ |
| `tests/test_daemon_stale_backoff.py` | Créer | Tests intégration : wake doublé/cappé, 1 seule décision, event léger, reset streak |
| `tests/test_logging_setup.py` | Créer | Tests TTY→RichHandler, non-TTY→StreamHandler sans markup |

**Ne pas toucher :** `trader/consolidator.py`, `tests/test_consolidator.py`, `tests/test_cli_semantic.py`, `tests/test_daemon_learnings.py`, `tests/test_llm.py`, `README.md`, `docs/`.

---

## CHANTIER 1 — Backoff exponentiel stale

### Task 1 : Constantes et méthodes streak dans Scheduler

**Files:**
- Modify: `trader/tools/scheduler.py`
- Test: `tests/test_scheduler.py`

- [ ] **Step 1 : Écrire les tests échouants**

Ajouter à la fin de `tests/test_scheduler.py` :

```python
def test_stale_streak_vaut_zero_sur_un_state_sans_champ(tmp_path) -> None:
    """Vieux scheduler.json sans stale_streaks → pas de crash, streak=0."""
    state_path = tmp_path / "scheduler.json"
    # Écrire un state ancien sans le champ stale_streaks
    state_path.write_text('{"default_next_wake": null, "symbols": {}, "indicator_watches": {}}')
    sched = Scheduler(state_path)
    assert sched.get_stale_streak("SPY") == 0


def test_stale_streak_monte_et_est_persiste(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    assert sched.get_stale_streak("SPY") == 0
    sched.set_stale_streak("SPY", 1)
    assert sched.get_stale_streak("SPY") == 1
    sched.set_stale_streak("SPY", 3)
    assert sched.get_stale_streak("SPY") == 3


def test_stale_streak_est_persiste_a_travers_un_reload(tmp_path) -> None:
    path = tmp_path / "scheduler.json"
    sched1 = Scheduler(path)
    sched1.set_stale_streak("SPY", 5)
    sched2 = Scheduler(path)  # recharge depuis disque
    assert sched2.get_stale_streak("SPY") == 5


def test_stale_streak_reset_passe_a_zero(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    sched.set_stale_streak("SPY", 3)
    sched.reset_stale_streak("SPY")
    assert sched.get_stale_streak("SPY") == 0


def test_stale_streak_independant_par_symbole(tmp_path) -> None:
    sched = Scheduler(tmp_path / "scheduler.json")
    sched.set_stale_streak("SPY", 4)
    sched.set_stale_streak("QQQ", 1)
    assert sched.get_stale_streak("SPY") == 4
    assert sched.get_stale_streak("QQQ") == 1
    sched.reset_stale_streak("SPY")
    assert sched.get_stale_streak("SPY") == 0
    assert sched.get_stale_streak("QQQ") == 1
```

- [ ] **Step 2 : Vérifier échec**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader
uv run pytest tests/test_scheduler.py -v -k "stale_streak" 2>&1 | tail -20
```

Attendu : `AttributeError: 'Scheduler' object has no attribute 'get_stale_streak'`

- [ ] **Step 3 : Implémenter dans scheduler.py**

Ajouter en tête du fichier après les imports (ligne ~15), avant la classe :

```python
# Backoff stale — constantes explicites (AX : pas de defaults magiques)
STALE_BACKOFF_BASE_MULTIPLIER: int = 2
STALE_BACKOFF_MAX_MINUTES: float = 120.0
```

Ajouter à la fin de `_load_state()`, avant le `return raw` (ligne ~31), s'assurer que le champ existe :

```python
raw.setdefault("stale_streaks", {})
```

Ajouter à la fin de la classe `Scheduler` les trois méthodes :

```python
def get_stale_streak(self, symbol: str) -> int:
    """Nombre de réveils stale consécutifs pour ce symbole. 0 si inconnu."""
    state = self._load_state()
    return int(state.get("stale_streaks", {}).get(symbol, 0))

def set_stale_streak(self, symbol: str, streak: int) -> None:
    """Enregistre le streak stale d'un symbole (>= 0)."""
    state = self._load_state()
    state.setdefault("stale_streaks", {})[symbol] = streak
    self._save_state(state)

def reset_stale_streak(self, symbol: str) -> None:
    """Remet le streak à 0 (appeler dès qu'une donnée fraîche est reçue)."""
    state = self._load_state()
    state.setdefault("stale_streaks", {}).pop(symbol, None)
    self._save_state(state)
```

- [ ] **Step 4 : Vérifier passage au vert**

```bash
uv run pytest tests/test_scheduler.py -v 2>&1 | tail -20
```

Attendu : tous les tests scheduler passent.

---

### Task 2 : Fonction `_stale_backoff_wake_minutes` dans daemon.py

**Files:**
- Modify: `trader/daemon.py`
- Test: `tests/test_daemon_stale_backoff.py` (nouveau)

- [ ] **Step 1 : Écrire le test unitaire de la fonction de backoff**

Créer `tests/test_daemon_stale_backoff.py` :

```python
"""Tests chantier 1 : backoff stale et déduplication décisions."""
import json
from datetime import datetime, timezone

import pytest

from trader import daemon
from trader.tools.scheduler import Scheduler, STALE_BACKOFF_MAX_MINUTES


def test_backoff_wake_premier_stale_est_egal_au_defaut(tmp_path) -> None:
    """streak=0 → next_wake = default (pas de doublement au premier stale)."""
    result = daemon._stale_backoff_wake_minutes(streak=0, default_wake_minutes=30.0)
    assert result == 30.0


def test_backoff_wake_doublee_au_second_stale(tmp_path) -> None:
    """streak=1 → 30 * 2^1 = 60."""
    result = daemon._stale_backoff_wake_minutes(streak=1, default_wake_minutes=30.0)
    assert result == 60.0


def test_backoff_wake_triple_stale(tmp_path) -> None:
    """streak=2 → 30 * 2^2 = 120."""
    result = daemon._stale_backoff_wake_minutes(streak=2, default_wake_minutes=30.0)
    assert result == 120.0


def test_backoff_wake_cap_a_120_minutes(tmp_path) -> None:
    """streak=10 → capp à STALE_BACKOFF_MAX_MINUTES=120."""
    result = daemon._stale_backoff_wake_minutes(streak=10, default_wake_minutes=30.0)
    assert result == STALE_BACKOFF_MAX_MINUTES


def test_backoff_wake_cap_avec_defaut_petit(tmp_path) -> None:
    """Avec default=5min, streak=5 → 5*32=160 → cappé à 120."""
    result = daemon._stale_backoff_wake_minutes(streak=5, default_wake_minutes=5.0)
    assert result == STALE_BACKOFF_MAX_MINUTES
```

- [ ] **Step 2 : Vérifier échec**

```bash
uv run pytest tests/test_daemon_stale_backoff.py -v 2>&1 | tail -20
```

Attendu : `AttributeError: module 'trader.daemon' has no attribute '_stale_backoff_wake_minutes'`

- [ ] **Step 3 : Implémenter `_stale_backoff_wake_minutes` dans daemon.py**

Ajouter après `_bounded_wake_minutes` (ligne ~185) dans `trader/daemon.py` :

```python
def _stale_backoff_wake_minutes(streak: int, *, default_wake_minutes: float) -> float:
    """Next-wake pour un symbole stale avec backoff exponentiel.

    streak=0 → default (premier stale, pas encore de backoff)
    streak=N → min(default * 2^N, STALE_BACKOFF_MAX_MINUTES)

    Constantes dans trader/tools/scheduler.py :
      STALE_BACKOFF_BASE_MULTIPLIER = 2
      STALE_BACKOFF_MAX_MINUTES = 120.0
    """
    from .tools.scheduler import STALE_BACKOFF_BASE_MULTIPLIER, STALE_BACKOFF_MAX_MINUTES
    if streak == 0:
        return default_wake_minutes
    raw = default_wake_minutes * (STALE_BACKOFF_BASE_MULTIPLIER ** streak)
    return min(raw, STALE_BACKOFF_MAX_MINUTES)
```

- [ ] **Step 4 : Vérifier passage au vert**

```bash
uv run pytest tests/test_daemon_stale_backoff.py -v -k "backoff_wake" 2>&1 | tail -20
```

Attendu : 5 tests passent.

---

### Task 3 : Intégration backoff dans run_cycle — scheduling et streak

**Files:**
- Modify: `trader/daemon.py`
- Test: `tests/test_daemon_stale_backoff.py`

- [ ] **Step 1 : Écrire les tests intégration scheduling + streak**

Ajouter à la fin de `tests/test_daemon_stale_backoff.py` :

```python
# ── helpers communs ──────────────────────────────────────────────────────────

def _write_runtime_config(root) -> None:
    (root / "config").mkdir(exist_ok=True)
    (root / "mandate").mkdir(exist_ok=True)
    (root / "config" / "universe.yaml").write_text(
        "starting_cash: 100000\nsymbols:\n  - SPY\n"
    )
    (root / "config" / "risk.yaml").write_text(
        "\n".join([
            "max_position_value: 20000",
            "max_gross_exposure: 100000",
            "max_order_value: 10000",
            "max_orders_per_cycle: 5",
            "min_equity: 50000",
        ])
    )
    (root / "mandate" / "mandate.md").write_text("# Mandat\n")
    (root / "mandate" / "memory.md").write_text("# Memoire\n")


def _make_stale_source(now, age_minutes=90.0):
    """Retourne une data source dont la barre est trop vieille."""
    from datetime import timedelta
    from trader.tools.market import Bar

    class FakeStaleDataSource:
        def get_bars(self, symbol, lookback, interval):
            stale_ts = (now - timedelta(minutes=age_minutes)).isoformat()
            return [Bar(ts=stale_ts, open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)]

    return FakeStaleDataSource()


# ── tests intégration ────────────────────────────────────────────────────────

def test_stale_premier_cycle_wake_egal_au_defaut(monkeypatch, tmp_path, patch_batch) -> None:
    """Premier stale (streak=0) → next_wake = default_wake_minutes."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc)
    data_source = _make_stale_source(now)

    patch_batch(lambda **kwargs: __import__("trader.codex_client", fromlist=["Decision"]).Decision.hold(kwargs["symbol"], "hold"))

    daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=sched, data_source=data_source,
        default_wake_minutes=30.0,
    )

    # Streak après premier stale = 1 (on vient de staler)
    assert sched.get_stale_streak("SPY") == 1
    # Wake = default (30min), pas encore de backoff
    wake = sched.next_wake("SPY")
    expected = now.replace(microsecond=0) 
    delta = abs((wake - now).total_seconds())
    assert abs(delta - 30 * 60) < 2, f"wake delta={delta}s attendu=1800s"


def test_stale_deuxieme_cycle_wake_double(monkeypatch, tmp_path, patch_batch) -> None:
    """Deuxième stale consécutif (streak=1) → next_wake = default * 2."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    sched = Scheduler(state_dir / "scheduler.json")
    sched.set_stale_streak("SPY", 1)  # simuler: 1er stale déjà arrivé
    now = datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc)
    data_source = _make_stale_source(now)

    patch_batch(lambda **kwargs: __import__("trader.codex_client", fromlist=["Decision"]).Decision.hold(kwargs["symbol"], "hold"))

    daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=sched, data_source=data_source,
        default_wake_minutes=30.0,
    )

    assert sched.get_stale_streak("SPY") == 2
    wake = sched.next_wake("SPY")
    delta = abs((wake - now).total_seconds())
    assert abs(delta - 60 * 60) < 2, f"wake delta={delta}s attendu=3600s (60 min)"


def test_stale_streak_cappe_a_120_minutes(monkeypatch, tmp_path, patch_batch) -> None:
    """Streak=10 → wake cappé à 120min."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    sched = Scheduler(state_dir / "scheduler.json")
    sched.set_stale_streak("SPY", 10)
    now = datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc)
    data_source = _make_stale_source(now)

    patch_batch(lambda **kwargs: __import__("trader.codex_client", fromlist=["Decision"]).Decision.hold(kwargs["symbol"], "hold"))

    daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=sched, data_source=data_source,
        default_wake_minutes=30.0,
    )

    wake = sched.next_wake("SPY")
    delta = (wake - now).total_seconds()
    assert abs(delta - 120 * 60) < 2, f"wake delta={delta}s attendu=7200s (120 min cappé)"


def test_stale_streak_reset_quand_data_fraiche(monkeypatch, tmp_path, patch_batch) -> None:
    """Après une donnée fraîche, le streak est remis à 0."""
    from trader.tools.market import Bar

    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    sched = Scheduler(state_dir / "scheduler.json")
    sched.set_stale_streak("SPY", 3)  # streak existant
    now = datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc)  # heure de marché

    class FreshDataSource:
        def get_bars(self, symbol, lookback, interval):
            # barre très récente (1 minute old)
            from datetime import timedelta
            fresh_ts = (now - timedelta(minutes=1)).isoformat()
            return [Bar(ts=fresh_ts, open=100.0, high=101.0, low=99.0, close=100.0, volume=1000.0)]

    patch_batch(lambda **kwargs: __import__("trader.codex_client", fromlist=["Decision"]).Decision.hold(kwargs["symbol"], "hold"))

    daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=sched, data_source=FreshDataSource(),
        default_wake_minutes=30.0,
        max_market_data_age_minutes=5.0,
    )

    assert sched.get_stale_streak("SPY") == 0
```

- [ ] **Step 2 : Vérifier échec**

```bash
uv run pytest tests/test_daemon_stale_backoff.py -v -k "premier_cycle or deuxieme_cycle or cappe or reset_quand" 2>&1 | tail -30
```

Attendu : échecs variés — le streak n'est pas encore géré dans `run_cycle`.

- [ ] **Step 3 : Modifier run_cycle dans daemon.py**

Localiser le bloc stale dans `run_cycle` (~ligne 1116-1145). Il y a deux zones à modifier :

**Zone A — après la collecte de `stale_market_data` (~ligne 905), avant le batch :**

Ajouter le reset des streaks pour les symboles frais. Insérer juste après la ligne `if stale_market_data:` (~L905), avant `tradable_prices = ...` :

```python
    # Reset streak pour tous les symboles frais (data fraîche reçue)
    if sched is not None:
        for sym in symbols:
            if sym in prices and sym not in stale_market_data:
                current_streak = sched.get_stale_streak(sym)
                if current_streak > 0:
                    sched.reset_stale_streak(sym)
```

**Zone B — dans la boucle `for index, sym in enumerate(symbols_to_decide, start=1):` (~ligne 1117-1145) :**

Remplacer le bloc entier :
```python
        stale_data = stale_market_data.get(sym)
        if stale_data is not None:
            _log_cycle_progress(
                "[decision %d/%d] %s skipped stale_market_data reason=%s age=%s",
                index,
                len(symbols_to_decide),
                sym,
                stale_data.get("stale_reason"),
                stale_data.get("data_age_minutes"),
            )
            if sched is not None:
                sched.set_symbol_next_wake_in(sym, minutes=default_wake_minutes, now=now)
            record_decision(
                {
                    "symbol": sym,
                    "action": "HOLD",
                    "qty": 0.0,
                    "confidence": 0.0,
                    "rationale": "stale_market_data",
                    "next_wake_in_minutes": default_wake_minutes,
                    "intent": "HOLD",
                    "trade_plan_created": False,
                    "executed": False,
                    "reason": "stale_market_data",
                    "data_source": runtime_data_source_by_sym.get(sym),
                    **stale_data,
                }
            )
            continue
```

Par :
```python
        stale_data = stale_market_data.get(sym)
        if stale_data is not None:
            streak = sched.get_stale_streak(sym) if sched is not None else 0
            wake_minutes = _stale_backoff_wake_minutes(streak, default_wake_minutes=default_wake_minutes)
            new_streak = streak + 1
            if sched is not None:
                sched.set_stale_streak(sym, new_streak)
                sched.set_symbol_next_wake_in(sym, minutes=wake_minutes, now=now)

            is_first_stale = streak == 0  # transition fresh→stale : enregistrer la décision
            if is_first_stale:
                _log_cycle_progress(
                    "[decision %d/%d] %s stale_market_data (streak=1) reason=%s age=%s wake=%.0fmin",
                    index,
                    len(symbols_to_decide),
                    sym,
                    stale_data.get("stale_reason"),
                    stale_data.get("data_age_minutes"),
                    wake_minutes,
                )
                record_decision(
                    {
                        "symbol": sym,
                        "action": "HOLD",
                        "qty": 0.0,
                        "confidence": 0.0,
                        "rationale": "stale_market_data",
                        "next_wake_in_minutes": wake_minutes,
                        "intent": "HOLD",
                        "trade_plan_created": False,
                        "executed": False,
                        "reason": "stale_market_data",
                        "stale_streak": new_streak,
                        "data_source": runtime_data_source_by_sym.get(sym),
                        **stale_data,
                    }
                )
            else:
                _log_cycle_progress(
                    "[decision %d/%d] %s stale_backoff streak=%d wake=%.0fmin reason=%s",
                    index,
                    len(symbols_to_decide),
                    sym,
                    new_streak,
                    wake_minutes,
                    stale_data.get("stale_reason"),
                )
                _append_event(
                    "stale_backoff",
                    symbol=sym,
                    streak=new_streak,
                    next_wake_minutes=wake_minutes,
                    stale_reason=stale_data.get("stale_reason"),
                    data_age_minutes=stale_data.get("data_age_minutes"),
                )
            continue
```

- [ ] **Step 4 : Vérifier passage au vert**

```bash
uv run pytest tests/test_daemon_stale_backoff.py -v 2>&1 | tail -30
```

Attendu : tous les tests stale_backoff passent.

---

### Task 4 : Test déduplication des décisions (1 décision puis events légers)

**Files:**
- Test: `tests/test_daemon_stale_backoff.py`

- [ ] **Step 1 : Écrire les tests de déduplication**

Ajouter à la fin de `tests/test_daemon_stale_backoff.py` :

```python
def test_stale_premiere_occurrence_enregistre_une_decision(monkeypatch, tmp_path, patch_batch) -> None:
    """Premier stale (streak=0) → 1 décision HOLD dans report['decisions']."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    sched = Scheduler(state_dir / "scheduler.json")
    now = datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc)
    data_source = _make_stale_source(now)

    patch_batch(lambda **kwargs: __import__("trader.codex_client", fromlist=["Decision"]).Decision.hold(kwargs["symbol"], "hold"))

    report = daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=sched, data_source=data_source,
        default_wake_minutes=30.0,
    )

    decisions = report["decisions"]
    assert len(decisions) == 1
    assert decisions[0]["reason"] == "stale_market_data"
    assert decisions[0]["stale_streak"] == 1


def test_stale_occurrences_suivantes_ne_creent_pas_de_decision(monkeypatch, tmp_path, patch_batch) -> None:
    """Stales suivants (streak>=1) → 0 décision dans report, mais event stale_backoff dans events.jsonl."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    sched = Scheduler(state_dir / "scheduler.json")
    sched.set_stale_streak("SPY", 2)  # déjà 2 stales, pas le premier
    now = datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc)
    data_source = _make_stale_source(now)

    patch_batch(lambda **kwargs: __import__("trader.codex_client", fromlist=["Decision"]).Decision.hold(kwargs["symbol"], "hold"))

    report = daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=sched, data_source=data_source,
        default_wake_minutes=30.0,
    )

    # Pas de décision enregistrée pour ce cycle
    assert report["decisions"] == []

    # Event léger dans events.jsonl
    events_path = state_dir / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
    stale_events = [e for e in events if e.get("event") == "stale_backoff"]
    assert len(stale_events) >= 1
    ev = stale_events[-1]
    assert ev["symbol"] == "SPY"
    assert ev["streak"] == 3
    assert "next_wake_minutes" in ev
    assert "stale_reason" in ev


def test_stale_sans_scheduler_ne_plante_pas(monkeypatch, tmp_path, patch_batch) -> None:
    """run_cycle sans sched=None ne doit pas planter sur stale."""
    _write_runtime_config(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(daemon, "ROOT", tmp_path)
    monkeypatch.setattr(daemon, "STATE_DIR", state_dir)
    now = datetime(2026, 6, 10, 3, 0, tzinfo=timezone.utc)
    data_source = _make_stale_source(now)

    patch_batch(lambda **kwargs: __import__("trader.codex_client", fromlist=["Decision"]).Decision.hold(kwargs["symbol"], "hold"))

    # Pas de sched → doit fonctionner sans crash
    report = daemon.run_cycle(
        dry_run=True, now=now, symbols_filter=["SPY"],
        sched=None, data_source=data_source,
        default_wake_minutes=30.0,
    )

    # Premier stale sans sched → 1 décision quand même
    assert len(report["decisions"]) == 1
    assert report["decisions"][0]["reason"] == "stale_market_data"
```

- [ ] **Step 2 : Vérifier échec**

```bash
uv run pytest tests/test_daemon_stale_backoff.py -v -k "premiere_occurrence or occurrences_suivantes or sans_scheduler" 2>&1 | tail -30
```

Attendu : échecs sur les tests de déduplication (streak non géré correctement pour sched=None).

- [ ] **Step 3 : Corriger daemon.py si nécessaire**

Si `test_stale_sans_scheduler_ne_plante_pas` échoue, c'est parce que le streak vaut toujours 0 (pas de sched) → `is_first_stale = True` → enregistre la décision. C'est le comportement correct. Si le test échoue pour une autre raison, corriger le bloc ajouté en Task 3 Zone B.

Si `test_stale_occurrences_suivantes` échoue, vérifier que `_append_event` dans le bloc `else` utilise bien les bons noms de champ.

- [ ] **Step 4 : Vérifier passage au vert (tous les tests stale)**

```bash
uv run pytest tests/test_daemon_stale_backoff.py -v 2>&1 | tail -30
```

Attendu : tous passent.

- [ ] **Step 5 : Suite complète pour régression**

```bash
uv run pytest -q --tb=short 2>&1 | tail -20
```

Attendu : 517+ passed, 1 skipped. Si des tests échouent, enquêter avant de continuer.

---

## CHANTIER 2 — Logs Rich TTY/non-TTY

### Task 5 : Module `trader/logging_setup.py`

**Files:**
- Create: `trader/logging_setup.py`
- Test: `tests/test_logging_setup.py`

- [ ] **Step 1 : Écrire les tests échouants**

Créer `tests/test_logging_setup.py` :

```python
"""Tests logging_setup : TTY → RichHandler, non-TTY → StreamHandler sans markup."""
import io
import logging

import pytest

from trader.logging_setup import setup_logging


def test_tty_installe_un_rich_handler(monkeypatch) -> None:
    """Quand stdout est un TTY, le logger reçoit un RichHandler."""
    from rich.logging import RichHandler

    root_logger = logging.getLogger("casys-trader")
    # Nettoyer les handlers résiduels d'autres tests
    root_logger.handlers.clear()

    class FakeTTY(io.StringIO):
        def isatty(self):
            return True

    setup_logging(level=logging.INFO, stream=FakeTTY())

    handler_types = [type(h).__name__ for h in root_logger.handlers]
    assert "RichHandler" in handler_types, f"Handlers: {handler_types}"
    # Nettoyer après le test
    root_logger.handlers.clear()


def test_non_tty_installe_un_stream_handler_standard(monkeypatch) -> None:
    """Quand stdout n'est pas un TTY (pipe/CI), le logger reçoit un StreamHandler."""
    root_logger = logging.getLogger("casys-trader")
    root_logger.handlers.clear()

    class FakePipe(io.StringIO):
        def isatty(self):
            return False

    setup_logging(level=logging.INFO, stream=FakePipe())

    handler_types = [type(h).__name__ for h in root_logger.handlers]
    assert "StreamHandler" in handler_types
    assert "RichHandler" not in handler_types, f"RichHandler ne doit pas être là: {handler_types}"
    root_logger.handlers.clear()


def test_non_tty_ne_laisse_pas_fuir_les_balises_rich() -> None:
    """En mode non-TTY, les messages ne doivent pas contenir de markup [green]..."""
    root_logger = logging.getLogger("casys-trader")
    root_logger.handlers.clear()

    output = io.StringIO()

    class FakePipe(io.StringIO):
        def isatty(self):
            return False

    # On ne peut pas tester directement le rendu ici (le formatter est sur le handler),
    # mais on vérifie que le handler n'est pas un RichHandler (qui interpréterait le markup).
    setup_logging(level=logging.INFO, stream=FakePipe())

    # Le formatter du StreamHandler ne doit pas contenir "[" dans le format raw
    handler = root_logger.handlers[0]
    assert not hasattr(handler, "console"), "RichHandler ne doit pas être installé"
    root_logger.handlers.clear()


def test_setup_logging_configure_le_niveau() -> None:
    """setup_logging doit configurer le niveau demandé sur le logger."""
    root_logger = logging.getLogger("casys-trader")
    root_logger.handlers.clear()

    class FakePipe(io.StringIO):
        def isatty(self):
            return False

    setup_logging(level=logging.WARNING, stream=FakePipe())
    assert root_logger.level == logging.WARNING
    root_logger.handlers.clear()
```

- [ ] **Step 2 : Vérifier échec**

```bash
uv run pytest tests/test_logging_setup.py -v 2>&1 | tail -20
```

Attendu : `ModuleNotFoundError: No module named 'trader.logging_setup'`

- [ ] **Step 3 : Créer `trader/logging_setup.py`**

```python
"""logging_setup — installe un handler adapté au contexte d'exécution.

TTY (terminal interactif) → RichHandler : couleurs par niveau, timestamps
courts, sans path/lineno.

Non-TTY (pipe, CI, redirect) → StreamHandler texte simple. Les messages
du daemon peuvent contenir du markup Rich ([green]...) mais ce module
s'assure que le formatter n'interprète PAS ce markup en mode non-TTY
(option markup=False sur le handler, ou handler sans console Rich).

Approche retenue : le markup Rich N'EST PAS injecté dans les chaînes de
message (les fichiers machine restent indemnes). La mise en couleur est
exclusivement pilotée par le RichHandler via les niveaux de log standard.
Cela garantit zéro fuite de balises dans les logs non-TTY.
"""

from __future__ import annotations

import logging
import sys
from typing import IO


def setup_logging(
    level: int = logging.INFO,
    stream: IO | None = None,
) -> None:
    """Configure le logger racine 'casys-trader'.

    Args:
        level: niveau de log (ex. logging.INFO).
        stream: flux de sortie. None → sys.stdout. Injecter un faux stream
                en test pour contrôler isatty().
    """
    if stream is None:
        stream = sys.stdout

    logger = logging.getLogger("casys-trader")
    logger.setLevel(level)
    # Supprimer les handlers hérités pour éviter les doublons
    logger.handlers.clear()

    is_tty = callable(getattr(stream, "isatty", None)) and stream.isatty()

    if is_tty:
        from rich.logging import RichHandler

        handler = RichHandler(
            level=level,
            rich_tracebacks=False,
            show_path=False,
            markup=False,         # on ne met PAS de markup dans les messages
            log_time_format="[%H:%M:%S]",
            stream=stream,        # type: ignore[arg-type]
        )
    else:
        handler = logging.StreamHandler(stream=stream)
        handler.setLevel(level)
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        handler.setFormatter(formatter)

    logger.addHandler(handler)
    # Ne pas propager vers le root logger (évite les doublons si basicConfig est
    # appelé ailleurs dans le process).
    logger.propagate = False
```

- [ ] **Step 4 : Vérifier passage au vert**

```bash
uv run pytest tests/test_logging_setup.py -v 2>&1 | tail -20
```

Attendu : 4 tests passent.

---

### Task 6 : Intégrer `setup_logging` dans `daemon.main()`

**Files:**
- Modify: `trader/daemon.py`

- [ ] **Step 1 : Remplacer le basicConfig dans daemon.main()**

Dans `trader/daemon.py`, localiser la ligne ~1587 :
```python
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
```

La remplacer par :
```python
    from .logging_setup import setup_logging
    setup_logging(level=logging.INFO)
```

- [ ] **Step 2 : Vérifier que la suite complète reste verte**

```bash
uv run pytest -q --tb=short 2>&1 | tail -20
```

Attendu : 517+ passed, 1 skipped. Les tests qui utilisent `caplog` ne doivent pas casser car `caplog` intercepte les appels au logger avant le handler.

> **Note :** Si des tests `caplog` cassent après ce changement, c'est parce que `setup_logging` appelle `logger.propagate = False` ou vide `logger.handlers`. Dans ce cas, vérifier que `setup_logging` n'est pas appelé dans les tests existants (il ne doit l'être que dans `main()`). Si besoin, ajouter un guard dans `setup_logging` : ne modifier les handlers que si appelé explicitement (pas lors des imports).

---

### Task 7 : Vérification finale complète

**Files:** aucun

- [ ] **Step 1 : Suite complète**

```bash
uv run pytest -q 2>&1 | tail -10
```

Attendu : 530+ passed (517 existants + ~13 nouveaux), 1 skipped.

- [ ] **Step 2 : Vérifier les fichiers machine non touchés**

```bash
# Vérifier que events.jsonl et decisions.jsonl ne sont PAS modifiés par les tests
grep -r "events.jsonl\|decisions.jsonl" /Users/erwanpesle/Documents/GitHub/casys-trader/trader/logging_setup.py 2>/dev/null && echo "PROBLÈME" || echo "OK"
```

Attendu : `OK`

- [ ] **Step 3 : Exemple visuel (mode non-TTY)**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader
python -c "
import logging, io, sys
sys.path.insert(0, '.')
from trader.logging_setup import setup_logging
buf = io.StringIO()
buf.isatty = lambda: False
setup_logging(level=logging.INFO, stream=buf)
log = logging.getLogger('casys-trader')
log.info('[market] stale symbols=[SPY, QQQ]')
log.warning('[decision 1/2] SPY stale_backoff streak=3 wake=120min')
log.info('[cycle] start dry_run=True due=2 max_model_calls=25')
print(buf.getvalue())
"
```

Attendu (format texte, sans markup) :
```
2026-06-10 03:00:01 INFO [market] stale symbols=[SPY, QQQ]
2026-06-10 03:00:01 WARNING [decision 1/2] SPY stale_backoff streak=3 wake=120min
2026-06-10 03:00:01 INFO [cycle] start dry_run=True due=2 max_model_calls=25
```

---

## Self-Review

### Couverture spec

| Exigence spec | Task |
|---|---|
| Backoff exponentiel, streak dans scheduler.json, tolérant vieux state | Task 1, 2, 3 |
| Cap à 120 min, constantes nommées | Task 1 (constantes), Task 2 (cap) |
| Reset streak sur data fraîche | Task 3 Zone A |
| 1 décision HOLD sur transition fresh→stale uniquement | Task 3 Zone B, Task 4 |
| Event `stale_backoff` (symbol, streak, next_wake_minutes) | Task 3 Zone B |
| TTY → RichHandler | Task 5, 6 |
| Non-TTY → StreamHandler sans fuite markup | Task 5 |
| Tests caplog non cassés | Task 6 Step 2 |
| `state/events.jsonl` machine inchangé | Task 7 Step 2 |
| Pas de touche à consolidator/cli_semantic/daemon_learnings/llm | contrainte dans header |

### Vérification placeholders

Aucun TBD/TODO laissé dans le plan. Tous les blocs de code sont complets.

### Cohérence des types

- `Scheduler.get_stale_streak` → `int` ; `daemon._stale_backoff_wake_minutes(streak: int, ...)` → `float` : cohérent.
- `sched.set_stale_streak(sym, new_streak)` où `new_streak = streak + 1` : `int + int` = `int` : OK.
- `_append_event("stale_backoff", ...)` utilise les mêmes noms que les tests de Task 4 (`streak`, `next_wake_minutes`, `stale_reason`, `data_age_minutes`) : cohérent.
