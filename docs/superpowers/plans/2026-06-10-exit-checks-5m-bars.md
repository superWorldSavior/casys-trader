# Exit Checks 5m Bars — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Affiner les checks de sortie (stops/TP/trailing) avec des barres 5m pour les symboles ayant un trade plan ouvert, en gardant le fallback 15m en cas d'échec.

**Architecture:** Dans `run_cycle`, juste avant `_apply_planned_exits`, on identifie les symboles avec plan ouvert (`plan_store.open_plans()`), on fetche des barres 5m via le même `data_source`, et on construit un `bars_by_symbol` enrichi qui remplace les barres 15m pour ces seuls symboles. Le fallback est immédiat : toute exception ou liste vide conserve les barres 15m. La granularité effective est tracée dans le rapport (`planned_exits[*].bars_interval`).

**Tech Stack:** Python 3.12, pytest, uv — aucune dépendance nouvelle.

---

## Cartographie des fichiers

| Fichier | Rôle |
|---|---|
| `trader/daemon.py` | **Modifier** : `_apply_planned_exits` (ajouter `bars_interval` dans chaque entrée du rapport) + `run_cycle` (fetch 5m + construction du dict enrichi) |
| `tests/test_daemon_exit_engine.py` | **Modifier** : ajouter la classe `TestExitChecks5mBars` avec les 4 tests specs |

> Fichiers **intouchables** : `trader/exit_engine.py`, `trader/consolidator.py`, `tests/test_consolidator.py`, `tests/test_cli_semantic.py`, `tests/test_daemon_learnings.py`, `tests/test_llm.py`, `README.md`, `docs/`, `trader/cockpit*.py`.

---

## Task 1 : Tests rouges — fetch 5m uniquement pour symboles avec plan

**Files:**
- Modify: `tests/test_daemon_exit_engine.py`

- [ ] **Step 1 : Écrire le test rouge — fetch 5m sur symbole avec plan, pas sur les autres**

Ajouter à la fin de `tests/test_daemon_exit_engine.py` :

```python
class TestExitChecks5mBars:
    """Checks de sortie affinés sur barres 5m pour les symboles avec plan ouvert."""

    def _setup_state(self, tmp_path, state_dir, *, opened_at: str, symbol: str = "SPY") -> None:
        from trader.tools.execution import Order, SimBroker
        from trader.trade_plan import TradePlanStore, create_trade_plan
        broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
        broker.submit(Order(symbol, "BUY", 10.0), 100.0, opened_at, dry_run=False)
        TradePlanStore(state_dir / "trade_plans.json").upsert(
            create_trade_plan(
                symbol=symbol,
                side="LONG",
                quantity=10.0,
                entry_price=100.0,
                opened_at=opened_at,
                raw_exit_plan={"hard_stop": 95.0},
            )
        )

    def test_5m_fetch_uniquement_sur_symbole_avec_plan(
        self, monkeypatch, tmp_path, patch_batch, make_data_source
    ) -> None:
        """get_bars doit être appelé avec interval='5m' pour le symbole avec plan,
        et PAS avec interval='5m' pour un symbole sans plan."""
        _write_runtime_config(tmp_path, symbols=["SPY", "QQQ"])
        state_dir = tmp_path / "state"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        opened_at = "2026-06-10T11:00:00+00:00"
        self._setup_state(tmp_path, state_dir, opened_at=opened_at, symbol="SPY")
        # QQQ n'a pas de plan → pas de fetch 5m attendu pour QQQ

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

        calls: list[tuple[str, str, str]] = []

        def get_bars_spy(symbol, lookback, interval):
            calls.append((symbol, lookback, interval))
            return [Bar(ts=now.isoformat(), open=98.0, high=99.0, low=97.5, close=98.0, volume=500.0)]

        data_source = make_data_source(get_bars_spy)
        patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

        daemon.run_cycle(
            dry_run=True,
            now=now,
            symbols_filter=["SPY", "QQQ"],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=data_source,
        )

        five_m_calls = [(sym, iv) for sym, _lk, iv in calls if iv == "5m"]
        assert ("SPY", "5m") in five_m_calls, "SPY (avec plan) doit être fetché en 5m"
        assert ("QQQ", "5m") not in five_m_calls, "QQQ (sans plan) ne doit PAS être fetché en 5m"
```

