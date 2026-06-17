# Rotation swing-aware + réactivation du sélecteur LLM — Plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Faire entrer les marchés fermés daily-valides dans l'univers de surveillance via une fenêtre pré-open (Phase A, déterministe), puis réactiver le sélecteur LLM sticky/contexte-aware sur le chemin vivant D10 avec défaut déterministe préservé (Phase B).

**Architecture:** `universe.yaml` est réétiqueté « univers de surveillance active ». Phase A élargit `open_venues` → `analyzable_venues = open ∪ preopen` dans le composeur d'univers (`rotation_venues.tick`), réutilisant les hotlists déjà persistées dans `venue_state.json` à la clôture ; l'exécution reste gatée au runtime par briques 2/3 (inchangées). Phase B branche un override LLM pré-open par venue (`apply_override`, fail-safe sur le défaut, prompt enrichi sticky + régime), avec `rotation_ledger` qui mesure défaut-vs-final.

**Tech Stack:** Python, pytest (fonctions pures, temps passé en `now_iso` UTC), `exchange_calendars` (§13.8), modules `trader/rotation_*`.

**Spec:** `docs/superpowers/specs/2026-06-17-rotation-swing-aware-design.md` · **Décision:** D13.

**Convention de ce repo (à respecter par l'implémenteur):** temps TOUJOURS passé en argument ISO 8601 UTC (jamais `datetime.now()` interne) ; fonctions de rotation pures et testées unitairement ; commits fréquents par tâche ; `make test` ou `pytest` doit rester vert. Lance les tests avec `pytest tests/<fichier> -v`.

---

## Phase A — Appartenance swing-aware (déterministe, shippable seule)

### Task A1: `preopen_venues` — venues dont la prochaine ouverture est imminente

**Files:**
- Modify: `trader/rotation_schedule.py` (ajouter après `open_venues`, ~ligne 68)
- Test: `tests/test_rotation_schedule.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rotation_schedule.py
from trader.rotation_schedule import preopen_venues

_SESS = {
    "TW": {"open": "01:00", "close": "05:30"},
    "EU": {"open": "07:00", "close": "15:30"},
    "US": {"open": "13:30", "close": "20:00"},
}

def test_preopen_inclut_venue_dans_la_fenetre():
    # 00:30 UTC un mardi, fenêtre 90 min → TW (ouvre 01:00) est en pré-open
    assert preopen_venues("2026-06-16T00:30:00+00:00", _SESS, window_minutes=90) == ["TW"]

def test_preopen_exclut_venue_hors_fenetre():
    # 23:00 UTC → prochaine ouverture TW (01:00 lendemain) à >90 min → vide
    assert preopen_venues("2026-06-16T23:00:00+00:00", _SESS, window_minutes=90) == []

def test_preopen_exclut_venue_deja_ouverte():
    # 02:00 UTC → TW déjà ouverte (pas pré-open), EU/US trop loin
    assert preopen_venues("2026-06-16T02:00:00+00:00", _SESS, window_minutes=90) == []

def test_preopen_weekend_pas_de_preopen_actions():
    # samedi → aucune venue actions en pré-open (le lundi est à >fenêtre)
    assert preopen_venues("2026-06-20T00:30:00+00:00", _SESS, window_minutes=90) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_rotation_schedule.py -k preopen -v`
Expected: FAIL `ImportError: cannot import name 'preopen_venues'`

- [ ] **Step 3: Write minimal implementation**

```python
# trader/rotation_schedule.py — après open_venues()
def preopen_venues(
    now_iso: str, sessions: dict[str, SessionHours], *, window_minutes: int = 90
) -> list[str]:
    """Venues actions dont la PROCHAINE ouverture est dans (now, now+window].

    Pré-open = analysable pour préparer le gong, PAS exécutable (brique 3 gate).
    N'inclut jamais FX (24/5, pas de gong). Une venue déjà ouverte n'est pas pré-open.
    Calendrier v1 = HH:MM + jour ouvré, cohérent avec open_venues (cf Task A4 pour
    l'alignement exchange_calendars).
    """
    now = datetime.fromisoformat(now_iso).astimezone(timezone.utc)
    horizon = now + timedelta(minutes=window_minutes)
    open_now = set(open_venues(now_iso, sessions))
    hits: list[str] = []
    for venue, hours in sessions.items():
        if venue in open_now:
            continue
        # prochaine ouverture : aujourd'hui si pas encore passée, sinon prochain jour ouvré
        candidate = _close_dt(now, hours["open"])
        for _ in range(4):  # aujourd'hui + jusqu'à 3 jours (saute le week-end)
            if candidate > now and candidate.weekday() < 5:
                break
            candidate = _close_dt(candidate + timedelta(days=1), hours["open"])
        if now < candidate <= horizon:
            hits.append(venue)
    return sorted(hits)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_rotation_schedule.py -k preopen -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add trader/rotation_schedule.py tests/test_rotation_schedule.py
git commit -m "feat(rotation): preopen_venues — venues dont l'ouverture est imminente (§A1, D13)"
```

---

### Task A2: `analyzable_venues` — union ouvertes ∪ pré-open

**Files:**
- Modify: `trader/rotation_schedule.py`
- Test: `tests/test_rotation_schedule.py`

- [ ] **Step 1: Write the failing test**

```python
from trader.rotation_schedule import analyzable_venues

def test_analyzable_union_ouvertes_et_preopen():
    # 00:30 UTC mardi : FX ouvert (24/5) + TW en pré-open
    assert analyzable_venues("2026-06-16T00:30:00+00:00", _SESS, preopen_window_minutes=90) == ["FX", "TW"]

def test_analyzable_egal_open_quand_aucun_preopen():
    # 02:00 UTC : TW + FX ouverts, aucun pré-open
    assert analyzable_venues("2026-06-16T02:00:00+00:00", _SESS, preopen_window_minutes=90) == ["FX", "TW"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_rotation_schedule.py -k analyzable -v`
Expected: FAIL `ImportError: cannot import name 'analyzable_venues'`

- [ ] **Step 3: Write minimal implementation**

```python
# trader/rotation_schedule.py
def analyzable_venues(
    now_iso: str, sessions: dict[str, SessionHours], *, preopen_window_minutes: int = 90
) -> list[str]:
    """Venues à inclure dans l'univers de SURVEILLANCE = ouvertes ∪ pré-open.

    L'exécutabilité reste décidée au runtime par classify_symbol_context (brique 2/3) ;
    cette union ne dit que « surveille/analyse », jamais « trade ».
    """
    union = set(open_venues(now_iso, sessions))
    union |= set(preopen_venues(now_iso, sessions, window_minutes=preopen_window_minutes))
    return sorted(union)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_rotation_schedule.py -k analyzable -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/rotation_schedule.py tests/test_rotation_schedule.py
git commit -m "feat(rotation): analyzable_venues = ouvertes ∪ pré-open (§A2, D13)"
```

---

### Task A3: brancher `analyzable_venues` dans `tick` + config `preopen_window_minutes`

**Files:**
- Modify: `trader/rotation_venues.py:212` (le `open_venues(...)` qui alimente `compose_active_universe`)
- Modify: `trader/radar_config.py` (ajouter le param `preopen_window_minutes`, défaut 90)
- Modify: `config/radar.yaml` (documenter le nouveau champ)
- Test: `tests/test_rotation_venues.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rotation_venues.py — un test d'intégration de tick() qui vérifie qu'une
# venue FERMÉE mais en PRÉ-OPEN voit sa hotlist persistée entrer dans l'univers écrit.
def test_tick_preopen_admet_la_hotlist_de_la_venue_fermee(tmp_path):
    import json, yaml
    from trader.rotation_venues import tick
    state_dir = tmp_path / "state"; state_dir.mkdir()
    config_dir = tmp_path
    (config_dir / "config").mkdir()
    (config_dir / "config" / "sessions.yaml").write_text(yaml.safe_dump({
        "TW": {"open": "01:00", "close": "05:30"},
        "EU": {"open": "07:00", "close": "15:30"},
        "US": {"open": "13:30", "close": "20:00"},
    }))
    (config_dir / "config" / "universe.yaml").write_text(yaml.safe_dump({"symbols": ["SEED"]}))
    (config_dir / "config" / "radar.yaml").write_text(yaml.safe_dump({"cap_m": 25, "preopen_window_minutes": 90}))
    # venue TW déjà classée à sa dernière clôture (hotlist persistée), last_close_at récent
    (state_dir / "venue_state.json").write_text(json.dumps({
        "venues": {"TW": {"hotlist": ["2330.TW", "2317.TW"], "last_close_at": "2026-06-15T05:30:00+00:00", "dwell": {}}}
    }))
    # 00:30 UTC mardi : TW fermée mais en pré-open (ouvre 01:00) → ses symboles entrent
    res = tick(str(config_dir), str(state_dir), "2026-06-16T00:30:00+00:00",
               rank_fn=lambda: {"ranked": [], "gap_adverse": frozenset()}, sticky_fn=lambda: set())
    written = yaml.safe_load((config_dir / "config" / "universe.yaml").read_text())["symbols"]
    assert "2330.TW" in written and "2317.TW" in written
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_rotation_venues.py -k preopen -v`
Expected: FAIL (les `.TW` absents — `tick` utilise encore `open_venues`, TW fermée à 00:30)

- [ ] **Step 3: Write minimal implementation**

```python
# trader/rotation_venues.py — remplacer ligne ~212
#   open_v = open_venues(now_iso, sessions)
# par :
    open_v = analyzable_venues(
        now_iso, sessions, preopen_window_minutes=params.preopen_window_minutes
    )
# (importer analyzable_venues depuis rotation_schedule en tête de fichier)
```

```python
# trader/radar_config.py — ajouter au dataclass de params + au loader
preopen_window_minutes: int = 90
# et le lire depuis radar.yaml (clé "preopen_window_minutes", défaut 90)
```

```yaml
# config/radar.yaml — ajouter
preopen_window_minutes: 90  # fenêtre (min) où une venue fermée entre en surveillance avant son ouverture (D13)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_rotation_venues.py -k preopen -v && pytest tests/test_radar_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add trader/rotation_venues.py trader/radar_config.py config/radar.yaml tests/
git commit -m "feat(rotation): tick admet les venues en pré-open dans l'univers de surveillance (§A3, D13)"
```

---

### Task A4: garde anti-churn — transition preopen→open ne purge rien

**Files:**
- Test: `tests/test_rotation_venues.py` (test d'invariant, pas de code prod si A3 suffit)

- [ ] **Step 1: Write the failing/guarding test**

```python
def test_preopen_vers_open_sans_churn(tmp_path):
    # Même setup que A3. À 00:30 (pré-open) puis 01:30 (ouvert), les .TW restent présents :
    # l'ensemble écrit ne doit pas RETIRER les .TW à l'ouverture (zéro purge).
    # ... (réutiliser le harnais de A3 ; appeler tick à 00:30 puis 01:30) ...
    syms_preopen = set(_run_tick("2026-06-16T00:30:00+00:00"))
    syms_open = set(_run_tick("2026-06-16T01:30:00+00:00"))
    assert {"2330.TW", "2317.TW"} <= syms_preopen
    assert {"2330.TW", "2317.TW"} <= syms_open  # toujours là → pas de churn reconcile
```

- [ ] **Step 2: Run, verify PASS** (A3 le garantit déjà ; ce test verrouille l'invariant)

Run: `pytest tests/test_rotation_venues.py -k churn -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_rotation_venues.py
git commit -m "test(rotation): invariant zéro-churn preopen→open (§A4, D13)"
```

> **Note de scope (garde-fou #4 spec) :** `preopen_venues` et `open_venues` utilisent le calendrier HH:MM (jour ouvré), PAS encore `exchange_calendars`. Limitation pré-existante de `open_venues` (qui marquerait une venue « ouverte » un jour férié) ; safe car l'exécution est gatée par `session_snapshot`/`exchange_calendars` (brique 3). Aligner les DEUX (`open_venues` + `preopen_venues`) sur `exchange_calendars` est un chantier séparé tracé (sinon divergence : open « ouvert » / preopen « pas de prochaine ouverture »). Hors Phase A.

---

### ⛳ Checkpoint Phase A

Run: `pytest tests/ -q` (suite complète verte) ; vérifier sur un run réel que `tick` à T-90min d'une ouverture écrit bien les symboles de la venue fermée. **Phase A est shippable seule** — corrige le mismatch observable (incident Taïwan) sans toucher au LLM.

---

## Phase B — Réactivation du sélecteur LLM (défaut déterministe + override tracé)

> **Pré-requis de lecture pour l'implémenteur** (lire AVANT de coder B) :
> `trader/rotation_override.py` (`build_override_prompt:17`, `make_llm_override_fn:98`),
> `trader/rotation.py` (`apply_override:121`, payload `{ranked, default_hot}:320`),
> `trader/rotation_wiring.py` (`build_llm_override_fn:244`, `run_cli:280`),
> `trader/rotation_ledger.py` (format défaut-vs-final),
> `trader/rotation_collectors.py` (`sticky_collector:21`),
> `trader/family_regime.py` (régime sectoriel D2, pour le contexte du prompt).

### Task B1: le prompt du sélecteur reçoit les sticky

**Files:**
- Modify: `trader/rotation_override.py` (`build_override_prompt`)
- Test: `tests/test_rotation_override.py`

- [ ] **Step 1: Write the failing test**

```python
from trader.rotation_override import build_override_prompt

def test_prompt_liste_les_sticky_comme_non_retirables():
    prompt = build_override_prompt(
        ranked=[{"symbol": "AAA", "attractiveness": 0.9}, {"symbol": "BBB", "attractiveness": 0.5}],
        default_hot=["AAA"],
        sticky={"ZZZ"},
    )
    assert "ZZZ" in prompt
    # le prompt doit dire explicitement que les sticky ne sont pas retirables
    assert "sticky" in prompt.lower() or "non retirable" in prompt.lower()
```

- [ ] **Step 2: Run, verify it fails**

Run: `pytest tests/test_rotation_override.py -k sticky -v`
Expected: FAIL (signature actuelle sans `sticky`)

- [ ] **Step 3: Implement** — ajouter le paramètre `sticky: set[str] = frozenset()` à `build_override_prompt`, et une section dans le prompt FR listant les sticky avec la consigne « ces symboles sont déjà surveillés/positionnés, tu ne peux pas les retirer ; tiens-en compte pour ne pas dupliquer ». Garder la sortie JSON `{add, remove}` inchangée.

- [ ] **Step 4: Run, verify PASS**

Run: `pytest tests/test_rotation_override.py -v`
Expected: PASS (anciens tests + nouveau)

- [ ] **Step 5: Commit**

```bash
git add trader/rotation_override.py tests/test_rotation_override.py
git commit -m "feat(rotation): le prompt du sélecteur LLM voit les sticky (fin du choix en aveugle, §B1, D13)"
```

---

### Task B2: contexte régime sectoriel dans le prompt

**Files:**
- Modify: `trader/rotation_override.py` (`build_override_prompt`, `make_llm_override_fn`)
- Test: `tests/test_rotation_override.py`

- [ ] **Step 1: Write the failing test**

```python
def test_prompt_inclut_le_regime_sectoriel():
    prompt = build_override_prompt(
        ranked=[{"symbol": "AAA", "attractiveness": 0.9}],
        default_hot=["AAA"], sticky=set(),
        market_context={"regime_families": {"defense": {"sens": "up", "force": 0.8}}},
    )
    assert "defense" in prompt and ("up" in prompt.lower() or "haussier" in prompt.lower())
```

- [ ] **Step 2: Run, verify it fails** — Run: `pytest tests/test_rotation_override.py -k regime -v` → FAIL.

- [ ] **Step 3: Implement** — ajouter `market_context: dict | None = None` à `build_override_prompt` ; sérialiser une section « contexte de marché » (régime par famille) en texte court. `make_llm_override_fn` lit `payload.get("market_context")` et le transmet. Contrat narrow : v1 ne consomme que `regime_families` (extensible).

- [ ] **Step 4: Run, verify PASS** — `pytest tests/test_rotation_override.py -v`.

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(rotation): contexte régime sectoriel dans le prompt du sélecteur (§B2, D13)"
```

---

### Task B3: hook override pré-open par venue dans `tick` (défaut + fail-safe + ledger)

**Files:**
- Modify: `trader/rotation_venues.py` (`tick` — appliquer l'override sur la hotlist d'une venue qui ENTRE en pré-open)
- Modify: `trader/rotation_wiring.py` si besoin (exposer `build_llm_override_fn` + collecte du `market_context`)
- Test: `tests/test_rotation_venues.py`

- [ ] **Step 1: Write the failing test**

```python
def test_tick_override_preopen_ajuste_la_hotlist_et_failsafe(tmp_path):
    # venue TW en pré-open avec défaut ["2330.TW"] ; override_fn injecté ajoute "2454.TW".
    # 1) override OK → final contient l'ajout. 2) override qui lève → final = défaut (fail-safe).
    # injecter override_fn via paramètre de tick (à ajouter) ou via build.
    ...
    # cas fail-safe :
    def boom(_payload): raise RuntimeError("llm down")
    syms = _run_tick_with_override(boom)
    assert "2330.TW" in syms  # défaut conservé, rotation jamais bloquée
```

- [ ] **Step 2: Run, verify it fails** — `tick` n'a pas de hook override.

- [ ] **Step 3: Implement.** Ajouter à `tick` un `override_fn=None` (injectable pour test). Pour chaque venue **qui entre en pré-open ce tick** (présente dans `preopen_venues` et dont l'override n'a pas déjà tourné aujourd'hui — garder un `last_override_at` par venue dans `venue_state`), construire le payload `{ranked (de la venue), default_hot (sa hotlist persistée), sticky, market_context}`, appeler `override_fn`, passer par `apply_override` (qui protège déjà les sticky), écrire la hotlist finale dans `venue_state`, et journaliser défaut-vs-final via `rotation_ledger`. `try/except` autour de l'appel LLM → fail-safe sur le défaut. En prod, `override_fn = build_llm_override_fn()` si `params.override_enabled`, sinon `None` (rotation 100 % déterministe).

- [ ] **Step 4: Run, verify PASS** — `pytest tests/test_rotation_venues.py -k override -v`.

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(rotation): override LLM pré-open par venue sur D10 (défaut+fail-safe+ledger, §B3, D13)"
```

---

### Task B4: `override_enabled=false` ⇒ aucun appel LLM (garde déterministe)

**Files:**
- Test: `tests/test_rotation_venues.py`

- [ ] **Step 1: Write the test**

```python
def test_override_disabled_aucun_appel_llm(tmp_path):
    calls = []
    def spy(payload): calls.append(payload); return {"add": [], "remove": []}
    # avec override_enabled=false dans radar.yaml, tick ne doit PAS appeler override_fn
    _run_tick_disabled(override_fn=spy)
    assert calls == []  # défaut déterministe pur
```

- [ ] **Step 2-4:** Run → implémenter la garde `if params.override_enabled` dans `tick` → PASS.

- [ ] **Step 5: Commit**

```bash
git commit -am "test(rotation): override_enabled=false ⇒ rotation déterministe pure (§B4, D13)"
```

---

### Task B5: câblage prod — `tick` du daemon passe l'override réel

**Files:**
- Modify: `trader/daemon.py` (le `_rotation_tick`/`tick(...)` du main loop, ~ligne 2706) pour passer `override_fn` réel quand `override_enabled`
- Modify: `trader/rotation_wiring.py` (helper assemblant `override_fn` + `market_context` depuis `family_regime`)
- Test: smoke d'intégration `tests/test_rotation_venues.py` ou `tests/test_daemon_*`

- [ ] **Step 1-4:** test smoke (override_fn assemblé non-None quand enabled ; market_context peuplé). Implémenter le câblage prod (fail-safe, timeout via `make_llm_override_fn`). Vérifier `pytest tests/ -q` vert.

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(rotation): câblage prod du sélecteur LLM pré-open dans le daemon (§B5, D13)"
```

---

### ⛳ Checkpoint Phase B

Run: `pytest tests/ -q`. Vérifier sur run réel : à T-90min d'une ouverture, 1 appel LLM/venue, `rotation_ledger` trace défaut-vs-final, fail-safe testé en coupant le LLM. Mettre à jour D13 (statut → 🛠 implémenté) et le §14 du spec swing si pertinent.

---

## Self-review (couverture spec → plan)

- Spec §3 (volet A pré-open) → A1, A2, A3, A4. ✅
- Spec §4 (volet B : défaut+override tracé, prompt sticky+contexte, câblage D10, fail-safe, ledger) → B1, B2, B3, B4, B5. ✅
- Spec §4.3 (pas de « LLM sur tout le pool ») → préservé : l'override agit sur le `ranked`/`default_hot` de la venue (shortlist radar), jamais sur le pool brut. ✅
- Garde-fou #1 (sticky avant ressortie) → couvert par l'invariant sticky existant (`sticky_collector` dans `tick`) + B1 (prompt) ; **à vérifier explicitement** qu'un plan créé en pré-open devient sticky avant la clôture (ajouter un test si non couvert par les tests sticky existants).
- Garde-fou #2 (dû au scheduler) → **à vérifier** : un symbole pré-open passe par `reconcile_universe` puis `_select_due_symbols`. Ajouter un test daemon si un symbole admis pré-open n'est jamais dû.
- Garde-fou #3 (overlap + cap) → `max_model_calls_per_cycle` inchangé ; l'override pré-open est 1/venue/jour, hors hot-path. ✅
- Garde-fou #4 (calendrier) → **limitation v1 assumée et tracée** (note Task A4) : alignement `exchange_calendars` = chantier séparé.

> Deux points « à vérifier » ci-dessus (garde-fous #1 et #2) sont des risques d'intégration daemon non couverts par les tests unitaires de rotation. L'implémenteur DOIT ajouter un test d'intégration daemon pour chacun avant le checkpoint Phase B, ou confirmer qu'un test existant les couvre.
