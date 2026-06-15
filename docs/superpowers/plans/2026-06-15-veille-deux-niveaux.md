# Veille à deux niveaux — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bâtir un radar daily (Tier 1) qui classe un grand pool liquide et pilote un hot-set dynamique (≤25) consommé par le hot-path 15m existant (Tier 2), avec décision code-défaut + override agent tracé.

**Architecture:** Modules purs (`radar.py` scoring, `rotation.py` sélection) + une couche data daily batchée (`radar_data.py`) + un job CLI `rotation --run` (EOD) qui écrit atomiquement `config/universe.yaml`. `universe.yaml` devient un output généré ; `starting_cash` déménage dans `config/portfolio.yaml`. Le hot-path 15m (`daemon.py`) est inchangé — seul le set de symboles qu'il lit devient dynamique.

**Tech Stack:** Python, pytest (TDD), PyYAML, yfinance (`yf.download` batché), réutilisation de `trader/features.py` (`compute_indicator_values`).

**Spec :** `docs/superpowers/specs/2026-06-15-veille-deux-niveaux-design.md` · **Décision métier :** registre D9.

**Convention de commit :** préfixes du repo (`feat(...)`, `test(...)`, `refactor(...)`). Terminer chaque message par `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Coder sur une branche dédiée `feat/veille-deux-niveaux`.

---

## File Structure

**Créés :**
- `config/portfolio.yaml` — `starting_cash` (sorti de `universe.yaml`).
- `config/pool.yaml` — pool (~500-800) + `hard_exclusions` + filtre liquidité.
- `config/radar.yaml` — poids du score, fenêtres, `atr_floor`, plafond amplitude, benchmarks par venue, `M`, `delta`, `dwell_days`, seuils sortie d'urgence.
- `config/conviction.yaml` — tilt par famille (vide par défaut).
- `trader/portfolio_config.py` — loader `starting_cash` (fallback transitoire sur `universe.yaml`).
- `trader/pool_config.py` — loader pool + exclusions, fail-fast.
- `trader/radar_config.py` — loader params radar + conviction, validation.
- `trader/radar_data.py` — fetch daily batché (ajusté) + cache + backoff + seuil de couverture.
- `trader/radar.py` — pur : éligibilité, score composite, classement, snapshot.
- `trader/rotation.py` — hystérésis + sortie d'urgence + sticky + override agent + écriture atomique + CLI `--run`.
- `trader/rotation_ledger.py` — journal `source="rotation"` (défaut vs final, as_of).
- Tests associés sous `tests/`.

**Modifiés :**
- `trader/daemon.py:1103` — `starting_equity` depuis `portfolio_config`.
- `trader/stats.py:114` — idem.
- `config/universe.yaml` — retrait de `starting_cash` (devient output `symbols:` seul).
- `tests/test_universe_config.py` — adapter si assertion sur `starting_cash`/composition statique.

---

## PHASE 0 — Migration `starting_cash` (fondation)

### Task 1: Loader `portfolio.yaml`

**Files:**
- Create: `config/portfolio.yaml`
- Create: `trader/portfolio_config.py`
- Test: `tests/test_portfolio_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_portfolio_config.py
from pathlib import Path
from trader.portfolio_config import load_starting_cash


def test_reads_portfolio_yaml(tmp_path: Path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "portfolio.yaml").write_text("starting_cash: 250000\n")
    assert load_starting_cash(cfg_dir) == 250000.0


