# News-Feed Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Logger deux signaux d'actualité (`earnings_in_h`, `news_coverage`) au moment de chaque décision, sans influencer la décision en live.

**Architecture:** Nouveau module `trader/tools/news_feed.py` (provider Yahoo via `yfinance`, déjà dépendance). Une fonction publique `news_snapshot(symbol, *, now)` avec fetcher injectable (testable offline), cache TTL déterministe basé sur `now`, et garantie « ne lève jamais ». Câblé dans `daemon.py` au moment de `record_decision` (jamais dans le contexte LLM) et persisté en sous-objet `news` top-level de `decision_ledger.build_decision_row`.

**Tech Stack:** Python 3, `yfinance` (existant), `pytest`. Pas de nouvelle dépendance, pas de client HTTP custom (yfinance encapsule le réseau).

## Global Constraints

- `news_snapshot` ne propage **jamais** d'exception : tout échec → snapshot `coverage="error"`. Le cycle de décision ne doit jamais casser à cause du fil d'actu.
- Phase attribution : la donnée est **loggée uniquement**, jamais passée à `codex_client.decide_batch` ni à `_batch_decide`.
- Déterminisme : `asof` et le cache dérivent du `now` injecté (pas de `datetime.now()` caché). yfinance reste la seule source de non-déterminisme, isolée derrière le fetcher.
- `news_coverage` ∈ {`"ok"`, `"empty"`, `"unmapped"`, `"error"`} — toujours une de ces 4 valeurs.
- Conventions du repo : `from __future__ import annotations`, dataclasses frozen, type hints, docstrings en français (cf. `trader/tools/market.py`).

---

### Task 1 : Fonctions pures de calcul du snapshot

**Files:**
- Create: `trader/tools/news_feed.py`
- Test: `tests/test_news_feed.py`

**Interfaces:**
- Produces:
  - `RawNews` (frozen dataclass) : `mapped: bool`, `earnings_dates: tuple[datetime, ...]` (tz-aware UTC, passées ou futures), `news_count: int`.
  - `_next_future_earnings(now: datetime, dates) -> datetime | None`
  - `_earnings_in_h(now: datetime, next_dt: datetime | None) -> float | None`
  - `_classify_coverage(raw: RawNews) -> str`
  - `build_snapshot(symbol: str, *, now: datetime, raw: RawNews, source: str) -> dict`
  - Constantes : `COVERAGE_OK="ok"`, `COVERAGE_EMPTY="empty"`, `COVERAGE_UNMAPPED="unmapped"`, `COVERAGE_ERROR="error"`.

- [ ] **Step 1 : Écrire les tests qui échouent**

```python
# tests/test_news_feed.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trader.tools import news_feed as nf

UTC = timezone.utc
NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)


def test_next_future_earnings_picks_soonest_future():
    dates = (
        NOW - timedelta(days=1),       # passée → ignorée
        NOW + timedelta(hours=10),     # future la plus proche
        NOW + timedelta(days=5),
    )
    assert nf._next_future_earnings(NOW, dates) == NOW + timedelta(hours=10)


def test_next_future_earnings_none_when_all_past():
    dates = (NOW - timedelta(days=1), NOW - timedelta(hours=2))
    assert nf._next_future_earnings(NOW, dates) is None


def test_earnings_in_h_rounds_to_two_decimals():
    assert nf._earnings_in_h(NOW, NOW + timedelta(hours=10, minutes=30)) == 10.5


def test_earnings_in_h_none_when_no_date():
    assert nf._earnings_in_h(NOW, None) is None


def test_classify_coverage_unmapped_when_not_mapped():
    raw = nf.RawNews(mapped=False, earnings_dates=(), news_count=0)
    assert nf._classify_coverage(raw) == nf.COVERAGE_UNMAPPED


def test_classify_coverage_empty_when_mapped_no_news():
    raw = nf.RawNews(mapped=True, earnings_dates=(), news_count=0)
    assert nf._classify_coverage(raw) == nf.COVERAGE_EMPTY


def test_classify_coverage_ok_when_mapped_with_news():
    raw = nf.RawNews(mapped=True, earnings_dates=(), news_count=3)
    assert nf._classify_coverage(raw) == nf.COVERAGE_OK


def test_build_snapshot_shape():
    raw = nf.RawNews(
        mapped=True,
        earnings_dates=(NOW + timedelta(hours=24),),
        news_count=2,
    )
    snap = nf.build_snapshot("ACA.PA", now=NOW, raw=raw, source="yahoo")
    assert snap == {
        "earnings_in_h": 24.0,
        "news_coverage": "ok",
        "news_count": 2,
        "source": "yahoo",
        "asof": "2026-06-23T12:00:00+00:00",
    }
```