- [ ] **Step 2 : Vérifier que le test échoue**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader
uv run pytest tests/test_daemon_exit_engine.py::TestExitChecks5mBars::test_5m_fetch_uniquement_sur_symbole_avec_plan -v
```

Résultat attendu : `FAILED` — la feature n'existe pas encore.

---

## Task 2 : Test rouge — spike 5m déclenche le stop

**Files:**
- Modify: `tests/test_daemon_exit_engine.py` (ajouter méthode dans `TestExitChecks5mBars`)

- [ ] **Step 1 : Écrire le test rouge — spike visible en 5m mais pas dans 15m**

Ajouter à `TestExitChecks5mBars` :

```python
    def test_spike_5m_declenche_le_stop(
        self, monkeypatch, tmp_path, patch_batch, make_data_source
    ) -> None:
        """Un spike visible sur la barre 5m (bar_high > stop) mais invisible en 15m
        doit déclencher le stop grâce aux barres 5m."""
        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"
        # Plan LONG SPY, stop à 95.0
        # Barre 15m : high=96, pas de stop
        # Barre 5m  : high=94.5 (< 95 = stop côté LONG : bar_low)
        # En fait pour LONG, le stop se déclenche quand bar_low <= stop.
        # Barre 15m low=96 → pas de stop
        # Barre 5m low=94.5 ≤ 95 → stop déclenché
        opened_at = "2026-06-10T11:00:00+00:00"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        self._setup_state(tmp_path, state_dir, opened_at=opened_at)

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

        bar_15m = Bar(
            ts="2026-06-10T11:45:00+00:00",
            open=97.0, high=98.0, low=96.0, close=97.0, volume=1000.0,
        )
        bar_5m = Bar(
            ts="2026-06-10T11:55:00+00:00",
            open=96.0, high=96.5, low=94.5, close=95.5, volume=300.0,
        )

        def get_bars(symbol, lookback, interval):
            if interval == "5m":
                return [bar_5m]
            return [bar_15m]

        data_source = make_data_source(get_bars)
        patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

        report = daemon.run_cycle(
            dry_run=False,
            now=now,
            symbols_filter=[],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=data_source,
        )

        assert len(report["planned_exits"]) == 1, "Le stop doit être déclenché via la barre 5m"
        assert report["planned_exits"][0]["reason"] == "hard_stop"
```

- [ ] **Step 2 : Vérifier que le test échoue**

```bash
uv run pytest tests/test_daemon_exit_engine.py::TestExitChecks5mBars::test_spike_5m_declenche_le_stop -v
```

Résultat attendu : `FAILED` (le stop n'est pas déclenché car on utilise encore les barres 15m dont low=96 > stop=95).

---

## Task 3 : Test rouge — fallback 15m si le fetch 5m échoue

**Files:**
- Modify: `tests/test_daemon_exit_engine.py` (ajouter méthode dans `TestExitChecks5mBars`)

- [ ] **Step 1 : Écrire le test rouge — exception fetch 5m → fallback 15m, sortie non bloquée**

Ajouter à `TestExitChecks5mBars` :

```python
    def test_fallback_15m_si_fetch_5m_echoue(
        self, monkeypatch, tmp_path, patch_batch, make_data_source
    ) -> None:
        """Si le fetch 5m lève une MarketError, le daemon doit tomber en fallback
        sur les barres 15m et ne pas bloquer l'évaluation des exits."""
        from trader.tools.market import MarketError
        _write_runtime_config(tmp_path)
        state_dir = tmp_path / "state"
        opened_at = "2026-06-10T11:00:00+00:00"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        self._setup_state(tmp_path, state_dir, opened_at=opened_at)

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

        # 15m : low=94.0 → stop déclenché (stop=95 pour LONG)
        bar_15m = Bar(
            ts="2026-06-10T11:45:00+00:00",
            open=97.0, high=98.0, low=94.0, close=96.0, volume=1000.0,
        )

        def get_bars(symbol, lookback, interval):
            if interval == "5m":
                raise MarketError("fetch_failed", "5m indispo simulé")
            return [bar_15m]

        data_source = make_data_source(get_bars)
        patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

        report = daemon.run_cycle(
            dry_run=False,
            now=now,
            symbols_filter=[],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=data_source,
        )

        # Le fallback doit s'activer : bar_15m low=94.0 ≤ stop=95 → déclenché
        assert len(report["planned_exits"]) == 1, "Fallback 15m doit fonctionner"
        assert report["planned_exits"][0]["reason"] == "hard_stop"