def test_fallback_to_universe_then_default(tmp_path: Path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    # ni portfolio.yaml ni universe.yaml → défaut
    assert load_starting_cash(cfg_dir) == 100000.0
    # transition : universe.yaml porte encore starting_cash
    (cfg_dir / "universe.yaml").write_text("starting_cash: 50000\nsymbols: [SPY]\n")
    assert load_starting_cash(cfg_dir) == 50000.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_portfolio_config.py -v`
Expected: FAIL (`ModuleNotFoundError: trader.portfolio_config`)

- [ ] **Step 3: Write minimal implementation**

```python
# trader/portfolio_config.py
"""Source de vérité du capital de départ — sortie de universe.yaml (devenu généré)."""
from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_STARTING_CASH = 100_000.0


def load_starting_cash(config_dir: Path) -> float:
    """starting_cash depuis portfolio.yaml ; fallback transitoire universe.yaml ; défaut 100k.

    Fail-safe : tout fichier illisible → on passe au fallback suivant, jamais d'exception.
    """
    portfolio = config_dir / "portfolio.yaml"
    if portfolio.exists():
        try:
            cfg = yaml.safe_load(portfolio.read_text()) or {}
            return float(cfg.get("starting_cash", DEFAULT_STARTING_CASH))
        except Exception:
            pass
    universe = config_dir / "universe.yaml"
    if universe.exists():
        try:
            cfg = yaml.safe_load(universe.read_text()) or {}
            if "starting_cash" in cfg:
                return float(cfg["starting_cash"])
        except Exception:
            pass
    return DEFAULT_STARTING_CASH
```

```yaml
# config/portfolio.yaml
# Capital de départ du portefeuille paper. Sorti de universe.yaml (devenu un
# output généré par la rotation, cf registre D9). Lu par le daemon (SimBroker)
# et par stats.py.
starting_cash: 100000
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_portfolio_config.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add trader/portfolio_config.py config/portfolio.yaml tests/test_portfolio_config.py
git commit -m "feat(config): portfolio.yaml — starting_cash sorti de universe.yaml"
```

### Task 2: Repointer le daemon et stats vers `portfolio_config`

**Files:**
- Modify: `trader/daemon.py:1103`
- Modify: `trader/stats.py:114`
- Test: `tests/test_starting_cash_migration.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_starting_cash_migration.py
from pathlib import Path
from trader import stats as stats_mod


def test_stats_reads_starting_cash_from_portfolio(tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    # universe.yaml SANS starting_cash (cas post-migration : symbols seul)
    (cfg_dir / "universe.yaml").write_text("symbols: [SPY]\n")
    (cfg_dir / "portfolio.yaml").write_text("starting_cash: 333000\n")
    report = stats_mod.compute_stats(state_dir)
    assert report["starting_equity"] == 333000.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_starting_cash_migration.py -v`
Expected: FAIL (lit encore 100000 depuis universe.yaml sans starting_cash)

- [ ] **Step 3: Write minimal implementation**

Dans `trader/stats.py`, remplacer le bloc `# --- starting_equity depuis universe.yaml ---` (≈ lignes 113-120) par :

```python
    # --- starting_equity depuis portfolio.yaml (fallback universe.yaml) ---
    from .portfolio_config import load_starting_cash
    starting_equity: float = load_starting_cash(state_dir.parent / "config")
```

Dans `trader/daemon.py`, remplacer la ligne 1103 :

```python
    starting_equity = float(universe_cfg.get("starting_cash", 100_000))
```
par :
```python
    from .portfolio_config import load_starting_cash
    starting_equity = load_starting_cash(ROOT / "config")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_starting_cash_migration.py tests/test_daemon_status.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/daemon.py trader/stats.py tests/test_starting_cash_migration.py
git commit -m "refactor(config): starting_cash depuis portfolio.yaml (daemon + stats)"
```

### Task 3: Retirer `starting_cash` de `universe.yaml`

**Files:**
- Modify: `config/universe.yaml` (supprimer la ligne `starting_cash: 100000`)
- Modify: `tests/test_universe_config.py` (si assertion sur `starting_cash`)

- [ ] **Step 1: Vérifier les assertions existantes**

Run: `uv run grep -n "starting_cash" tests/test_universe_config.py`
Si une assertion existe, la retirer (le champ vit désormais dans `portfolio.yaml`, couvert par Task 1-2).

- [ ] **Step 2: Retirer le champ**

Supprimer la ligne `starting_cash: 100000` en tête de `config/universe.yaml`.

- [ ] **Step 3: Run full suite (régression migration)**

Run: `uv run pytest tests/test_universe_config.py tests/test_starting_cash_migration.py tests/test_daemon_status.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add config/universe.yaml tests/test_universe_config.py
git commit -m "refactor(config): universe.yaml ne porte plus starting_cash"
```

---

## PHASE 1 — Configs pool / radar / conviction (validées)

### Task 4: `pool.yaml` + loader avec exclusions dures

**Files:**
- Create: `config/pool.yaml`
- Create: `trader/pool_config.py`
- Test: `tests/test_pool_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pool_config.py
from pathlib import Path
import pytest
from trader.pool_config import load_pool, PoolConfigError


def _write(cfg_dir: Path, body: str) -> Path:
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "pool.yaml").write_text(body)
    return cfg_dir


def test_loads_symbols_and_excludes_hard_exclusions(tmp_path):
    cfg = _write(tmp_path, "symbols: [SPY, USO, CL=F]\nhard_exclusions: [CL=F, NG=F, GC=F]\n")
    pool = load_pool(cfg)
    assert "SPY" in pool.symbols and "USO" in pool.symbols
    assert "CL=F" not in pool.symbols          # exclu même si listé dans symbols
    assert pool.hard_exclusions == {"CL=F", "NG=F", "GC=F"}


def test_empty_pool_is_an_error(tmp_path):
    cfg = _write(tmp_path, "symbols: []\nhard_exclusions: []\n")
    with pytest.raises(PoolConfigError):
        load_pool(cfg)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pool_config.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write minimal implementation**

```python
# trader/pool_config.py
"""Pool Tier-1 + exclusions dures (source explicite — PAS regime.yaml, cf D9)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


class PoolConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Pool:
    symbols: tuple[str, ...]          # éligibles (exclusions retirées)
    hard_exclusions: frozenset[str]


def load_pool(config_dir: Path) -> Pool:
    path = config_dir / "pool.yaml"
    if not path.exists():
        raise PoolConfigError(f"pool_yaml_missing: {path}")
    cfg = yaml.safe_load(path.read_text()) or {}
    raw = [str(s) for s in (cfg.get("symbols") or [])]
    excl = frozenset(str(s) for s in (cfg.get("hard_exclusions") or []))
    eligible = tuple(s for s in dict.fromkeys(raw) if s not in excl)  # dedup + filtre
    if not eligible:
        raise PoolConfigError("pool_empty_after_exclusions")
    return Pool(symbols=eligible, hard_exclusions=excl)
```

```yaml
# config/pool.yaml
# Pool Tier-1 (radar). Source de vérité du POOL ; le hot-set (universe.yaml)
# en est dérivé par la rotation (cf registre D9). Élargir = éditer ici.
# hard_exclusions = exclusions DURES pour raison data (futures différés), pas
# une opinion. NE PAS confondre avec regime.yaml (attribution).
# v0 : graine restreinte ; à élargir vers ~500-800 (S&P500 + ETF sectoriels +
# indices monde + FX majors) lors de la calibration.
symbols:
  - SPY
  - QQQ
  - NVDA
  - USO
  - UNG
  - HO.PA
  - AM.PA
  - RHM.DE
  - 2330.TW
  - 2454.TW
  - EURUSD=X
  - USDJPY=X
hard_exclusions:
  - CL=F
  - NG=F
  - GC=F
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_pool_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/pool_config.py config/pool.yaml tests/test_pool_config.py
git commit -m "feat(radar): pool.yaml + exclusions dures (source explicite)"
```

### Task 5: `radar.yaml` + loader params

**Files:**
- Create: `config/radar.yaml`
- Create: `trader/radar_config.py`
- Test: `tests/test_radar_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_radar_config.py
from pathlib import Path
from trader.radar_config import load_radar_params


def test_defaults_and_overrides(tmp_path):
    cfg = tmp_path
    (cfg / "radar.yaml").write_text(
        "cap_m: 25\ndelta: 0.05\ndwell_days: 3\natr_floor: 0.01\n"
        "amplitude_cap: 0.05\nbenchmarks: {US: SPY, EU: ^FCHI}\n"
    )
    p = load_radar_params(cfg)
    assert p.cap_m == 25 and p.dwell_days == 3
    assert p.atr_floor == 0.01 and p.amplitude_cap == 0.05
    assert p.benchmarks["EU"] == "^FCHI"


def test_missing_file_yields_documented_defaults(tmp_path):
    p = load_radar_params(tmp_path)        # pas de fichier → défauts explicites
    assert p.cap_m == 25
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_radar_config.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write minimal implementation**

```python
# trader/radar_config.py
"""Paramètres du radar (calibrables). Défauts explicites — pas de magie (AX)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class RadarParams:
    cap_m: int = 25
    delta: float = 0.05               # marge d'hystérésis (swap)
    dwell_days: int = 3               # dwell min d'un chaud avant éviction
    atr_floor: float = 0.01           # amplitude min d'éligibilité (ATR%)
    amplitude_cap: float = 0.05       # saturation de la récompense d'amplitude
    emergency_score: float = 0.0      # sortie d'urgence si score < ce seuil
    w_trend: float = 1.0
    w_rs: float = 1.0
    w_amp: float = 1.0
    benchmarks: dict = field(default_factory=lambda: {"US": "SPY"})
    default_benchmark: str = "SPY"


def load_radar_params(config_dir: Path) -> RadarParams:
    path = config_dir / "radar.yaml"
    if not path.exists():
        return RadarParams()
    cfg = yaml.safe_load(path.read_text()) or {}
    known = RadarParams().__dict__
    kwargs = {k: cfg[k] for k in known if k in cfg}
    return RadarParams(**kwargs)
```

```yaml
# config/radar.yaml
# Paramètres du radar (calibrables via le bench de rotation). Cf registre D9 §14.
cap_m: 25
delta: 0.05
dwell_days: 3
atr_floor: 0.01
amplitude_cap: 0.05
emergency_score: 0.0
w_trend: 1.0
w_rs: 1.0
w_amp: 1.0
# Benchmark de force relative PAR VENUE (pas SPY global).
benchmarks:
  US: SPY
  EU: ^FCHI
  TW: ^TWII
default_benchmark: SPY
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_radar_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/radar_config.py config/radar.yaml tests/test_radar_config.py
git commit -m "feat(radar): radar.yaml — params calibrables (cap, hystérésis, score)"
```

### Task 6: `conviction.yaml` + validation du tilt

**Files:**
- Create: `config/conviction.yaml`
- Modify: `trader/radar_config.py` (ajouter `load_conviction`)
- Test: `tests/test_conviction.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_conviction.py
import pytest
from trader.radar_config import load_conviction, ConvictionError


def test_empty_default(tmp_path):
    assert load_conviction(tmp_path, known_families={"energy"}) == {}


def test_valid_tilt(tmp_path):
    (tmp_path / "conviction.yaml").write_text("energy: 0.30\n")
    assert load_conviction(tmp_path, known_families={"energy"}) == {"energy": 0.30}


def test_unknown_family_rejected(tmp_path):
    (tmp_path / "conviction.yaml").write_text("zzz: 0.1\n")
    with pytest.raises(ConvictionError):
        load_conviction(tmp_path, known_families={"energy"})


def test_tilt_le_minus_one_rejected(tmp_path):
    (tmp_path / "conviction.yaml").write_text("energy: -1.0\n")
    with pytest.raises(ConvictionError):
        load_conviction(tmp_path, known_families={"energy"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_conviction.py -v`
Expected: FAIL (`ImportError: cannot import name 'load_conviction'`)

- [ ] **Step 3: Write minimal implementation**

Ajouter à `trader/radar_config.py` :

```python
class ConvictionError(ValueError):
    pass


def load_conviction(config_dir: Path, known_families: set[str]) -> dict[str, float]:
    """Tilt par famille, vide par défaut. Schéma strict (fail-fast, AX)."""
    path = config_dir / "conviction.yaml"
    if not path.exists():
        return {}
    cfg = yaml.safe_load(path.read_text()) or {}
    out: dict[str, float] = {}
    for fam, raw in cfg.items():
        if fam not in known_families:
            raise ConvictionError(f"unknown_family: {fam}")
        try:
            tilt = float(raw)
        except (TypeError, ValueError):
            raise ConvictionError(f"non_numeric_tilt: {fam}={raw!r}")
        if tilt <= -1.0:
            raise ConvictionError(f"tilt_le_minus_one: {fam}={tilt}")
        out[fam] = tilt
    return out
```

```yaml
# config/conviction.yaml
# Tilt de conviction par famille (cf registre D9). VIDE par défaut = 100 %
# systématique. score_final = score × (1 + tilt_famille). Famille = clé de
# trader/semantic/catalog.py FAMILIES. tilt > -1 obligatoire.
# Exemple : energy: 0.30  # +30 % pendant une thèse géopo
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_conviction.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/radar_config.py config/conviction.yaml tests/test_conviction.py
git commit -m "feat(radar): conviction.yaml — tilt validé, vide par défaut"
```

---

## PHASE 2 — Radar (data + scoring)

### Task 7: `radar_data.py` — fetch daily batché, ajusté, avec couverture

**Files:**
- Create: `trader/radar_data.py`
- Test: `tests/test_radar_data.py`

Note : la fonction de téléchargement est **injectée** (`fetch_fn`) pour rester déterministe et testable sans réseau. Le défaut prod l'enverra vers `yf.download([...], auto_adjust=True, ...)` (séries ajustées — corrige les corporate actions).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_radar_data.py
from trader.radar_data import fetch_daily, CoverageError
import pytest


def _fake_fetch(symbols):
    # renvoie 3 barres pour tous sauf 'DEAD' (pas de data)
    return {s: ([] if s == "DEAD" else [object(), object(), object()]) for s in symbols}


def test_returns_only_covered_symbols():
    bars = fetch_daily(["SPY", "DEAD"], fetch_fn=_fake_fetch, min_coverage=0.0)
    assert "SPY" in bars and "DEAD" not in bars


def test_coverage_below_threshold_raises():
    with pytest.raises(CoverageError):
        fetch_daily(["SPY", "DEAD"], fetch_fn=_fake_fetch, min_coverage=0.9)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_radar_data.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write minimal implementation**

```python
# trader/radar_data.py
"""Couche data du radar : fetch daily batché + seuil de couverture.

Le fetch est injecté (déterminisme/tests). Le défaut prod batche via
yf.download(..., auto_adjust=True) (séries AJUSTÉES — corrige splits/dividendes,
contrairement au get_bars 15m qui est auto_adjust=False) avec cache + backoff.
"""
from __future__ import annotations

from typing import Callable


class CoverageError(RuntimeError):
    pass


def fetch_daily(
    symbols: list[str],
    *,
    fetch_fn: Callable[[list[str]], dict[str, list]],
    min_coverage: float = 0.8,
) -> dict[str, list]:
    """{symbol: bars} pour les symboles avec data ; lève si couverture < seuil.

    Un scan partiel (rate-limit) biaiserait le classement → on refuse sous le seuil
    plutôt que de classer sur un sous-ensemble (cf spec §9 échec radar massif).
    """
    raw = fetch_fn(symbols)
    covered = {s: bars for s, bars in raw.items() if bars}
    if symbols and len(covered) / len(symbols) < min_coverage:
        raise CoverageError(f"coverage {len(covered)}/{len(symbols)} < {min_coverage}")
    return covered
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_radar_data.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/radar_data.py tests/test_radar_data.py
git commit -m "feat(radar): couche data daily batchée + seuil de couverture"
```

### Task 8: `radar.py` — éligibilité

**Files:**
- Create: `trader/radar.py`
- Test: `tests/test_radar.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_radar.py
from trader.radar import is_eligible


def test_excluded_symbol_not_eligible():
    assert not is_eligible("CL=F", amplitude=0.10, hard_exclusions={"CL=F"}, atr_floor=0.01)


def test_too_calm_not_eligible():
    assert not is_eligible("SPY", amplitude=0.002, hard_exclusions=set(), atr_floor=0.01)


def test_missing_amplitude_not_eligible():
    assert not is_eligible("SPY", amplitude=None, hard_exclusions=set(), atr_floor=0.01)


def test_tradable_is_eligible():
    assert is_eligible("SPY", amplitude=0.03, hard_exclusions=set(), atr_floor=0.01)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_radar.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write minimal implementation**

```python
# trader/radar.py
"""Radar Tier-1 : éligibilité + score composite + classement. Fonction pure.

Score (cf registre D9 §5) : on veut une tendance PROPRE (efficiency_ratio) ET de
l'AMPLITUDE (ohlc_volatility récompensée bornée, pas pénalisée). Réutilise les
primitives publiques de features.compute_indicator_values.
"""
from __future__ import annotations


def is_eligible(symbol: str, *, amplitude: float | None,
                hard_exclusions: set[str], atr_floor: float) -> bool:
    """Éligible si non-exclu, data présente (amplitude calculée) et assez volatil."""
    if symbol in hard_exclusions:
        return False
    if amplitude is None:
        return False
    return amplitude >= atr_floor
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_radar.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/radar.py tests/test_radar.py
git commit -m "feat(radar): filtre d'éligibilité (exclusions, data, plancher ATR%)"
```

### Task 9: `radar.py` — score composite (propriétés)

**Files:**
- Modify: `trader/radar.py`
- Test: `tests/test_radar.py`

On teste les **invariants** du score (pas des constantes magiques calibrables) : amplitude récompensée mais saturante, tendance propre favorisée, force relative favorisée, tilt appliqué.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_radar.py  (ajouts)
from trader.radar import score_symbol


def _base():  # composants neutres
    return dict(efficiency_ratio=0.5, ret=0.02, benchmark_ret=0.0,
                amplitude=0.02, tilt=0.0,
                amplitude_cap=0.05, w_trend=1.0, w_rs=1.0, w_amp=1.0)


def test_cleaner_trend_scores_higher():
    low = score_symbol(**{**_base(), "efficiency_ratio": 0.2})
    high = score_symbol(**{**_base(), "efficiency_ratio": 0.9})
    assert high > low


def test_beating_benchmark_scores_higher():
    lag = score_symbol(**{**_base(), "ret": 0.00, "benchmark_ret": 0.02})
    lead = score_symbol(**{**_base(), "ret": 0.04, "benchmark_ret": 0.02})
    assert lead > lag


def test_amplitude_rewarded_but_saturates():
    calm = score_symbol(**{**_base(), "amplitude": 0.02})
    loud = score_symbol(**{**_base(), "amplitude": 0.04})
    insane = score_symbol(**{**_base(), "amplitude": 0.20})  # au-delà du cap
    capped = score_symbol(**{**_base(), "amplitude": 0.05})  # = cap
    assert loud > calm
    assert insane == capped          # saturation au plafond


def test_positive_tilt_increases_score():
    assert score_symbol(**{**_base(), "tilt": 0.30}) > score_symbol(**_base())


def test_sign_follows_direction():
    up = score_symbol(**{**_base(), "ret": 0.03})
    down = score_symbol(**{**_base(), "ret": -0.03})
    assert up > 0 > down            # biais long/short porté par le signe
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_radar.py -v`
Expected: FAIL (`ImportError: score_symbol`)

- [ ] **Step 3: Write minimal implementation**

```python
# trader/radar.py  (ajouts)
def score_symbol(*, efficiency_ratio: float, ret: float, benchmark_ret: float,
                 amplitude: float, tilt: float, amplitude_cap: float,
                 w_trend: float, w_rs: float, w_amp: float) -> float:
    """Score composite signé. Magnitude = attractivité ; signe = biais long/short.

    trend  = efficiency_ratio (propreté) porté par le signe du rendement.
    rs     = surperformance vs benchmark de la venue (force relative).
    amp    = amplitude récompensée mais bornée à amplitude_cap (anti-chaos).
    Forme additive-pondérée (calibrable, cf D9 §14) ; la vol n'est JAMAIS soustraite.
    """
    direction = 1.0 if ret >= 0 else -1.0
    trend = efficiency_ratio * direction
    rs = ret - benchmark_ret
    amp = min(amplitude, amplitude_cap)
    raw = w_trend * trend + w_rs * rs * direction
    return raw * (1.0 + w_amp * amp) * (1.0 + tilt)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_radar.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/radar.py tests/test_radar.py
git commit -m "feat(radar): score composite (tendance × force relative × amplitude bornée)"
```

### Task 10: `radar.py` — `scan_and_rank` + snapshot

**Files:**
- Modify: `trader/radar.py`
- Test: `tests/test_radar.py`

Branche les composants sur `features.compute_indicator_values` et produit le classement + un snapshot machine-readable. Le calcul des composants par symbole est injecté (`indicators_fn`) pour tester sans data réelle.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_radar.py  (ajouts)
from trader.radar import scan_and_rank


def test_rank_orders_by_score_and_records_ineligible():
    bars_by_symbol = {"SPY": "barsA", "ZZZ": "barsB", "CL=F": "barsC"}
    # indicateurs injectés : ZZZ trop calme (amplitude < floor), CL=F exclu
    def indicators_fn(symbol, bars):
        table = {
            "SPY": dict(efficiency_ratio=0.8, ret=0.03, amplitude=0.03),
            "ZZZ": dict(efficiency_ratio=0.9, ret=0.05, amplitude=0.001),
            "CL=F": dict(efficiency_ratio=0.7, ret=0.04, amplitude=0.05),
        }
        return table[symbol]

    result = scan_and_rank(
        bars_by_symbol,
        indicators_fn=indicators_fn,
        benchmark_ret_for={"SPY": 0.0, "ZZZ": 0.0, "CL=F": 0.0},
        tilt_for=lambda sym: 0.0,
        hard_exclusions={"CL=F"},
        atr_floor=0.01, amplitude_cap=0.05,
        w_trend=1.0, w_rs=1.0, w_amp=1.0,
    )
    assert [r["symbol"] for r in result["ranked"]] == ["SPY"]
    assert set(result["ineligible"]) == {"ZZZ", "CL=F"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_radar.py -v`
Expected: FAIL (`ImportError: scan_and_rank`)

- [ ] **Step 3: Write minimal implementation**

```python
# trader/radar.py  (ajouts)
from typing import Callable


def scan_and_rank(bars_by_symbol: dict[str, object], *,
                  indicators_fn: Callable[[str, object], dict],
                  benchmark_ret_for: dict[str, float],
                  tilt_for: Callable[[str], float],
                  hard_exclusions: set[str], atr_floor: float, amplitude_cap: float,
                  w_trend: float, w_rs: float, w_amp: float) -> dict:
    """Classe le pool. Retourne {ranked:[{symbol,score,bias}...], ineligible:[...]}.

    Tri stable, déterministe (départage par symbole) — pas de hasard, pas d'horloge.
    """
    ranked, ineligible = [], []
    for symbol in sorted(bars_by_symbol):                    # ordre déterministe
        ind = indicators_fn(symbol, bars_by_symbol[symbol])
        amplitude = ind.get("amplitude")
        if not is_eligible(symbol, amplitude=amplitude,
                           hard_exclusions=hard_exclusions, atr_floor=atr_floor):
            ineligible.append(symbol)
            continue
        score = score_symbol(
            efficiency_ratio=ind["efficiency_ratio"], ret=ind["ret"],
            benchmark_ret=benchmark_ret_for.get(symbol, 0.0),
            amplitude=amplitude, tilt=tilt_for(symbol),
            amplitude_cap=amplitude_cap, w_trend=w_trend, w_rs=w_rs, w_amp=w_amp,
        )
        ranked.append({"symbol": symbol, "score": score,
                       "bias": "long" if score >= 0 else "short"})
    ranked.sort(key=lambda r: (-abs(r["score"]), r["symbol"]))  # attractivité desc
    return {"ranked": ranked, "ineligible": sorted(ineligible)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_radar.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/radar.py tests/test_radar.py
git commit -m "feat(radar): scan_and_rank + classement déterministe + inéligibles"
```

---

## PHASE 3 — Rotation (sélection + écriture)

### Task 11: `rotation.py` — hystérésis

**Files:**
- Create: `trader/rotation.py`
- Test: `tests/test_rotation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rotation.py
from trader.rotation import apply_hysteresis


def test_no_swap_below_delta():
    ranked = [{"symbol": "A", "score": 1.00}, {"symbol": "B", "score": 0.96}]
    # B veut entrer mais ne bat pas A de delta=0.05 → A reste
    out = apply_hysteresis(ranked, current={"A"}, dwell={"A": 5}, cap_m=1,
                           delta=0.05, dwell_days=3)
    assert out == ["A"]


def test_swap_when_margin_and_dwell_satisfied():
    ranked = [{"symbol": "B", "score": 1.20}, {"symbol": "A", "score": 1.00}]
    out = apply_hysteresis(ranked, current={"A"}, dwell={"A": 5}, cap_m=1,
                           delta=0.05, dwell_days=3)
    assert out == ["B"]


def test_no_swap_before_dwell_even_if_margin():
    ranked = [{"symbol": "B", "score": 2.0}, {"symbol": "A", "score": 1.0}]
    out = apply_hysteresis(ranked, current={"A"}, dwell={"A": 1}, cap_m=1,
                           delta=0.05, dwell_days=3)   # A chaud depuis 1 j < 3
    assert out == ["A"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rotation.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write minimal implementation**

```python
# trader/rotation.py
"""Rotation : hystérésis + sortie d'urgence + sticky + override + écriture atomique.

Fonctions pures pour la logique ; le job CLI (--run) orchestre les I/O.
"""
from __future__ import annotations


def apply_hysteresis(ranked: list[dict], *, current: set[str], dwell: dict[str, int],
                     cap_m: int, delta: float, dwell_days: int) -> list[str]:
    """Hot-set défaut : remplit jusqu'à cap_m, protège les sortants par delta+dwell.

    Un entrant ne déloge un chaud que si score_entrant > score_sortant + delta ET
    que le sortant est chaud depuis >= dwell_days. Déterministe.
    """
    by_symbol = {r["symbol"]: r["score"] for r in ranked}
    order = [r["symbol"] for r in ranked]                 # déjà trié par attractivité
    selected: list[str] = []
    for sym in order:
        if len(selected) < cap_m:
            selected.append(sym)
            continue
        # pour entrer, battre le plus faible chaud délogeable
        incumbents = [s for s in selected if s in current]
        delogeables = [s for s in incumbents if dwell.get(s, 0) >= dwell_days]
        if not delogeables:
            continue
        weakest = min(delogeables, key=lambda s: by_symbol.get(s, float("-inf")))
        if by_symbol[sym] > by_symbol.get(weakest, float("-inf")) + delta:
            selected.remove(weakest)
            selected.append(sym)
    return selected[:cap_m]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rotation.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/rotation.py tests/test_rotation.py
git commit -m "feat(rotation): hystérésis (marge delta + dwell) sur le hot-set défaut"
```

### Task 12: `rotation.py` — sortie d'urgence

**Files:**
- Modify: `trader/rotation.py`
- Test: `tests/test_rotation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rotation.py  (ajouts)
from trader.rotation import emergency_exits


def test_chaud_sous_seuil_est_evince_malgre_dwell():
    ranked = [{"symbol": "A", "score": -0.5}, {"symbol": "B", "score": 1.0}]
    evicted = emergency_exits(["A", "B"], ranked, emergency_score=0.0)
    assert evicted == {"A"}            # A sous le seuil absolu → sortie immédiate


def test_rien_si_tous_au_dessus():
    ranked = [{"symbol": "A", "score": 0.3}, {"symbol": "B", "score": 1.0}]
    assert emergency_exits(["A", "B"], ranked, emergency_score=0.0) == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rotation.py -v`
Expected: FAIL (`ImportError: emergency_exits`)

- [ ] **Step 3: Write minimal implementation**

```python
# trader/rotation.py  (ajouts)
def emergency_exits(hot_set: list[str], ranked: list[dict], *,
                    emergency_score: float) -> set[str]:
    """Chauds dont le score passe sous le seuil absolu → éviction immédiate.

    Court-circuite le dwell (anti stale-loser sur choc/gap, cf D9 §6.1).
    """
    by_symbol = {r["symbol"]: r["score"] for r in ranked}
    return {s for s in hot_set if by_symbol.get(s, 0.0) < emergency_score}
```

Et l'appliquer dans la composition (le job appellera `apply_hysteresis` puis retirera `emergency_exits`).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rotation.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/rotation.py tests/test_rotation.py
git commit -m "feat(rotation): sortie d'urgence sur seuil absolu (anti stale-loser)"
```

### Task 13: `rotation.py` — sticky (union, hors quota)

**Files:**
- Modify: `trader/rotation.py`
- Test: `tests/test_rotation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rotation.py  (ajouts)
from trader.rotation import sticky_symbols, compose_final


def test_sticky_union_des_sources():
    s = sticky_symbols(positions={"AAA"}, armed_plans={"BBB"},
                       exit_watches={"CCC"}, pending_orders={"DDD"})
    assert s == {"AAA", "BBB", "CCC", "DDD"}


def test_compose_sticky_hors_quota_et_over_cap():
    # cap 2, mais 3 sticky → sticky tous gardés, 0 slot libre, alerte
    final, alert = compose_final(default_hot=["X", "Y"], sticky={"A", "B", "C"}, cap_m=2)
    assert set(final) == {"A", "B", "C"}        # sticky hors quota, tous présents
    assert alert == "sticky_over_cap"


def test_compose_sticky_plus_slots_libres():
    final, alert = compose_final(default_hot=["X", "Y", "Z"], sticky={"A"}, cap_m=2)
    # 1 sticky + slots libres = max(0, 2-1)=1 → A + X
    assert set(final) == {"A", "X"}
    assert alert is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rotation.py -v`
Expected: FAIL (`ImportError`)

- [ ] **Step 3: Write minimal implementation**

```python
# trader/rotation.py  (ajouts)
def sticky_symbols(*, positions: set[str], armed_plans: set[str],
                   exit_watches: set[str], pending_orders: set[str]) -> set[str]:
    """Symboles intouchables : union de toutes les sources de garde (cf D9 §6.2)."""
    return positions | armed_plans | exit_watches | pending_orders


def compose_final(*, default_hot: list[str], sticky: set[str],
                  cap_m: int) -> tuple[list[str], str | None]:
    """Hot-set final = sticky (hors quota) ∪ slots libres du défaut.

    free = max(0, cap_m - |sticky|). Si |sticky| > cap_m → 0 slot + alerte.
    """
    alert = "sticky_over_cap" if len(sticky) > cap_m else None
    free = max(0, cap_m - len(sticky))
    extras = [s for s in default_hot if s not in sticky][:free]
    final = sorted(sticky) + extras
    # dédup en préservant l'ordre
    seen, out = set(), []
    for s in final:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out, alert
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rotation.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/rotation.py tests/test_rotation.py
git commit -m "feat(rotation): sticky hors quota (union positions/plans/watches/pending)"
```

### Task 14: `rotation.py` — écriture atomique de `universe.yaml`

**Files:**
- Modify: `trader/rotation.py`
- Test: `tests/test_rotation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rotation.py  (ajouts)
import yaml
from trader.rotation import write_universe_atomic, UniverseWriteError
import pytest


def test_writes_symbols_only_atomically(tmp_path):
    path = tmp_path / "universe.yaml"
    write_universe_atomic(path, ["SPY", "QQQ"])
    cfg = yaml.safe_load(path.read_text())
    assert cfg == {"symbols": ["SPY", "QQQ"]}
    assert not list(tmp_path.glob("*.tmp*"))      # pas de fichier temporaire laissé


def test_refuses_empty_symbols(tmp_path):
    with pytest.raises(UniverseWriteError):
        write_universe_atomic(tmp_path / "universe.yaml", [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rotation.py -v`
Expected: FAIL (`ImportError`)

- [ ] **Step 3: Write minimal implementation**

```python
# trader/rotation.py  (ajouts)
import os
import tempfile
from pathlib import Path

import yaml


class UniverseWriteError(ValueError):
    pass


def write_universe_atomic(path: Path, symbols: list[str]) -> None:
    """Écrit {symbols:[...]} via tmp + os.replace. Valide avant remplacement.

    Jamais de fichier partiel lu par le daemon (qui relit universe.yaml en boucle
    et réconcilie le scheduler — une écriture non atomique purgerait wakes/watches).
    """
    if not symbols:
        raise UniverseWriteError("refuse_empty_universe")
    path = Path(path)
    payload = yaml.safe_dump({"symbols": list(symbols)}, allow_unicode=True,
                             sort_keys=False)
    yaml.safe_load(payload)                       # validation avant replace
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)                     # atomique
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rotation.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/rotation.py tests/test_rotation.py
git commit -m "feat(rotation): écriture atomique de universe.yaml (tmp + os.replace)"
```

### Task 15: `rotation.py` — application de l'override agent

**Files:**
- Modify: `trader/rotation.py`
- Test: `tests/test_rotation.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rotation.py  (ajouts)
from trader.rotation import apply_override


def test_valid_override_add_and_remove():
    final, rejects = apply_override(
        default_hot=["A", "B"], add=["C"], remove=["B"],
        pool={"A", "B", "C", "D"}, sticky=set(), cap_m=25)
    assert set(final) == {"A", "C"} and rejects == []


def test_override_out_of_pool_rejected():
    final, rejects = apply_override(
        default_hot=["A"], add=["ZZZ"], remove=[],
        pool={"A", "B"}, sticky=set(), cap_m=25)
    assert set(final) == {"A"}
    assert rejects == [{"symbol": "ZZZ", "reason": "out_of_pool"}]


def test_override_cannot_remove_sticky():
    final, rejects = apply_override(
        default_hot=["A", "B"], add=[], remove=["A"],
        pool={"A", "B"}, sticky={"A"}, cap_m=25)
    assert "A" in final
    assert {"symbol": "A", "reason": "sticky_protected"} in rejects
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rotation.py -v`
Expected: FAIL (`ImportError`)

- [ ] **Step 3: Write minimal implementation**

```python
# trader/rotation.py  (ajouts)
def apply_override(*, default_hot: list[str], add: list[str], remove: list[str],
                   pool: set[str], sticky: set[str], cap_m: int) -> tuple[list[str], list[dict]]:
    """Applique les overrides agent, tracés. Rejets machine-readable (AX).

    Refuse : symbole hors pool, retrait d'un sticky, dépassement du cap.
    """
    hot = list(default_hot)
    rejects: list[dict] = []
    for sym in remove:
        if sym in sticky:
            rejects.append({"symbol": sym, "reason": "sticky_protected"})
            continue
        if sym in hot:
            hot.remove(sym)
    for sym in add:
        if sym not in pool:
            rejects.append({"symbol": sym, "reason": "out_of_pool"})
            continue
        if sym in hot:
            continue
        if len([s for s in hot if s not in sticky]) >= cap_m:
            rejects.append({"symbol": sym, "reason": "cap_exceeded"})
            continue
        hot.append(sym)
    return hot, rejects
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rotation.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/rotation.py tests/test_rotation.py
git commit -m "feat(rotation): override agent tracé (rejets out_of_pool/sticky/cap)"
```

### Task 16: `rotation_ledger.py` — journal défaut vs final

**Files:**
- Create: `trader/rotation_ledger.py`
- Test: `tests/test_rotation_ledger.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rotation_ledger.py
import json
from trader.rotation_ledger import log_rotation


def test_appends_jsonl_with_as_of_and_both_sets(tmp_path):
    path = tmp_path / "rotation_ledger.jsonl"
    log_rotation(path, as_of="2026-06-15", default_hot=["A", "B"],
                 final_hot=["A", "C"], overrides={"add": ["C"], "remove": ["B"]},
                 rejects=[], alert=None)
    line = json.loads(path.read_text().splitlines()[-1])
    assert line["source"] == "rotation"
    assert line["as_of"] == "2026-06-15"
    assert line["default_hot_set"] == ["A", "B"]
    assert line["final_hot_set"] == ["A", "C"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rotation_ledger.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write minimal implementation**

```python
# trader/rotation_ledger.py
"""Journal des décisions de composition d'univers (distinct du decision_ledger
par-trade). Permet le contrefactuel défaut-vs-final (cf D9 §11)."""
from __future__ import annotations

import json
from pathlib import Path


def log_rotation(path: Path, *, as_of: str, default_hot: list[str], final_hot: list[str],
                 overrides: dict, rejects: list[dict], alert: str | None) -> None:
    """Append-only JSONL. as_of fourni par l'appelant (déterminisme : pas d'horloge ici)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "source": "rotation",
        "as_of": as_of,
        "default_hot_set": list(default_hot),
        "final_hot_set": list(final_hot),
        "overrides": overrides,
        "rejects": rejects,
        "alert": alert,
    }
    with path.open("a") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rotation_ledger.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/rotation_ledger.py tests/test_rotation_ledger.py
git commit -m "feat(rotation): rotation_ledger — contrefactuel défaut vs final (as_of)"
```

### Task 17: CLI `rotation --run` (orchestration + échec Codex → défaut)

**Files:**
- Modify: `trader/rotation.py` (ajouter `run` + `main`)
- Test: `tests/test_rotation_run.py`

`run()` prend ses dépendances en injection (data, override agent, horloge `as_of`) pour rester testable et déterministe. La CLI `main` câble les vraies dépendances (pattern `consolidator.py:482`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rotation_run.py
import yaml
from pathlib import Path
from trader.rotation import run


def _ranked(symbols):
    return {"ranked": [{"symbol": s, "score": 1.0 - i * 0.01}
                       for i, s in enumerate(symbols)], "ineligible": []}


def test_run_writes_universe_and_ledger(tmp_path):
    cfg = tmp_path / "config"; cfg.mkdir()
    state = tmp_path / "state"; state.mkdir()
    out = run(
        config_dir=cfg, state_dir=state, as_of="2026-06-15",
        rank_fn=lambda: _ranked(["A", "B", "C"]),
        sticky_fn=lambda: {"Z"},
        override_fn=lambda payload: {"add": [], "remove": []},
        current=set(), dwell={}, cap_m=2, delta=0.05, dwell_days=3,
        emergency_score=-1.0, pool={"A", "B", "C", "Z"},
    )
    universe = yaml.safe_load((cfg / "universe.yaml").read_text())
    assert "Z" in universe["symbols"]            # sticky présent
    assert (state / "rotation_ledger.jsonl").exists()
    assert out["final_hot_set"]


def test_run_falls_back_to_default_on_agent_failure(tmp_path):
    cfg = tmp_path / "config"; cfg.mkdir()
    state = tmp_path / "state"; state.mkdir()
    def boom(payload):
        raise TimeoutError("codex down")
    out = run(
        config_dir=cfg, state_dir=state, as_of="2026-06-15",
        rank_fn=lambda: _ranked(["A", "B"]),
        sticky_fn=lambda: set(),
        override_fn=boom,                         # agent KO
        current=set(), dwell={}, cap_m=5, delta=0.05, dwell_days=3,
        emergency_score=-1.0, pool={"A", "B"},
    )
    assert out["alert"] == "override_unavailable"
    assert set(out["final_hot_set"]) == {"A", "B"}   # commit du défaut
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rotation_run.py -v`
Expected: FAIL (`ImportError: run`)

- [ ] **Step 3: Write minimal implementation**

```python
# trader/rotation.py  (ajouts)
from typing import Callable


def run(*, config_dir: Path, state_dir: Path, as_of: str,
        rank_fn: Callable[[], dict], sticky_fn: Callable[[], set],
        override_fn: Callable[[dict], dict],
        current: set[str], dwell: dict[str, int], cap_m: int, delta: float,
        dwell_days: int, emergency_score: float, pool: set[str]) -> dict:
    """Un cycle de rotation EOD. I/O injectées → déterministe et testable."""
    from .rotation_ledger import log_rotation

    ranked_obj = rank_fn()
    ranked = ranked_obj["ranked"]
    default_hot = apply_hysteresis(ranked, current=current, dwell=dwell, cap_m=cap_m,
                                   delta=delta, dwell_days=dwell_days)
    evicted = emergency_exits(default_hot, ranked, emergency_score=emergency_score)
    default_hot = [s for s in default_hot if s not in evicted]

    sticky = sticky_fn()
    alert: str | None = None
    overrides = {"add": [], "remove": []}
    rejects: list[dict] = []
    try:
        overrides = override_fn({"ranked": ranked, "default_hot": default_hot})
        hot, rejects = apply_override(
            default_hot=default_hot, add=overrides.get("add", []),
            remove=overrides.get("remove", []), pool=pool, sticky=sticky, cap_m=cap_m)
    except Exception:
        hot, alert = default_hot, "override_unavailable"   # commit du défaut (spec §8)

    final, cap_alert = compose_final(default_hot=hot, sticky=sticky, cap_m=cap_m)
    alert = alert or cap_alert
    write_universe_atomic(config_dir / "universe.yaml", final)
    log_rotation(state_dir / "rotation_ledger.jsonl", as_of=as_of,
                 default_hot=default_hot, final_hot=final, overrides=overrides,
                 rejects=rejects, alert=alert)
    return {"final_hot_set": final, "default_hot_set": default_hot, "alert": alert}


def main(argv: list[str] | None = None) -> int:
    """CLI : python -m trader.rotation --run (pattern consolidator.py:482).

    Câble les vraies dépendances : radar_data.fetch_daily, radar.scan_and_rank,
    sticky depuis broker/plans/watches, override via codex_client. as_of = dernière
    barre daily clôturée. (Détail du câblage hors logique pure — couvert par un
    test smoke d'intégration séparé.)
    """
    import argparse
    parser = argparse.ArgumentParser(prog="trader.rotation")
    parser.add_argument("--run", action="store_true", help="exécute un cycle EOD")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--state-dir", default="state")
    args = parser.parse_args(argv)
    if not args.run:
        parser.print_help()
        return 0
    # Câblage prod assemblé ici (rank_fn/sticky_fn/override_fn réels) puis run(...).
    raise SystemExit("wiring prod assemblé en Task 18 (intégration)")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rotation_run.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/rotation.py tests/test_rotation_run.py
git commit -m "feat(rotation): run() EOD orchestré + fallback défaut si agent KO"
```

---

## PHASE 4 — Câblage prod + mesure d'alpha

### Task 18: Câblage prod de `main` (intégration) + `as_of` par venue

**Files:**
- Modify: `trader/rotation.py` (assembler les vraies `rank_fn`/`sticky_fn`/`override_fn`)
- Test: `tests/test_rotation_integration.py` (smoke, fetch injecté)

- [ ] **Step 1: Write the failing smoke test**

```python
# tests/test_rotation_integration.py
from pathlib import Path
from trader.rotation import build_rank_fn


def test_build_rank_fn_uses_pool_and_radar(tmp_path, monkeypatch):
    cfg = tmp_path / "config"; cfg.mkdir()
    (cfg / "pool.yaml").write_text("symbols: [SPY, QQQ]\nhard_exclusions: []\n")
    (cfg / "radar.yaml").write_text("atr_floor: 0.0\namplitude_cap: 0.05\n")
    # fetch + indicateurs injectés → SPY classé
    fake_bars = {"SPY": ["b"], "QQQ": ["b"]}
    def fake_fetch(symbols): return {s: fake_bars[s] for s in symbols}
    def fake_ind(symbol, bars): return dict(efficiency_ratio=0.7, ret=0.02, amplitude=0.03)
    rank_fn = build_rank_fn(cfg, fetch_fn=fake_fetch, indicators_fn=fake_ind,
                            benchmark_ret_for={"SPY": 0.0, "QQQ": 0.0})
    out = rank_fn()
    assert {r["symbol"] for r in out["ranked"]} == {"SPY", "QQQ"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rotation_integration.py -v`
Expected: FAIL (`ImportError: build_rank_fn`)

- [ ] **Step 3: Write minimal implementation**

```python
# trader/rotation.py  (ajouts)
def build_rank_fn(config_dir: Path, *, fetch_fn, indicators_fn, benchmark_ret_for):
    """Assemble la rank_fn de prod depuis pool.yaml + radar.yaml + conviction.yaml."""
    from .pool_config import load_pool
    from .radar_config import load_radar_params, load_conviction
    from .radar_data import fetch_daily
    from .radar import scan_and_rank
    from .semantic.catalog import FAMILIES, family_for_symbol

    pool = load_pool(config_dir)
    params = load_radar_params(config_dir)
    tilt = load_conviction(config_dir, known_families=set(FAMILIES))

    def rank_fn() -> dict:
        bars = fetch_daily(list(pool.symbols), fetch_fn=fetch_fn, min_coverage=0.0)
        return scan_and_rank(
            bars, indicators_fn=indicators_fn, benchmark_ret_for=benchmark_ret_for,
            tilt_for=lambda s: tilt.get(family_for_symbol(s), 0.0),
            hard_exclusions=set(pool.hard_exclusions), atr_floor=params.atr_floor,
            amplitude_cap=params.amplitude_cap, w_trend=params.w_trend,
            w_rs=params.w_rs, w_amp=params.w_amp)
    return rank_fn
```

Puis dans `main`, remplacer le `raise SystemExit(...)` par l'assemblage réel : `build_rank_fn` (fetch prod = `yf.download` ajusté ; `indicators_fn` = `features.compute_indicator_values` sur barres daily ; `benchmark_ret_for` calculé depuis `radar.yaml` benchmarks par venue), `sticky_fn` (positions broker + plans armés + exit watches + pending), `override_fn` (un appel `codex_client`), `as_of` = dernière barre daily clôturée. Appeler `run(...)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rotation_integration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/rotation.py tests/test_rotation_integration.py
git commit -m "feat(rotation): câblage prod rank_fn (pool+radar+conviction+benchmarks venue)"
```

### Task 19: Bench de rotation as-of (mesure d'alpha)

**Files:**
- Create: `trader/rotation_bench.py`
- Test: `tests/test_rotation_bench.py`

Compare le hot-set dynamique à des baselines (univers statique, top-liquidité) sur le P&L 15m conditionné par appartenance — depuis l'`as_of` journalisé (corrige le finding « bench pollué par l'univers courant »).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rotation_bench.py
from trader.rotation_bench import membership_pnl


def test_pnl_conditioned_by_hot_set_membership():
    # trades : (symbol, pnl). hot_set ne contient que A.
    trades = [("A", 10.0), ("A", -3.0), ("B", 50.0)]
    res = membership_pnl(trades, hot_set={"A"})
    assert res["in_hot_set_pnl"] == 7.0       # 10 - 3
    assert res["out_hot_set_pnl"] == 50.0
    assert res["in_hot_set_trades"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rotation_bench.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write minimal implementation**

```python
# trader/rotation_bench.py
"""Bench de rotation : l'apport du hot-set se mesure au P&L 15m conditionné par
appartenance, pas au classement (cf D9 §11). Fonction pure ; reconstruction
depuis l'as_of journalisé dans rotation_ledger."""
from __future__ import annotations


def membership_pnl(trades: list[tuple[str, float]], *, hot_set: set[str]) -> dict:
    """Sépare le P&L des trades selon l'appartenance au hot-set du jour."""
    in_pnl = sum(p for s, p in trades if s in hot_set)
    out_pnl = sum(p for s, p in trades if s not in hot_set)
    in_n = sum(1 for s, _ in trades if s in hot_set)
    return {"in_hot_set_pnl": in_pnl, "out_hot_set_pnl": out_pnl,
            "in_hot_set_trades": in_n, "out_hot_set_trades": len(trades) - in_n}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_rotation_bench.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/rotation_bench.py tests/test_rotation_bench.py
git commit -m "feat(rotation): bench as-of — P&L 15m conditionné par le hot-set"
```

---

## Self-Review — couverture du spec

| Spec | Tâche(s) |
|---|---|
| Pool + exclusions dures (D9 §5, §8) | Task 4 |
| Score : efficacité × force relative × amplitude bornée, plancher ATR%, pas de `−vol` (D9 §5) | Task 8, 9 |
| Force relative par venue (pas SPY global) | Task 5 (benchmarks), 18 (`benchmark_ret_for`) |
| Séries ajustées (corporate actions) | Task 7 (`auto_adjust=True` en prod) |
| Code défaut + override agent tracé (D9 §6) | Task 11-12 (défaut), 15 (override), 16 (ledger) |
| Hystérésis + sortie d'urgence | Task 11, 12 |
| Sticky hors quota, AVANT écriture | Task 13, 17 (`run` calcule sticky avant `write`) |
| Cap M=25 sur slots libres ; `sticky_over_cap` | Task 13 |
| Écriture atomique de `universe.yaml` | Task 14 |
| Échec Codex daily → commit du défaut | Task 17 |
| Échec radar massif / couverture | Task 7 |
| Tilt validé, vide par défaut | Task 6 |
| Migration `starting_cash` (`daemon.py:1103` + `stats.py:114`) | Task 1-3 |
| `rotation_ledger` dédié (pas `decision-bench`) | Task 16 |
| Bench as-of (P&L conditionné hot-set) | Task 19 |
| Cadence EOD / CLI `--run` | Task 17, 18 |

**Restent à câbler en prod (Task 18, non purement testable unitairement) :** `as_of` par calendrier de venue ; `sticky_fn` réelle (broker + plans + watches + pending) ; `override_fn` via `codex_client` ; `fetch` `yf.download` ajusté + cache + backoff. Couverts par le smoke d'intégration + revue manuelle.

**Hors plan (réserve spec) :** sizing corrélation-aware ; lifecycle symbole complet (états delisted/halted) et pool mutable runtime — à ajouter en itération si besoin opérationnel (non bloquant pour un premier hot-set dynamique fonctionnel).