- [ ] **Step 2 : Lancer les tests, vérifier l'échec**

Run: `pytest tests/test_news_feed.py -v`
Expected: FAIL — `AttributeError: module 'trader.tools.news_feed' has no attribute ...` (le module n'existe pas encore).

- [ ] **Step 3 : Implémentation minimale**

```python
# trader/tools/news_feed.py
"""news_feed — ingestion du fil d'actu (phase attribution).

v1 : Yahoo via yfinance (déjà dépendance, couvre l'univers natif EU/TW/US).
Contrat : entrée minimale (symbole, now injecté), sortie machine-readable.
Aucune décision ici — la donnée est LOGGÉE, jamais vue par l'agent en live.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

COVERAGE_OK = "ok"
COVERAGE_EMPTY = "empty"
COVERAGE_UNMAPPED = "unmapped"
COVERAGE_ERROR = "error"

_NEWS_WINDOW_DAYS = 7


@dataclass(frozen=True)
class RawNews:
    """Données brutes extraites de la source, avant classification.

    `mapped` : la source connaît-elle le symbole (sinon → unmapped).
    `earnings_dates` : dates earnings tz-aware UTC, passées ou futures.
    `news_count` : nb de news dans la fenêtre récente (déjà filtré par le fetcher).
    """

    mapped: bool
    earnings_dates: tuple[datetime, ...]
    news_count: int


def _next_future_earnings(now: datetime, dates) -> datetime | None:
    future = [d for d in dates if d > now]
    return min(future) if future else None


def _earnings_in_h(now: datetime, next_dt: datetime | None) -> float | None:
    if next_dt is None:
        return None
    return round((next_dt - now).total_seconds() / 3600.0, 2)


def _classify_coverage(raw: RawNews) -> str:
    if not raw.mapped:
        return COVERAGE_UNMAPPED
    if raw.news_count > 0:
        return COVERAGE_OK
    return COVERAGE_EMPTY


def build_snapshot(symbol: str, *, now: datetime, raw: RawNews, source: str) -> dict:
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    next_earnings = _next_future_earnings(now_utc, raw.earnings_dates)
    return {
        "earnings_in_h": _earnings_in_h(now_utc, next_earnings),
        "news_coverage": _classify_coverage(raw),
        "news_count": raw.news_count,
        "source": source,
        "asof": now_utc.astimezone(timezone.utc).isoformat(),
    }
```

- [ ] **Step 4 : Lancer les tests, vérifier le succès**

Run: `pytest tests/test_news_feed.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5 : Commit**

```bash
git add trader/tools/news_feed.py tests/test_news_feed.py
git commit -m "feat(news-feed): fonctions pures de calcul du snapshot d'actu"
```

---

### Task 2 : `news_snapshot` — orchestration avec fetcher injectable, jamais d'exception

**Files:**
- Modify: `trader/tools/news_feed.py`
- Test: `tests/test_news_feed.py`

**Interfaces:**
- Consumes: `RawNews`, `build_snapshot`, constantes coverage (Task 1).
- Produces:
  - `Fetcher` = `Callable[[str], RawNews]` (signature : `fetcher(symbol, *, now) -> RawNews`).
  - `_error_snapshot(now: datetime) -> dict`
  - `news_snapshot(symbol: str, *, now: datetime, fetcher: Fetcher | None = None) -> dict`

- [ ] **Step 1 : Écrire les tests qui échouent**

```python
# tests/test_news_feed.py  (ajouter)

def _fake_fetcher(raw=None, exc=None):
    def _f(symbol, *, now):
        if exc is not None:
            raise exc
        return raw
    return _f


def test_news_snapshot_ok_path():
    raw = nf.RawNews(mapped=True, earnings_dates=(), news_count=1)
    snap = nf.news_snapshot("ACA.PA", now=NOW, fetcher=_fake_fetcher(raw=raw))
    assert snap["news_coverage"] == "ok"
    assert snap["source"] == "yahoo"


def test_news_snapshot_empty_distinct_from_unmapped():
    empty = nf.news_snapshot(
        "ACA.PA", now=NOW,
        fetcher=_fake_fetcher(raw=nf.RawNews(mapped=True, earnings_dates=(), news_count=0)),
    )
    unmapped = nf.news_snapshot(
        "ZZZZ.XX", now=NOW,
        fetcher=_fake_fetcher(raw=nf.RawNews(mapped=False, earnings_dates=(), news_count=0)),
    )
    assert empty["news_coverage"] == "empty"
    assert unmapped["news_coverage"] == "unmapped"


def test_news_snapshot_never_raises_on_fetcher_error():
    snap = nf.news_snapshot(
        "ACA.PA", now=NOW, fetcher=_fake_fetcher(exc=RuntimeError("yahoo down")),
    )
    assert snap["news_coverage"] == "error"
    assert snap["source"] == "none"
    assert snap["news_count"] == 0
    assert snap["earnings_in_h"] is None
    assert snap["asof"] == "2026-06-23T12:00:00+00:00"
```

- [ ] **Step 2 : Lancer les tests, vérifier l'échec**

Run: `pytest tests/test_news_feed.py -k "news_snapshot" -v`
Expected: FAIL — `AttributeError: ... has no attribute 'news_snapshot'`.

- [ ] **Step 3 : Implémentation minimale**

Ajouter en tête de fichier l'import :

```python
from typing import Callable
```

Ajouter après `build_snapshot` :

```python
Fetcher = Callable[..., RawNews]


def _error_snapshot(now: datetime) -> dict:
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return {
        "earnings_in_h": None,
        "news_coverage": COVERAGE_ERROR,
        "news_count": 0,
        "source": "none",
        "asof": now_utc.astimezone(timezone.utc).isoformat(),
    }


def news_snapshot(
    symbol: str, *, now: datetime, fetcher: Fetcher | None = None
) -> dict:
    """Snapshot d'actu pour un symbole. NE LÈVE JAMAIS : tout échec → coverage=error.

    `fetcher` injectable pour les tests ; défaut = Yahoo via yfinance (Task 4).
    """
    if fetcher is None:
        fetcher = _yahoo_fetch
    try:
        raw = fetcher(symbol, now=now)
        return build_snapshot(symbol, now=now, raw=raw, source="yahoo")
    except Exception:
        return _error_snapshot(now)
```

Note : `_yahoo_fetch` n'existe pas encore → un appel sans `fetcher` tombera dans le `except` et renverra `error`. C'est volontaire et sûr ; Task 4 fournit le vrai fetcher. Les tests de cette tâche injectent toujours `fetcher`.

- [ ] **Step 4 : Lancer les tests, vérifier le succès**

Run: `pytest tests/test_news_feed.py -v`
Expected: PASS (tous, y compris Task 1).

- [ ] **Step 5 : Commit**

```bash
git add trader/tools/news_feed.py tests/test_news_feed.py
git commit -m "feat(news-feed): news_snapshot orchestration, garantie no-raise"
```

---

### Task 3 : Cache TTL déterministe basé sur `now`

**Files:**
- Modify: `trader/tools/news_feed.py`
- Test: `tests/test_news_feed.py`

**Interfaces:**
- Consumes: `news_snapshot` (Task 2).
- Produces:
  - `_CACHE: dict[str, tuple[datetime, dict]]` (module-level), `CACHE_TTL_MINUTES = 30.0`.
  - `reset_cache() -> None` (pour les tests).
  - `news_snapshot` gagne le comportement de cache (succès uniquement ; les snapshots `error` ne sont PAS cachés → retry au cycle suivant).

Décision : un seul TTL (30 min) couvre tout le snapshot. Refetcher les earnings toutes les 30 min est négligeable et plus simple qu'un cache à deux niveaux (YAGNI vs le spec qui suggérait news 30 min / earnings 1×/jour — simplification assumée, dans l'esprit du spec : plus frais = plus sûr).

- [ ] **Step 1 : Écrire les tests qui échouent**

```python
# tests/test_news_feed.py  (ajouter)

from datetime import timedelta as _td  # si pas déjà importé via timedelta


class _CountingFetcher:
    def __init__(self, raw):
        self.raw = raw
        self.calls = 0

    def __call__(self, symbol, *, now):
        self.calls += 1
        return self.raw


def test_cache_hit_within_ttl_does_not_refetch():
    nf.reset_cache()
    fetcher = _CountingFetcher(nf.RawNews(mapped=True, earnings_dates=(), news_count=1))
    nf.news_snapshot("ACA.PA", now=NOW, fetcher=fetcher)
    nf.news_snapshot("ACA.PA", now=NOW + timedelta(minutes=10), fetcher=fetcher)
    assert fetcher.calls == 1


def test_cache_expires_after_ttl():
    nf.reset_cache()
    fetcher = _CountingFetcher(nf.RawNews(mapped=True, earnings_dates=(), news_count=1))
    nf.news_snapshot("ACA.PA", now=NOW, fetcher=fetcher)
    nf.news_snapshot("ACA.PA", now=NOW + timedelta(minutes=31), fetcher=fetcher)
    assert fetcher.calls == 2


def test_error_snapshot_not_cached():
    nf.reset_cache()
    calls = {"n": 0}

    def flaky(symbol, *, now):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("down")
        return nf.RawNews(mapped=True, earnings_dates=(), news_count=2)

    first = nf.news_snapshot("ACA.PA", now=NOW, fetcher=flaky)
    second = nf.news_snapshot("ACA.PA", now=NOW + timedelta(minutes=1), fetcher=flaky)
    assert first["news_coverage"] == "error"
    assert second["news_coverage"] == "ok"  # pas servi depuis le cache
    assert calls["n"] == 2
```

- [ ] **Step 2 : Lancer les tests, vérifier l'échec**

Run: `pytest tests/test_news_feed.py -k "cache or not_cached" -v`
Expected: FAIL — `AttributeError: ... has no attribute 'reset_cache'`.

- [ ] **Step 3 : Implémentation minimale**

Ajouter après les constantes coverage :

```python
CACHE_TTL_MINUTES = 30.0
_CACHE: dict[str, tuple[datetime, dict]] = {}


def reset_cache() -> None:
    _CACHE.clear()
```

Remplacer le corps de `news_snapshot` par :

```python
def news_snapshot(
    symbol: str, *, now: datetime, fetcher: Fetcher | None = None
) -> dict:
    """Snapshot d'actu pour un symbole. NE LÈVE JAMAIS : tout échec → coverage=error.

    Cache TTL (CACHE_TTL_MINUTES) déterministe basé sur `now` ; seuls les
    snapshots réussis sont cachés (un `error` est retenté au cycle suivant).
    `fetcher` injectable pour les tests ; défaut = Yahoo via yfinance (Task 4).
    """
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    cached = _CACHE.get(symbol)
    if cached is not None:
        cached_now, snap = cached
        if (now_utc - cached_now).total_seconds() < CACHE_TTL_MINUTES * 60.0:
            return snap
    if fetcher is None:
        fetcher = _yahoo_fetch
    try:
        raw = fetcher(symbol, now=now_utc)
        snap = build_snapshot(symbol, now=now_utc, raw=raw, source="yahoo")
    except Exception:
        return _error_snapshot(now_utc)
    _CACHE[symbol] = (now_utc, snap)
    return snap
```

- [ ] **Step 4 : Lancer les tests, vérifier le succès**

Run: `pytest tests/test_news_feed.py -v`
Expected: PASS (tous).

- [ ] **Step 5 : Commit**

```bash
git add trader/tools/news_feed.py tests/test_news_feed.py
git commit -m "feat(news-feed): cache TTL déterministe (now-based), erreurs non cachées"
```

---

### Task 4 : Fetcher Yahoo réel (`_yahoo_fetch`) — frontière impure

**Files:**
- Modify: `trader/tools/news_feed.py`
- Test: `tests/test_news_feed.py`

**Interfaces:**
- Consumes: `RawNews` (Task 1).
- Produces: `_yahoo_fetch(symbol: str, *, now: datetime) -> RawNews` (utilise `yfinance`).

Sémantique : `mapped` = vrai si yfinance expose un dernier prix (`fast_info`). `earnings_dates` extraites de `get_earnings_dates`. `news_count` = news dans la fenêtre `_NEWS_WINDOW_DAYS`. Si AUCUN des trois appels ne réussit (réseau mort / symbole introuvable lève partout) → propager pour que `news_snapshot` retombe en `error`.

- [ ] **Step 1 : Écrire le test qui échoue (yfinance mocké)**

```python
# tests/test_news_feed.py  (ajouter)
import sys
import types


def _install_fake_yfinance(monkeypatch, *, last_price, earnings_index, news_items):
    fake = types.ModuleType("yfinance")

    class _FastInfo(dict):
        pass

    class _Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        @property
        def fast_info(self):
            return _FastInfo({"last_price": last_price})

        def get_earnings_dates(self, limit=12):
            class _DF:
                def __init__(self, idx):
                    self.index = idx
            return _DF(earnings_index)

        @property
        def news(self):
            return news_items

    fake.Ticker = _Ticker
    monkeypatch.setitem(sys.modules, "yfinance", fake)


def test_yahoo_fetch_maps_and_counts(monkeypatch):
    import pandas as pd
    idx = [pd.Timestamp("2026-06-24T08:00:00Z")]
    news = [
        {"providerPublishTime": int((NOW - timedelta(days=1)).timestamp())},  # dans la fenêtre
        {"providerPublishTime": int((NOW - timedelta(days=30)).timestamp())},  # hors fenêtre
    ]
    _install_fake_yfinance(monkeypatch, last_price=12.3, earnings_index=idx, news=news)
    raw = nf._yahoo_fetch("ACA.PA", now=NOW)
    assert raw.mapped is True
    assert raw.news_count == 1
    assert len(raw.earnings_dates) == 1


def test_yahoo_fetch_unmapped_when_no_price(monkeypatch):
    _install_fake_yfinance(monkeypatch, last_price=None, earnings_index=[], news=[])
    raw = nf._yahoo_fetch("ZZZZ.XX", now=NOW)
    assert raw.mapped is False
```

- [ ] **Step 2 : Lancer les tests, vérifier l'échec**

Run: `pytest tests/test_news_feed.py -k "yahoo_fetch" -v`
Expected: FAIL — `AttributeError: ... has no attribute '_yahoo_fetch'`.

- [ ] **Step 3 : Implémentation minimale**

Ajouter en fin de fichier :

```python
from datetime import timedelta


def _yahoo_fetch(symbol: str, *, now: datetime) -> RawNews:
    """Frontière impure : interroge Yahoo via yfinance.

    Best-effort par champ : un champ qui échoue n'invalide pas les autres.
    Propage seulement si AUCUN champ ne répond (→ news_snapshot renverra error).
    """
    import yfinance as yf

    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    tk = yf.Ticker(symbol)
    any_ok = False

    mapped = False
    try:
        last = tk.fast_info.get("last_price")
        mapped = last is not None
        any_ok = True
    except Exception:
        pass

    earnings: list[datetime] = []
    try:
        df = tk.get_earnings_dates(limit=12)
        any_ok = True
        if df is not None and getattr(df, "index", None) is not None:
            for ts in df.index:
                dt = ts.to_pydatetime()
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                earnings.append(dt.astimezone(timezone.utc))
    except Exception:
        pass

    news_count = 0
    try:
        items = tk.news or []
        any_ok = True
        cutoff = now_utc - timedelta(days=_NEWS_WINDOW_DAYS)
        for item in items:
            ts_epoch = item.get("providerPublishTime")
            if ts_epoch is None:
                news_count += 1  # pas de timestamp → on compte par prudence
                continue
            published = datetime.fromtimestamp(ts_epoch, tz=timezone.utc)
            if published >= cutoff:
                news_count += 1
    except Exception:
        pass

    if not any_ok:
        raise RuntimeError(f"yahoo unreachable for {symbol}")
    return RawNews(mapped=mapped, earnings_dates=tuple(earnings), news_count=news_count)
```

- [ ] **Step 4 : Lancer les tests, vérifier le succès**

Run: `pytest tests/test_news_feed.py -v`
Expected: PASS (tous).

- [ ] **Step 5 : Smoke réseau optionnel (manuel, non bloquant)**

Run (réseau requis, peut être ignoré en CI) :
```bash
python -c "from datetime import datetime,timezone; from trader.tools import news_feed as nf; print(nf.news_snapshot('ACA.PA', now=datetime.now(timezone.utc)))"
```
Expected : un dict avec `news_coverage` dans {ok,empty,unmapped,error}. Si Yahoo répond, `source="yahoo"`.

- [ ] **Step 6 : Commit**

```bash
git add trader/tools/news_feed.py tests/test_news_feed.py
git commit -m "feat(news-feed): fetcher Yahoo réel via yfinance (frontière impure)"
```

---

### Task 5 : Persister `news` dans la decision row

**Files:**
- Modify: `trader/decision_ledger.py:95-147` (dict retourné par `build_decision_row`)
- Test: `tests/test_decision_ledger.py` (créer si absent)

**Interfaces:**
- Consumes: `decision.get("news")` (dict produit par `news_snapshot`).
- Produces: `row["news"]` — sous-objet top-level (pas dans `runtime`). `{}` si absent.

- [ ] **Step 1 : Écrire le test qui échoue**

```python
# tests/test_decision_ledger.py
from __future__ import annotations

from trader import decision_ledger


def _report():
    return {"ts": "2026-06-23T12:00:00+00:00", "prices": {"ACA.PA": 12.3}}


def test_build_decision_row_includes_news_when_present():
    news = {"earnings_in_h": 24.0, "news_coverage": "ok",
            "news_count": 2, "source": "yahoo", "asof": "2026-06-23T12:00:00+00:00"}
    decision = {"symbol": "ACA.PA", "action": "HOLD", "news": news}
    row = decision_ledger.build_decision_row(_report(), decision, sequence=0)
    assert row["news"] == news


def test_build_decision_row_news_defaults_to_empty_dict():
    decision = {"symbol": "ACA.PA", "action": "HOLD"}
    row = decision_ledger.build_decision_row(_report(), decision, sequence=0)
    assert row["news"] == {}
```

- [ ] **Step 2 : Lancer les tests, vérifier l'échec**

Run: `pytest tests/test_decision_ledger.py -v`
Expected: FAIL — `KeyError: 'news'`.

- [ ] **Step 3 : Implémentation minimale**

Dans `trader/decision_ledger.py`, fonction `build_decision_row`, ajouter une entrée juste avant `"labels": {},` (ligne ~146) :

```python
        "news": _as_dict(decision.get("news")),
        "labels": {},
```

- [ ] **Step 4 : Lancer les tests, vérifier le succès**

Run: `pytest tests/test_decision_ledger.py -v`
Expected: PASS.

- [ ] **Step 5 : Commit**

```bash
git add trader/decision_ledger.py tests/test_decision_ledger.py
git commit -m "feat(news-feed): persiste le sous-objet news dans la decision row"
```

---

### Task 6 : Câbler `news_snapshot` dans le daemon (attribution, hors contexte LLM)

**Files:**
- Modify: `trader/daemon.py` (import + injection dans `entry`, ~ligne 2060)
- Test: `tests/test_news_feed_wiring.py`

**Interfaces:**
- Consumes: `news_feed.news_snapshot` (Task 3), `entry` dict daemon (~ligne 2040-2060).
- Produces: `entry["news"]` peuplé avant `record_decision`, donc avant `build_decision_row`. Aucun passage au contexte LLM.

- [ ] **Step 1 : Vérifier le style d'import existant**

Run: `grep -n "from .tools import" trader/daemon.py | head`
Expected : une ligne du type `from .tools import market` (ou `from trader.tools import ...`). Reproduire EXACTEMENT ce style pour ajouter `news_feed`.

- [ ] **Step 2 : Écrire le test de câblage qui échoue**

Le test vérifie l'invariant clé sans exécuter tout `run_cycle` : `record_decision` enrichit `entry["news"]` puis appelle `build_decision_row`, et la news n'est jamais ajoutée au `per_symbol` du contexte LLM. On teste via le chemin réel `entry → build_decision_row` que la forme attendue est respectée, et que `news_snapshot` est tolérant.

```python
# tests/test_news_feed_wiring.py
from __future__ import annotations

from datetime import datetime, timezone

from trader import decision_ledger
from trader.tools import news_feed as nf

NOW = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)