```

- [ ] **Step 2 : Vérifier que le test échoue**

```bash
uv run pytest tests/test_daemon_exit_engine.py::TestExitChecks5mBars::test_fallback_15m_si_fetch_5m_echoue -v
```

Résultat attendu : `FAILED` (le fetch 5m lève une exception non rattrapée, ou le stop ne se déclenche pas).

---

## Task 4 : Test rouge — `bars_interval` dans le rapport

**Files:**
- Modify: `tests/test_daemon_exit_engine.py` (ajouter méthode dans `TestExitChecks5mBars`)

- [ ] **Step 1 : Écrire le test rouge — `bars_interval` dans chaque entrée `planned_exits`**

Ajouter à `TestExitChecks5mBars` :

```python
    def test_bars_interval_dans_rapport_selon_chemin(
        self, monkeypatch, tmp_path, patch_batch, make_data_source
    ) -> None:
        """planned_exits[*].bars_interval = '5m' quand les barres 5m ont servi,
        '15m' quand le fallback a été pris."""
        from trader.tools.market import MarketError
        _write_runtime_config(tmp_path, symbols=["SPY", "QQQ"])
        state_dir = tmp_path / "state"
        opened_at = "2026-06-10T11:00:00+00:00"
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

        # SPY : plan LONG, stop 95, barre 5m low=94.5 → déclenché, interval='5m'
        from trader.tools.execution import Order, SimBroker
        from trader.trade_plan import TradePlanStore, create_trade_plan
        broker = SimBroker(state_dir / "broker.json", starting_cash=100_000)
        broker.submit(Order("SPY", "BUY", 10.0), 100.0, opened_at, dry_run=False)
        broker.submit(Order("QQQ", "BUY", 5.0), 200.0, opened_at, dry_run=False)
        store = TradePlanStore(state_dir / "trade_plans.json")
        store.upsert(create_trade_plan(
            symbol="SPY", side="LONG", quantity=10.0, entry_price=100.0,
            opened_at=opened_at, raw_exit_plan={"hard_stop": 95.0},
        ))
        # QQQ : plan LONG, stop 180, fetch 5m échoue → fallback 15m, barre 15m low=178 → déclenché
        store.upsert(create_trade_plan(
            symbol="QQQ", side="LONG", quantity=5.0, entry_price=200.0,
            opened_at=opened_at, raw_exit_plan={"hard_stop": 180.0},
        ))

        monkeypatch.setattr(daemon, "ROOT", tmp_path)
        monkeypatch.setattr(daemon, "STATE_DIR", state_dir)

        def get_bars(symbol, lookback, interval):
            if interval == "5m":
                if symbol == "SPY":
                    return [Bar(
                        ts="2026-06-10T11:55:00+00:00",
                        open=96.0, high=96.5, low=94.5, close=95.5, volume=300.0,
                    )]
                # QQQ : échec 5m
                raise MarketError("fetch_failed", "QQQ 5m indispo")
            # 15m
            if symbol == "SPY":
                return [Bar(
                    ts="2026-06-10T11:45:00+00:00",
                    open=97.0, high=98.0, low=96.0, close=97.0, volume=1000.0,
                )]
            if symbol == "QQQ":
                return [Bar(
                    ts="2026-06-10T11:45:00+00:00",
                    open=185.0, high=186.0, low=178.0, close=182.0, volume=800.0,
                )]
            return []

        data_source = make_data_source(get_bars)
        patch_batch(lambda **kwargs: Decision.hold(kwargs["symbol"], "hold"))

        report = daemon.run_cycle(
            dry_run=False,
            now=now,
            symbols_filter=[],
            sched=Scheduler(state_dir / "scheduler.json"),
            data_source=data_source,
        )

        exits_by_symbol = {e["symbol"]: e for e in report["planned_exits"]}
        assert exits_by_symbol["SPY"]["bars_interval"] == "5m", "SPY : 5m utilisé"
        assert exits_by_symbol["QQQ"]["bars_interval"] == "15m", "QQQ : fallback 15m"
