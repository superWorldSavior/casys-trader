# FX Conversion (base USD) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convertir toute la comptabilité et le sizing du système en devise de base USD, en convertissant chaque montant natif (prix, cash, P&L, commissions) via une couche FX unique.

**Architecture:** Un module `trader/fx.py` pur (dérivation devise + conversion) alimenté par un provider de taux yfinance caché par cycle avec fallback statique. Le taux est persisté sur chaque fill pour un P&L reproductible. Sizing, cash, P&L et affichage convertissent au passage des frontières natif↔USD. L'agent ne propose plus la quantité : le code la calcule depuis `hard_stop` + `risk_pct` + equity USD.

**Tech Stack:** Python 3.13, pytest, yfinance, PyYAML, dataclasses.

## Global Constraints

- Devise de base = **USD**. Tout `cash`/`equity`/`P&L`/borne de risque est en USD.
- Les **niveaux de prix par titre restent natifs** (stop/TP/entry affichés dans la devise de cotation).
- `fx_rate` = **USD par unité de devise native** (USD→1.0, TWD→~0.031, EUR→~1.08).
- Déterminisme (AX #6) : l'attribution lit le `fx_rate` **du fill**, jamais le taux courant.
- Safe defaults (AX #2) : le script de migration est `dry_run` par défaut, écrit seulement avec `--commit`.
- Fail-safe : toute erreur de fetch FX → fallback statique `config/fx.yaml`, loggé comme dégradation, jamais de crash.
- TDD strict, commits fréquents, un comportement = un test.
- Spec de référence : `docs/superpowers/specs/2026-06-24-fx-conversion-design.md`.

---

## File Structure

- `trader/fx.py` (créer) — `currency_for`, `to_usd`, constantes devise. Pur, sans I/O.
- `config/fx.yaml` (créer) — paires yfinance par devise + taux fallback statiques.
- `trader/fx_rates.py` (créer) — provider de taux : fetch live (fetcher injectable) + fallback config. I/O isolée ici.
- `trader/tools/execution.py` (modifier) — `Fill.fx_rate`, conversion cash dans `_fill`.
- `trader/risk.py` (modifier) — sizing et bornes en USD via FX.
- `trader/daemon.py` (modifier) — fetch des taux du cycle, sizing code-side, propagation `fx_rate` aux fills.
- `trader/codex_client.py` (modifier) — l'agent ne fournit plus `quantity`.
- `trader/attribution.py` (modifier) — conversion P&L par leg via `fx_rate`.
- `trader/tui.py` / `trader/cockpit.py` (modifier) — affichage USD.
- `scripts/migrate_fx_cash.py` (créer) — recalcul one-shot du cash depuis les fills.
- Tests : `tests/test_fx.py`, `tests/test_fx_rates.py`, et ajouts dans `tests/test_risk.py`, `tests/test_execution.py`, `tests/test_attribution.py`, `tests/test_migrate_fx_cash.py`.

---

## Task 1: Module FX pur (`trader/fx.py`)

**Files:**
- Create: `trader/fx.py`
- Test: `tests/test_fx.py`

**Interfaces:**
- Produces:
  - `currency_for(symbol: str) -> str` — devise de cotation ("USD"/"TWD"/"EUR").
  - `to_usd(amount: float, ccy: str, rate: float) -> float` — `amount * rate` ; `ccy=="USD"` → `amount`. Lève `ValueError` si `rate` non-fini ou ≤ 0 pour une devise ≠ USD.
  - `SUFFIX_CCY: dict[str, str]` — map suffixe → devise.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fx.py
import math
import pytest
from trader import fx


def test_currency_for_taiwan():
    assert fx.currency_for("2379.TW") == "TWD"
    assert fx.currency_for("6488.TWO") == "TWD"


def test_currency_for_europe():
    assert fx.currency_for("ACA.PA") == "EUR"
    assert fx.currency_for("RHM.DE") == "EUR"


def test_currency_for_us_and_default():
    assert fx.currency_for("MSFT") == "USD"
    assert fx.currency_for("EURUSD=X") == "USD"
    assert fx.currency_for("^FCHI") == "USD"


def test_to_usd_identity_for_usd():
    assert fx.to_usd(123.45, "USD", 999.0) == 123.45


def test_to_usd_linear():
    assert fx.to_usd(1000.0, "TWD", 0.031) == pytest.approx(31.0)


def test_to_usd_rejects_bad_rate_for_non_usd():
    for bad in (0.0, -1.0, math.inf, math.nan):
        with pytest.raises(ValueError):
            fx.to_usd(100.0, "TWD", bad)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_fx.py -v`
Expected: FAIL (`ModuleNotFoundError: trader.fx`).

- [ ] **Step 3: Write minimal implementation**

```python
# trader/fx.py
"""fx — devise de cotation et conversion vers la base USD.

Module pur : pas d'I/O, pas de fetch. Reçoit le taux, l'applique.
`rate` = USD par unité de la devise native (USD→1.0).
"""
from __future__ import annotations

import math

BASE_CCY = "USD"

SUFFIX_CCY: dict[str, str] = {
    ".TW": "TWD",
    ".TWO": "TWD",
    ".PA": "EUR",
    ".DE": "EUR",
    ".AS": "EUR",
    ".MI": "EUR",
}


def currency_for(symbol: str) -> str:
    """Devise de cotation dérivée du suffixe. Défaut USD."""
    sym = (symbol or "").strip().upper()
    for suffix, ccy in SUFFIX_CCY.items():
        if sym.endswith(suffix):
            return ccy
    return BASE_CCY


def to_usd(amount: float, ccy: str, rate: float) -> float:
    """Convertit `amount` (en `ccy`) vers USD via `rate` (USD/unité ccy)."""
    if ccy == BASE_CCY:
        return amount
    if not math.isfinite(rate) or rate <= 0.0:
        raise ValueError(f"fx rate invalide pour {ccy}: {rate!r}")
    return amount * rate
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_fx.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add trader/fx.py tests/test_fx.py
git commit -m "feat(fx): module pur devise + conversion USD"
```

---

## Task 2: Provider de taux FX (`config/fx.yaml` + `trader/fx_rates.py`)

**Files:**
- Create: `config/fx.yaml`
- Create: `trader/fx_rates.py`
- Test: `tests/test_fx_rates.py`

**Interfaces:**
- Consumes: `trader.fx.currency_for`, `trader.fx.BASE_CCY`.
- Produces:
  - `load_fx_config(path: str | Path) -> dict` — `{ccy: {"yahoo": str, "invert": bool, "fallback": float}}`.
  - `rates_for_symbols(symbols, *, fetcher, config) -> dict[str, float]` — `{ccy: rate_usd}` pour les devises distinctes des `symbols` (USD inclus = 1.0). `fetcher(yahoo_symbol) -> float | None` injectable (le close de la paire) ; sur `None`/exception → `fallback` du config, et la devise est notée dégradée via le logger fourni.

**Note convention yfinance :** `TWD=X` cote *TWD par USD* → `rate_usd = 1 / close` (`invert: true`). `EURUSD=X` cote *USD par EUR* → `rate_usd = close` (`invert: false`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fx_rates.py
from pathlib import Path
import textwrap
import pytest
from trader import fx_rates


def _write_cfg(tmp_path: Path) -> Path:
    p = tmp_path / "fx.yaml"
    p.write_text(textwrap.dedent("""
        TWD:
          yahoo: "TWD=X"
          invert: true
          fallback: 0.031
        EUR:
          yahoo: "EURUSD=X"
          invert: false
          fallback: 1.08
    """))
    return p


def test_usd_always_one(tmp_path):
    cfg = fx_rates.load_fx_config(_write_cfg(tmp_path))
    rates = fx_rates.rates_for_symbols(["MSFT"], fetcher=lambda s: None, config=cfg)
    assert rates["USD"] == 1.0


def test_inverted_pair(tmp_path):
    cfg = fx_rates.load_fx_config(_write_cfg(tmp_path))
    # TWD=X close = 32 TWD/USD -> rate_usd = 1/32
    rates = fx_rates.rates_for_symbols(["2379.TW"], fetcher=lambda s: 32.0, config=cfg)
    assert rates["TWD"] == pytest.approx(1 / 32.0)


def test_direct_pair(tmp_path):
    cfg = fx_rates.load_fx_config(_write_cfg(tmp_path))
    rates = fx_rates.rates_for_symbols(["ACA.PA"], fetcher=lambda s: 1.08, config=cfg)
    assert rates["EUR"] == pytest.approx(1.08)


def test_fallback_on_fetch_failure(tmp_path):
    cfg = fx_rates.load_fx_config(_write_cfg(tmp_path))
    def boom(_):
        raise RuntimeError("yfinance down")
    rates = fx_rates.rates_for_symbols(["2379.TW"], fetcher=boom, config=cfg)
    assert rates["TWD"] == pytest.approx(0.031)


def test_fallback_on_none(tmp_path):
    cfg = fx_rates.load_fx_config(_write_cfg(tmp_path))
    rates = fx_rates.rates_for_symbols(["2379.TW"], fetcher=lambda s: None, config=cfg)
    assert rates["TWD"] == pytest.approx(0.031)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_fx_rates.py -v`
Expected: FAIL (`ModuleNotFoundError: trader.fx_rates`).

- [ ] **Step 3: Write `config/fx.yaml`**

```yaml
# config/fx.yaml — taux de change vers la base USD.
# rate_usd = USD par unité de la devise. Convention yfinance par paire :
#   invert:true  -> la paire cote <ccy> par USD (TWD=X) -> rate = 1/close
#   invert:false -> la paire cote USD par <ccy> (EURUSD=X) -> rate = close
# fallback : taux statique utilisé si le fetch live échoue (dégradation loggée).
TWD:
  yahoo: "TWD=X"
  invert: true
  fallback: 0.031
EUR:
  yahoo: "EURUSD=X"
  invert: false
  fallback: 1.08
```

- [ ] **Step 4: Write minimal implementation**

```python
# trader/fx_rates.py
"""fx_rates — provider de taux FX vers USD (live yfinance + fallback statique).

I/O isolée ici : le fetch est injecté (testable). Le module fx reste pur.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import yaml

from . import fx

logger = logging.getLogger(__name__)

Fetcher = Callable[[str], float | None]


def load_fx_config(path: str | Path) -> dict:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return {str(k): dict(v) for k, v in data.items()}


def _rate_from_close(close: float | None, *, invert: bool) -> float | None:
    if close is None or close <= 0.0:
        return None
    return (1.0 / close) if invert else close


def rates_for_symbols(symbols, *, fetcher: Fetcher, config: dict) -> dict[str, float]:
    currencies = {fx.currency_for(s) for s in symbols}
    rates: dict[str, float] = {fx.BASE_CCY: 1.0}
    for ccy in currencies:
        if ccy == fx.BASE_CCY:
            continue
        spec = config.get(ccy)
        if spec is None:
            raise ValueError(f"devise non configurée dans fx.yaml: {ccy}")
        rate = None
        try:
            rate = _rate_from_close(fetcher(spec["yahoo"]), invert=bool(spec.get("invert")))
        except Exception as exc:  # noqa: BLE001 — fail-safe : on dégrade au fallback.
            logger.warning("fx fetch %s échoué (%s), fallback statique", ccy, exc)
        if rate is None:
            rate = float(spec["fallback"])
            logger.warning("fx %s : taux fallback statique %s", ccy, rate)
        rates[ccy] = rate
    return rates
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_fx_rates.py -v`
Expected: PASS (5 tests).

- [ ] **Step 6: Commit**

```bash
git add config/fx.yaml trader/fx_rates.py tests/test_fx_rates.py
git commit -m "feat(fx): provider de taux live yfinance + fallback statique"
```

---

## Task 3: `Fill.fx_rate` + conversion cash du broker

**Files:**
- Modify: `trader/tools/execution.py` (`Fill` ~30-39, `SimBroker._fill` ~225-253, `submit`)
- Test: `tests/test_execution.py`

**Interfaces:**
- Consumes: `trader.fx.currency_for`, `trader.fx.to_usd`.
- Produces:
  - `Fill` gagne `fx_rate: float = 1.0`.
  - `SimBroker.submit(order, price, ts, dry_run=True, fx_rate=1.0)` — `fx_rate` propagé au fill ; cash déduit en USD.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_execution.py (ajout)
from trader.tools.execution import SimBroker, Order


def test_cash_deducted_in_usd_for_twd_symbol(tmp_path):
    state = tmp_path / "broker.json"
    broker = SimBroker(state_path=state, starting_cash=100_000.0)
    # 10 actions @ 870 TWD, rate 0.031 -> 8700 * 0.031 = 269.7 USD
    broker.submit(Order("2379.TW", "BUY", 10.0), price=870.0, ts="t", dry_run=False, fx_rate=0.031)
    assert broker.cash() == pytest.approx(100_000.0 - 8700.0 * 0.031)
    fill = broker._state.fills[-1]
    assert fill["fx_rate"] == 0.031


def test_cash_unchanged_for_usd_symbol(tmp_path):
    state = tmp_path / "broker.json"
    broker = SimBroker(state_path=state, starting_cash=100_000.0)
    broker.submit(Order("MSFT", "BUY", 2.0), price=100.0, ts="t", dry_run=False, fx_rate=1.0)
    assert broker.cash() == pytest.approx(100_000.0 - 200.0)
```

(Vérifier l'en-tête du fichier de test : importer `pytest` si absent.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_execution.py -k usd -v`
Expected: FAIL (`submit() got an unexpected keyword argument 'fx_rate'`).

- [ ] **Step 3: Implement**

Dans `trader/tools/execution.py` :

```python
# en-tête
from .. import fx

# Fill : ajouter le champ
@dataclass(frozen=True)
class Fill:
    symbol: str
    side: Side
    quantity: float
    price: float
    ts: str
    commission: float = 0.0
    commission_currency: str = "USD"
    commission_model: str = "none"
    fx_rate: float = 1.0
```

Signature `submit` / `_fill` : ajouter `fx_rate: float = 1.0`, le passer au `Fill`, et convertir la déduction cash. Remplacer la ligne `self._state.cash -= signed * price + commission.amount` par :

```python
ccy = fx.currency_for(order.symbol)
cash_delta = fx.to_usd(signed * price, ccy, fx_rate)
fee_usd = fx.to_usd(commission.amount, commission.currency, fx_rate)
self._state.cash -= cash_delta + fee_usd
```

Construire le `Fill` avec `fx_rate=fx_rate`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_execution.py -v`
Expected: PASS (incl. tests existants — `fx_rate` a un défaut 1.0, rétro-compatible).

- [ ] **Step 5: Commit**

```bash
git add trader/tools/execution.py tests/test_execution.py
git commit -m "feat(fx): cash broker déduit en USD + fx_rate sur le fill"
```

---

## Task 4: Sizing et bornes de risque en USD (`trader/risk.py`)

**Files:**
- Modify: `trader/risk.py` (`max_order_quantity_at_price` ~81-87, `max_quantity_at_risk` ~89-124)
- Test: `tests/test_risk.py`

**Interfaces:**
- Consumes: `trader.fx.to_usd` (ou un `rate` passé en paramètre).
- Produces:
  - `max_order_quantity_at_price(price: float, *, fx_rate: float = 1.0) -> float` — plafond USD comparé à la valeur USD.
  - `max_quantity_at_risk(equity, entry_price, stop_price, *, fx_rate: float = 1.0) -> float` — `equity` en USD, distance convertie en USD.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_risk.py (ajout)
from trader.risk import RiskGate, RiskLimits


def _gate(max_order_value=10_000.0, pct=0.01):
    return RiskGate(RiskLimits(max_order_value=max_order_value, max_risk_per_trade_pct=pct))


def test_order_qty_respects_usd_cap_for_twd():
    gate = _gate(max_order_value=10_000.0)
    qty = gate.max_order_quantity_at_price(870.0, fx_rate=0.031)
    # valeur USD de l'ordre <= 10000
    assert qty * 870.0 * 0.031 <= 10_000.0 + 1e-6
    # et nettement plus que l'ancien 10000/870 ≈ 11
    assert qty > 300.0


def test_qty_at_risk_uses_usd_distance():
    gate = _gate(pct=0.01)
    # equity 100k USD, distance native 40 TWD, rate 0.031 -> risque USD ciblé 1000
    qty = gate.max_quantity_at_risk(100_000.0, 870.0, 830.0, fx_rate=0.031)
    real_risk_usd = qty * abs(870.0 - 830.0) * 0.031
    assert real_risk_usd == pytest.approx(1_000.0, rel=1e-6)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_risk.py -k "usd or risk" -v`
Expected: FAIL (`unexpected keyword argument 'fx_rate'`).

- [ ] **Step 3: Implement**

`max_order_quantity_at_price` : convertir le plafond dans la devise native (équivalent : comparer la valeur convertie). Le plus simple — borner en valeur USD :

```python
def max_order_quantity_at_price(self, price: float, *, fx_rate: float = 1.0) -> float:
    if not math.isfinite(price) or price <= 0 or not math.isfinite(fx_rate) or fx_rate <= 0:
        return 0.0
    price_usd = price * fx_rate
    quantity = self.limits.max_order_value / price_usd
    while quantity > 0.0 and quantity * price_usd > self.limits.max_order_value:
        quantity = math.nextafter(quantity, 0.0)
    return quantity
```

`max_quantity_at_risk` : convertir la distance en USD avant division.

```python
    distance = abs(entry_price - stop_price) * fx_rate
    risk_cap = pct * equity
    ...
    quantity = risk_cap / distance
    while quantity > 0.0 and quantity * distance > risk_cap:
        quantity = math.nextafter(quantity, 0.0)
    return quantity
```

(Ajouter la validation `fx_rate` fini > 0 en tête, comme les autres garde-fous.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_risk.py -v`
Expected: PASS (incl. tests existants : `fx_rate=1.0` par défaut → comportement identique pour l'USD).

- [ ] **Step 5: Commit**

```bash
git add trader/risk.py tests/test_risk.py
git commit -m "feat(fx): sizing et bornes de risque en USD via fx_rate"
```

---

## Task 5: Câblage daemon — taux du cycle, sizing code-side, propagation fill

**Files:**
- Modify: `trader/daemon.py` (boucle prix ~1429-1444 ; `_submit_*`/`_execute` qui appellent `broker.submit` ~199-204, ~855 ; sizing entrée ~2061, ~2224-2234)
- Modify: `trader/codex_client.py` (`_DECISION_KEYS` ~39 ; prompt sortie ~107,131,281,406 ; `Decision.quantity` reste accepté mais ignoré)
- Test: `tests/test_daemon_*.py` (suite existante + un test de sizing code-side)

**Interfaces:**
- Consumes: `fx_rates.rates_for_symbols`, `fx_rates.load_fx_config`, `fx.currency_for`, `RiskGate.max_quantity_at_risk`, `RiskGate.max_order_quantity_at_price`.
- Produces: `effective_quantity` calculée par le code à l'entrée ; `fx_rate` passé à chaque `broker.submit`.

- [ ] **Step 1: Write the failing test (sizing code-side)**

```python
# tests/test_daemon_sizing.py (créer)
from trader.risk import RiskGate, RiskLimits
from trader import fx


def test_code_sizes_entry_in_usd():
    """Le code calcule la quantité depuis equity USD + distance stop + fx_rate,
    sans dépendre d'une quantité proposée par l'agent."""
    gate = RiskGate(RiskLimits(max_order_value=10_000.0, max_risk_per_trade_pct=0.01))
    rate = fx.currency_for("2379.TW")  # TWD
    assert rate == "TWD"
    qty_risk = gate.max_quantity_at_risk(100_000.0, 870.0, 830.0, fx_rate=0.031)
    qty_cap = gate.max_order_quantity_at_price(870.0, fx_rate=0.031)
    qty = min(qty_risk, qty_cap)
    assert qty > 0
    # exposition USD bornée par max_order_value
    assert qty * 870.0 * 0.031 <= 10_000.0 + 1e-6
```

(Ce test verrouille la formule de sizing réutilisée dans le daemon ; le câblage exact dans `daemon.py` est couvert par la suite `test_daemon_*` existante qui doit rester verte.)

- [ ] **Step 2: Run test to verify it fails / suite de référence**

Run: `pytest tests/test_daemon_sizing.py -v`
Expected: FAIL au départ si `fx_rate` pas encore câblé (sinon vert une fois Task 4 mergée — dans ce cas, c'est un test de non-régression du contrat).

- [ ] **Step 3: Implement — fetch des taux du cycle**

Dans la boucle qui construit `prices` (`daemon.py:1429-1444`), après avoir collecté les symboles, charger la config FX une fois et fetcher les taux du cycle :

```python
from . import fx, fx_rates
# ... une seule fois par cycle, près de la construction de `prices` :
_fx_cfg = fx_rates.load_fx_config(CONFIG_DIR / "fx.yaml")
def _fx_fetch(yahoo_symbol: str):
    bars = market.get_bars(yahoo_symbol, lookback=2, interval="1d")
    return bars[-1].close if bars else None
fx_rate_by_ccy = fx_rates.rates_for_symbols(prices.keys(), fetcher=_fx_fetch, config=_fx_cfg)
```

Helper local : `def _rate(sym): return fx_rate_by_ccy[fx.currency_for(sym)]`.
Stocker `fx_rate_by_ccy` dans le snapshot/report (clé `fx_rates`) pour l'audit.

- [ ] **Step 4: Implement — sizing code-side à l'entrée**

À l'entrée (`daemon.py` ~2061), remplacer `effective_quantity = abs(decision.quantity)` par un calcul code-side, quand `action ∈ {BUY, SELL}` et qu'un `hard_stop` résolu existe :

```python
rate = _rate(sym)
entry_price = prices[sym]
stop_price = resolved_hard_stop  # déjà résolu par l'exit-plan engine
qty_risk = risk_gate.max_quantity_at_risk(equity_usd, entry_price, stop_price, fx_rate=rate)
qty_cap = risk_gate.max_order_quantity_at_price(entry_price, fx_rate=rate)
effective_quantity = min(qty_risk, qty_cap)
```

Conserver le clamp existant (`max_position_value`, `max_gross_exposure`) en convertissant les valeurs en USD via `rate`. `equity_usd` = `broker.cash()` + Σ positions valorisées en USD (cf. Task 7 helper, ou calcul inline ici).

- [ ] **Step 5: Implement — propager `fx_rate` aux fills**

Chaque appel `broker.submit(order, price, ts, dry_run=...)` (entrée ~199-204, sortie ~855, et `_submit_*`) reçoit `fx_rate=_rate(symbol)`.

- [ ] **Step 6: Implement — l'agent ne propose plus la quantité**

Dans `trader/codex_client.py` : retirer `"quantity"` de `_DECISION_KEYS` (ligne 39) et des gabarits de sortie du prompt (lignes 107, 131, 281, 406). Garder `quantity` optionnel dans `Decision` avec défaut 0.0 (rétro-compat parsing) mais documenter qu'il est ignoré. Mettre à jour le texte du prompt pour expliquer que le sizing est calculé par le code depuis le `hard_stop` et `risk_pct`.

- [ ] **Step 7: Run the full daemon + sizing suites**

Run: `pytest tests/test_daemon_sizing.py tests/test_daemon_confidence_gate.py tests/test_daemon_learnings.py -v`
Expected: PASS. Corriger les tests qui supposaient une quantité agent.

- [ ] **Step 8: Commit**

```bash
git add trader/daemon.py trader/codex_client.py tests/test_daemon_sizing.py
git commit -m "feat(fx): taux du cycle, sizing code-side USD, agent sans quantité"
```

---

## Task 5B: Conscience devise du contexte agent (`trader/agent_context.py`)

**Files:**
- Modify: `trader/agent_context.py` (`build_market_cockpit` ~120-200, construction de la ligne par symbole)
- Modify: `trader/codex_client.py` (texte du prompt : règle devise)
- Test: `tests/test_agent_context.py` (créer si absent, sinon ajout)

**Interfaces:**
- Consumes: `trader.fx.currency_for`, `fx_rate_by_ccy` du cycle (Task 5).
- Produces: chaque ligne symbole du cockpit agent contient `ccy: str` et
  `fx_usd: float`. Aucune valeur de prix/indicateur convertie.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_context.py (ajout)
from trader.agent_context import build_market_cockpit


def test_symbol_row_is_currency_stamped():
    bars = {"2379.TW": _fake_bars(close=829.0), "MSFT": _fake_bars(close=370.0)}
    cockpit = build_market_cockpit(
        bars, symbols=["2379.TW", "MSFT"],
        prices={"2379.TW": 829.0, "MSFT": 370.0},
        fx_rate_by_ccy={"TWD": 0.031, "USD": 1.0},
    )
    rows = {r["s"]: r for r in cockpit["rows"]}  # adapter à la clé réelle
    assert rows["2379.TW"]["ccy"] == "TWD"
    assert rows["2379.TW"]["fx_usd"] == 0.031
    assert rows["2379.TW"]["p"] == 829.0          # natif, NON converti
    assert rows["MSFT"]["ccy"] == "USD"
    assert rows["MSFT"]["fx_usd"] == 1.0
```

(`_fake_bars` : réutiliser le helper existant des tests d'`agent_context`/cockpit ; adapter le nom de la clé de prix `p` et la structure `rows` au contrat réel de `build_market_cockpit`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_agent_context.py -k currency_stamped -v`
Expected: FAIL (`build_market_cockpit() got an unexpected keyword argument 'fx_rate_by_ccy'` ou absence de `ccy`).

- [ ] **Step 3: Implement**

- Signature : ajouter `fx_rate_by_ccy: dict[str, float] | None = None` à
  `build_market_cockpit`.
- Pour chaque symbole, ajouter à la ligne : `"ccy": fx.currency_for(symbol)` et
  `"fx_usd": (fx_rate_by_ccy or {}).get(fx.currency_for(symbol), 1.0)`.
- **Ne convertir aucune valeur** (`p`, indicateurs, swings restent natifs).
- `daemon.py` : passer `fx_rate_by_ccy=fx_rate_by_ccy` (du cycle, Task 5) à
  l'appel `build_market_cockpit`.

- [ ] **Step 4: Add the prompt rule**

Dans `trader/codex_client.py`, ajouter au prompt une règle explicite :

```
"Chaque symbole porte sa devise `ccy` et `fx_usd` (USD par unité). "
"TOUS ses prix, indicateurs, swings et niveaux sont dans `ccy`. Tes `hard_stop` "
"et `take_profits` doivent être dans cette MÊME devise (PAS en USD). "
"Le portefeuille (equity, cash) est en USD : tu ne convertis rien, le code "
"calcule la taille de position depuis ton hard_stop et ton risk_pct."
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_agent_context.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add trader/agent_context.py trader/codex_client.py tests/test_agent_context.py
git commit -m "feat(fx): contexte agent estampillé devise (analyse en natif)"
```

---

## Task 6: Conversion P&L de l'attribution (`trader/attribution.py`)

**Files:**
- Modify: `trader/attribution.py` (`compute_round_trips` ~140-205 lecture des fills ; `_position_pnl` ~389-398)
- Test: `tests/test_attribution.py`

**Interfaces:**
- Consumes: `trader.fx.currency_for`, `trader.fx.to_usd`, `Fill.fx_rate`.
- Produces: round-trips dont `pnl`/`gross_pnl`/`commission` sont en **USD**, en utilisant le `fx_rate` de chaque leg.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_attribution.py (ajout)
def test_round_trip_pnl_converted_to_usd(tmp_path):
    """Un round-trip TWD est rapporté en USD = (pnl natif) converti via fx_rate."""
    state = tmp_path
    fills = [
        {"symbol": "2379.TW", "side": "BUY", "quantity": 10.0, "price": 870.0,
         "ts": "2026-06-23T05:00:00+00:00", "commission": 80.0,
         "commission_currency": "TWD", "fx_rate": 0.031},
        {"symbol": "2379.TW", "side": "SELL", "quantity": 10.0, "price": 829.0,
         "ts": "2026-06-24T01:00:00+00:00", "commission": 80.0,
         "commission_currency": "TWD", "fx_rate": 0.031},
    ]
    # écrire un broker.json minimal avec ces fills
    import json
    (state / "broker.json").write_text(json.dumps(
        {"cash": 100000.0, "positions": {}, "fills": fills}))
    trips = attribution.compute_round_trips(state)
    assert len(trips) == 1
    # P&L natif = (829-870)*10 - 160 = -570 TWD ; en USD = -570*0.031
    assert trips[0]["pnl"] == pytest.approx(-570.0 * 0.031, rel=1e-6)
```

(Adapter au contrat exact de `compute_round_trips` — vérifier la clé d'état attendue.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_attribution.py -k usd -v`
Expected: FAIL (pnl en TWD brut = -570, pas -570*0.031).

- [ ] **Step 3: Implement**

Dans `compute_round_trips`, lire `fx_rate` et `commission_currency` de chaque fill. Convertir au moment du calcul du trip :
- prix d'entrée/sortie : garder natifs pour l'affichage, MAIS calculer le `gross_pnl` puis convertir en USD via le `fx_rate` de la jambe concernée (entrée pour la part entrée, sortie pour la part sortie ; en pratique le mouvement de prix se convertit au taux de sortie, et chaque commission à son propre taux).
- `commission` du trip : `to_usd(entry_comm, entry_ccy, entry_fx) + to_usd(exit_comm, exit_ccy, exit_fx)`.
- `pnl = gross_pnl_usd - commission_usd`.

Approche simple et déterministe : convertir `gross_pnl` natif via le `fx_rate` de la jambe de **sortie** (le P&L est réalisé à la sortie), commissions chacune à leur taux. Documenter ce choix en commentaire.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_attribution.py -v`
Expected: PASS. Les fills legacy sans `fx_rate` → défaut 1.0 (P&L = natif, comportement d'avant pour l'USD).

- [ ] **Step 5: Commit**

```bash
git add trader/attribution.py tests/test_attribution.py
git commit -m "feat(fx): P&L attribution converti en USD via fx_rate du fill"
```

---

## Task 7: Affichage USD (`trader/tui.py`, `trader/cockpit.py`)

**Files:**
- Modify: `trader/cockpit.py` (valorisation holdings/equity ; `unrealized_pnl` ~holdings)
- Modify: `trader/tui.py` (labels « non conv. »/« local » ~727-731,817 ; symbole monétaire)
- Test: `tests/test_tui.py`, `tests/test_cockpit_smoke.py`

**Interfaces:**
- Consumes: `trader.fx.currency_for`, `trader.fx.to_usd`, `fx_rate_by_ccy` du report.
- Produces: holdings dont `unrealized_pnl`/`unrealized_pnl_net`/`round_trip_fee`/notional sont en USD ; equity USD.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cockpit_smoke.py (ajout)
def test_holding_unrealized_pnl_in_usd():
    """Un holding TWD est valorisé en USD (prix natif × qty × fx_rate)."""
    holding = _build_holding(  # helper du cockpit
        symbol="2379.TW", quantity=10.0, avg_price=870.0,
        last_price=829.0, fx_rate=0.031)
    # (829-870)*10 * 0.031 = -12.71 USD
    assert holding["unrealized_pnl"] == pytest.approx(-41.0 * 10 * 0.031, rel=1e-6)
```

(Adapter au point exact où le cockpit calcule `unrealized_pnl` — repérer la fonction qui construit `holdings`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cockpit_smoke.py -k usd -v`
Expected: FAIL (unrealized_pnl en TWD brut).

- [ ] **Step 3: Implement**

- `cockpit.py` : valoriser chaque holding en USD via `fx.to_usd(natif, currency_for(sym), rate)` (rate depuis `report["fx_rates"]`). `equity` = cash USD + Σ holdings USD. `round_trip_fee` converti aussi.
- `tui.py` : retirer les suffixes « non conv. »/« local » (lignes ~730-731, 817), réafficher `$`. Garder les **prix de niveau (stop/TP/entry) en natif** avec la devise affichée à côté.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_tui.py tests/test_cockpit_smoke.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/tui.py trader/cockpit.py tests/test_tui.py tests/test_cockpit_smoke.py
git commit -m "feat(fx): cockpit et TUI en USD, niveaux de prix natifs"
```

---

## Task 8: Migration one-shot du cash (`scripts/migrate_fx_cash.py`)

**Files:**
- Create: `scripts/migrate_fx_cash.py`
- Test: `tests/test_migrate_fx_cash.py`

**Interfaces:**
- Consumes: `trader.fx.currency_for`, `trader.fx.to_usd`.
- Produces: CLI `python -m scripts.migrate_fx_cash --state state/ [--commit]`. `dry_run` par défaut : rapporte l'écart cash avant/après sans muter. `--commit` : backup + écriture, `fx_rate` ajouté à chaque fill legacy.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migrate_fx_cash.py
import json
from pathlib import Path
import pytest
from scripts import migrate_fx_cash


def _broker(tmp_path: Path) -> Path:
    p = tmp_path / "broker.json"
    p.write_text(json.dumps({
        "cash": 100000.0 - 8700.0 - 80.0,  # cash pollué (TWD brut déduit)
        "positions": {},
        "fills": [
            {"symbol": "2379.TW", "side": "BUY", "quantity": 10.0, "price": 870.0,
             "ts": "2026-06-23T05:00:00+00:00", "commission": 80.0,
             "commission_currency": "TWD"},
        ],
    }))
    return p


def test_dry_run_does_not_mutate(tmp_path):
    p = _broker(tmp_path)
    before = p.read_text()
    report = migrate_fx_cash.run(p, rates={"TWD": 0.031}, starting_cash=100000.0, commit=False)
    assert p.read_text() == before
    assert "cash_before" in report and "cash_after" in report


def test_commit_recomputes_cash_usd(tmp_path):
    p = _broker(tmp_path)
    migrate_fx_cash.run(p, rates={"TWD": 0.031}, starting_cash=100000.0, commit=True)
    data = json.loads(p.read_text())
    # cash USD = 100000 - (8700+80)*0.031
    assert data["cash"] == pytest.approx(100000.0 - 8780.0 * 0.031)
    assert data["fills"][0]["fx_rate"] == 0.031
    assert (p.parent / (p.name + ".bak-pre-fx")).exists() or any(
        f.name.startswith("broker.json.bak-pre-fx") for f in p.parent.iterdir())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_migrate_fx_cash.py -v`
Expected: FAIL (`ModuleNotFoundError: scripts.migrate_fx_cash`).

- [ ] **Step 3: Implement**

```python
# scripts/migrate_fx_cash.py
"""Recalcule le cash du broker en USD depuis l'historique des fills.

dry_run par défaut. --commit pour écrire (avec backup).
Pour les fills legacy sans fx_rate, applique le taux fourni (`rates`) et le
persiste sur le fill (déterminisme futur).
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from trader import fx


def run(broker_path: Path, *, rates: dict[str, float], starting_cash: float, commit: bool) -> dict:
    data = json.loads(Path(broker_path).read_text())
    fills = data.get("fills", [])
    cash = starting_cash
    for fill in fills:
        ccy = fill.get("commission_currency") or fx.currency_for(fill["symbol"])
        sym_ccy = fx.currency_for(fill["symbol"])
        rate = float(fill.get("fx_rate") or rates.get(sym_ccy, 1.0))
        fill["fx_rate"] = rate
        signed = fill["quantity"] if fill["side"] == "BUY" else -fill["quantity"]
        cash -= fx.to_usd(signed * fill["price"], sym_ccy, rate)
        cash -= fx.to_usd(float(fill.get("commission") or 0.0),
                          fill.get("commission_currency") or sym_ccy, rate)
    report = {"cash_before": data.get("cash"), "cash_after": cash, "n_fills": len(fills)}
    if commit:
        ts_tag = fills[-1]["ts"][:10] if fills else "init"
        shutil.copyfile(broker_path, broker_path.with_name(f"{broker_path.name}.bak-pre-fx-{ts_tag}"))
        data["cash"] = cash
        data["fills"] = fills
        Path(broker_path).write_text(json.dumps(data, indent=2))
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state", help="dossier d'état (contient broker.json)")
    ap.add_argument("--starting-cash", type=float, default=100_000.0)
    ap.add_argument("--commit", action="store_true", help="écrire (défaut: dry-run)")
    args = ap.parse_args()
    # taux : fallback statique de config/fx.yaml (live optionnel hors scope du script)
    from trader import fx_rates
    cfg = fx_rates.load_fx_config(Path("config/fx.yaml"))
    rates = {ccy: float(spec["fallback"]) for ccy, spec in cfg.items()}
    report = run(Path(args.state) / "broker.json", rates=rates,
                 starting_cash=args.starting_cash, commit=args.commit)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_migrate_fx_cash.py -v`
Expected: PASS.

- [ ] **Step 5: Run the migration in dry-run on the real state**

Run: `python -m scripts.migrate_fx_cash --state state` (dry-run)
Expected: rapport JSON `cash_before` vs `cash_after`. **Ne pas committer l'état tant qu'Erwan n'a pas validé l'écart.**

- [ ] **Step 6: Commit (script seulement, pas l'état)**

```bash
git add scripts/migrate_fx_cash.py tests/test_migrate_fx_cash.py
git commit -m "feat(fx): script migration recalcul cash USD (dry-run par défaut)"
```

---

## Task 9: Suite complète + registre décision

**Files:**
- Modify: `docs/decisions/registre-decisions-metier.md` (ajouter D14)

- [ ] **Step 1: Run the full test suite**

Run: `pytest -q`
Expected: PASS (corriger toute régression résiduelle des tests qui supposaient le mélange de devises ou la quantité agent).

- [ ] **Step 2: Add D14 to the decision registry**

Documenter D14 : « Comptabilité et sizing en base USD via couche FX ; l'agent ne propose plus la quantité ; taux live yfinance persistés sur le fill. » Référencer la spec et l'incident Realtek.

- [ ] **Step 3: Commit**

```bash
git add docs/decisions/registre-decisions-metier.md
git commit -m "docs(decisions): D14 conversion FX base USD"
```

---

## Self-Review (effectué)

- **Couverture spec** : §3.1→T1, §3.2/3.3→T2+T5, §4.1→T4+T5, §4.2→T3, §4.3→T6, §4.4→T7, §4.5→T5, §4.6→T5B, §5→T8, §6 invariants→répartis dans les tests de chaque task. ✅
- **Analyse = natif, jamais converti** : verrouillé par T5B (assert `p` natif) et par l'absence de toute conversion dans T6/T7 sur les niveaux de prix. ✅
- **Placeholders** : code réel dans chaque step ; le câblage `daemon.py` (T5) pointe les lignes exactes et fournit le code des fragments (fetch, sizing, propagation) — pas de « TODO ». ✅
- **Cohérence des types** : `fx_rate` (USD/unité), `to_usd(amount, ccy, rate)`, `currency_for(symbol)` cohérents de T1 à T8 ; `Fill.fx_rate` défaut 1.0 (rétro-compat) utilisé en T3/T6/T8. ✅
- **Point d'attention implémenteur** : `equity_usd` en T5 nécessite la valorisation USD des positions ouvertes (helper introduit en T7 côté cockpit) — si T5 précède T7, calculer inline puis factoriser. Ordonner T7 avant la finalisation de T5 si besoin.