def test_entry_with_snapshot_flows_into_row():
    nf.reset_cache()
    raw = nf.RawNews(mapped=True, earnings_dates=(), news_count=0)
    snap = nf.news_snapshot("ACA.PA", now=NOW, fetcher=lambda s, *, now: raw)
    entry = {"symbol": "ACA.PA", "action": "HOLD", "news": snap}
    report = {"ts": "2026-06-23T12:00:00+00:00", "prices": {"ACA.PA": 12.3}}
    row = decision_ledger.build_decision_row(report, entry, sequence=0)
    assert row["news"]["news_coverage"] == "empty"
    assert row["news"]["source"] == "yahoo"
```

- [ ] **Step 3 : Lancer le test, vérifier le succès attendu après câblage**

Run: `pytest tests/test_news_feed_wiring.py -v`
Expected: PASS dès maintenant (valide la chaîne données ; le câblage daemon ci-dessous branche la source réelle).

- [ ] **Step 4 : Câbler dans `daemon.py`**

Ajouter l'import en tête (selon le style relevé Step 1), p.ex. :
```python
from .tools import news_feed
```
Puis, juste après la construction du dict `entry` (après `"data_source": runtime_data_source_by_sym.get(sym)}`, ~ligne 2060) :
```python
        # Phase attribution : on LOGGE l'actu, on ne la passe PAS au LLM.
        entry["news"] = news_feed.news_snapshot(sym, now=now)