```

- [ ] **Step 2 : Vérifier que les tests échouent**

```bash
uv run pytest tests/test_daemon_exit_engine.py::TestExitChecks5mBars -v
```

Résultat attendu : 4 `FAILED`.

---

## Task 5 : Implémentation — fetch 5m + construction du dict enrichi dans `run_cycle`

**Files:**
- Modify: `trader/daemon.py`

**Contexte :** Dans `run_cycle` (ligne ~989), l'appel `_apply_planned_exits` reçoit `bars_by_symbol=tradable_bars_by_symbol`. On va construire un `exit_bars_by_symbol` qui part de `tradable_bars_by_symbol` et remplace par des barres 5m pour les symboles avec plan ouvert.

La constante d'intervalle 5m sera déclarée en tête de fichier.

- [ ] **Step 1 : Ajouter la constante `EXIT_CHECK_INTERVAL` et la fonction `_fetch_5m_exit_bars`**

Dans `trader/daemon.py`, ajouter après les constantes existantes (ligne ~78, après `DEFAULT_MAX_MARKET_DATA_AGE_MINUTES`):

```python
# Intervalle fin pour les checks de sortie (stop/TP/trailing).
# Fetché uniquement pour les symboles ayant un plan ouvert.
EXIT_CHECK_INTERVAL = "5m"
EXIT_CHECK_LOOKBACK = "1d"
```

Puis, juste avant la fonction `run_cycle` (autour de la ligne 804), ajouter :

```python
def _fetch_5m_bars_for_open_plans(
    *,
    plan_store: TradePlanStore,
    data_source: object,
    tradable_bars_by_symbol: dict[str, list],
    tradable_prices: dict[str, float],
) -> tuple[dict[str, list], dict[str, str]]:
    """Fetche des barres 5m pour les symboles ayant un plan ouvert.

    Retourne :
      - exit_bars_by_symbol : tradable_bars_by_symbol enrichi avec les barres 5m
        pour chaque symbole dont le fetch a réussi ; les autres conservent les 15m.
      - intervals_by_symbol : intervalle réellement utilisé par symbole ('5m' ou '15m').

    Contraintes :
      - Aucun fetch pour les symboles SANS plan ouvert (coût = 0).
      - Fallback immédiat (jamais d'exception levée) : exception ou liste vide → 15m conservé.
      - Les barres 15m de `tradable_bars_by_symbol` sont copiées (pas mutées).
    """
    open_symbols = {plan.symbol for plan in plan_store.open_plans() if plan.symbol in tradable_prices}
    exit_bars: dict[str, list] = dict(tradable_bars_by_symbol)
    intervals: dict[str, str] = {sym: DEFAULT_RUNTIME_INTERVAL for sym in tradable_bars_by_symbol}

    for symbol in open_symbols:
        try:
            bars_5m = data_source.get_bars(symbol, lookback=EXIT_CHECK_LOOKBACK, interval=EXIT_CHECK_INTERVAL)
        except Exception:  # noqa: BLE001 — fallback inconditionnelle, ne jamais bloquer une sortie
            log.warning("5m bars fetch failed for %s — falling back to %s", symbol, DEFAULT_RUNTIME_INTERVAL)
            continue
        if not bars_5m:
            log.warning("5m bars empty for %s — falling back to %s", symbol, DEFAULT_RUNTIME_INTERVAL)
            continue
        exit_bars[symbol] = bars_5m
        intervals[symbol] = EXIT_CHECK_INTERVAL

    return exit_bars, intervals
```

- [ ] **Step 2 : Modifier `_apply_planned_exits` pour propager `bars_interval` dans chaque entrée**

Dans `_apply_planned_exits`, la signature accepte déjà `bars_by_symbol`. Il faut ajouter un paramètre `bars_intervals_by_symbol` et l'injecter dans chaque entrée du rapport.

Trouver la signature actuelle (ligne ~430) :

```python
def _apply_planned_exits(
    *,
    broker: SimBroker,
    plan_store: TradePlanStore,
    prices: dict[str, float],
    bars_by_symbol: dict[str, list] | None = None,
    valuation_prices: dict[str, float] | None = None,
    now: datetime,
    dry_run: bool,
    starting_equity: float,
) -> list[dict]:
```

Remplacer par :

```python
def _apply_planned_exits(
    *,
    broker: SimBroker,
    plan_store: TradePlanStore,
    prices: dict[str, float],
    bars_by_symbol: dict[str, list] | None = None,
    bars_intervals_by_symbol: dict[str, str] | None = None,
    valuation_prices: dict[str, float] | None = None,
    now: datetime,
    dry_run: bool,
    starting_equity: float,
) -> list[dict]:
```

Dans le corps de `_apply_planned_exits`, dans les deux blocs `entries.append(...)` (le chemin bloqué et le chemin normal), ajouter la clé `bars_interval` :

Pour le chemin bloqué (autour de ligne 489), dans le dict passé à `entries.append` :
```python
"bars_interval": (bars_intervals_by_symbol or {}).get(plan.symbol, DEFAULT_RUNTIME_INTERVAL),
```

Pour le chemin normal (autour de ligne 547), de même :
```python
"bars_interval": (bars_intervals_by_symbol or {}).get(plan.symbol, DEFAULT_RUNTIME_INTERVAL),
```

- [ ] **Step 3 : Modifier `run_cycle` pour appeler `_fetch_5m_exit_bars` et passer le résultat à `_apply_planned_exits`**

Dans `run_cycle`, trouver le bloc (ligne ~989) :

```python
    planned_exits = _apply_planned_exits(
        broker=broker,
        plan_store=plan_store,
        prices=tradable_prices,
        bars_by_symbol=tradable_bars_by_symbol,
        valuation_prices=prices,
        now=now,
        dry_run=dry_run,
        starting_equity=starting_equity,
    )