```

- [ ] **Step 5 : Vérifier l'absence de fuite vers le contexte LLM**

Run: `grep -n "news_snapshot\|\[\"news\"\]\|'news'" trader/daemon.py`
Expected : la seule écriture `entry["news"] = ...` se situe dans le bloc de construction de `entry` (record_decision), PAS dans `_batch_decide` / `per_symbol` (lignes ~1109-1136). Confirmer visuellement qu'aucune des clés du `per_symbol` passé à `codex_client.decide_batch` ne contient la news.

- [ ] **Step 6 : Lancer la suite complète (non-régression)**

Run: `pytest tests/test_news_feed.py tests/test_decision_ledger.py tests/test_news_feed_wiring.py -v && pytest -q`
Expected: tous verts (la suite globale ne régresse pas).

- [ ] **Step 7 : Commit**

```bash
git add trader/daemon.py tests/test_news_feed_wiring.py
git commit -m "feat(news-feed): câble news_snapshot en attribution (hors contexte LLM)"
```

---

## Self-Review

**Spec coverage :**
- Source Yahoo primaire / Finnhub dormant → Task 4 (yfinance), Finnhub non câblé (conforme « dormant »).
- Module `news_feed.py` + contrat `news_snapshot` → Tasks 1-4.
- `earnings_in_h`, `news_coverage` (ok/empty/unmapped/error) → Tasks 1-2.
- Garde-fou anti faux-négatif (empty ≠ unmapped ≠ error) → `test_news_snapshot_empty_distinct_from_unmapped`, `test_error_snapshot_not_cached`.
- Safe default « ne lève jamais » → `test_news_snapshot_never_raises_on_fetcher_error`.
- Cache TTL → Task 3.
- Logging top-level `news`, hors `runtime` → Task 5.
- Câblage hors contexte LLM → Task 6 (Steps 4-5).
- Phase 2 (veto) / sentiment / EODHD → hors scope, non planifiés. ✓

**Placeholder scan :** aucun TBD/TODO ; chaque step de code montre le code complet.

**Type consistency :** `RawNews(mapped, earnings_dates, news_count)`, `news_snapshot(symbol, *, now, fetcher=None)`, `build_snapshot(symbol, *, now, raw, source)`, coverage constants — noms identiques dans toutes les tâches. `row["news"]` cohérent Tasks 5-6.

Note d'écart assumé vs spec : TTL unique 30 min (au lieu de news 30 min / earnings 1×/jour) — simplification YAGNI documentée Task 3, dans l'esprit du spec.