```

Le remplacer par :

```python
    exit_bars_by_symbol, exit_intervals_by_symbol = _fetch_5m_bars_for_open_plans(
        plan_store=plan_store,
        data_source=data_source,
        tradable_bars_by_symbol=tradable_bars_by_symbol,
        tradable_prices=tradable_prices,
    )
    planned_exits = _apply_planned_exits(
        broker=broker,
        plan_store=plan_store,
        prices=tradable_prices,
        bars_by_symbol=exit_bars_by_symbol,
        bars_intervals_by_symbol=exit_intervals_by_symbol,
        valuation_prices=prices,
        now=now,
        dry_run=dry_run,
        starting_equity=starting_equity,
    )
```

- [ ] **Step 4 : Vérifier que les 4 tests passent**

```bash
uv run pytest tests/test_daemon_exit_engine.py::TestExitChecks5mBars -v
```

Résultat attendu : 4 `PASSED`.

---

## Task 6 : Vérification suite complète

**Files:** aucun

- [ ] **Step 1 : Lancer la suite complète**

```bash
cd /Users/erwanpesle/Documents/GitHub/casys-trader
uv run pytest -q
```

Résultat attendu : `580+ passed, 1 skipped` (576 + 4 nouveaux), 0 failed.

- [ ] **Step 2 : Si un test existant casse, diagnostiquer**

Si un test existant casse sur `_apply_planned_exits`, c'est probablement un test qui appelle `_apply_planned_exits` directement — vérifier avec :

```bash
grep -n "_apply_planned_exits\|bars_intervals" tests/test_daemon_exit_engine.py
```

Le paramètre `bars_intervals_by_symbol` a une valeur par défaut (`None`) → la signature est rétro-compatible, aucun test ne devrait casser.

---

## Self-Review

**Spec coverage :**

1. ✅ Fetch 5m uniquement pour symboles avec plan ouvert → Task 1 (test) + Task 5 Step 3 (impl).
2. ✅ Garde temporelle existante appliquée aux barres 5m → déjà dans `_apply_planned_exits` via `_bar_ts_after_plan_open` ; les barres 5m sont passées dans le même paramètre `bars_by_symbol`, donc la garde s'applique automatiquement.
3. ✅ Aucun fetch 5m pour symboles sans plan → vérifié par test Task 1 (assertion sur `QQQ`).
4. ✅ `bars_interval` dans le rapport → Task 4 (test) + Task 5 Steps 2 et 3 (impl).
5. ✅ Pas de changement à `exit_engine.py` → l'impl ne touche que `daemon.py`.
6. ✅ Fallback robuste → Task 3 (test) + `except Exception` dans `_fetch_5m_bars_for_open_plans`.
7. ✅ Prix courant reste celui du runtime → `prices` n'est pas modifié ; seuls `bars_by_symbol` change.

**Placeholder scan :** aucun TBD / TODO trouvé.

**Type consistency :**
- `_fetch_5m_bars_for_open_plans` retourne `tuple[dict[str, list], dict[str, str]]` → utilisé tel quel dans `run_cycle`.
- `bars_intervals_by_symbol: dict[str, str] | None = None` dans `_apply_planned_exits` → accédé via `.get(plan.symbol, DEFAULT_RUNTIME_INTERVAL)` → safe même si `None`.
- `EXIT_CHECK_INTERVAL = "5m"` et `EXIT_CHECK_LOOKBACK = "1d"` déclarés en tête de fichier et utilisés dans `_fetch_5m_bars_for_open_plans`.
