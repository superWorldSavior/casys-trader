# Attribution des sélections univers : verdict contre le banc — design

- **Date** : 2026-08-16
- **Statut** : ✅ validé (principe, Erwan 2026-08-16) · fact-check grok-4.6
  **GO-AVEC-CORRECTIFS** intégré le 2026-08-16 (§2.8-2.9 et corrections datées) ·
  à implémenter (6 lots)
- **Auteurs** : Erwan + Claude
- **Portée** : boucle de feedback des sélections d'univers (attribution `9fbc9ae`,
  digest → prompt `500cfdd`). Ne touche ni au trader, ni à la composition de la
  hotlist, ni au retrieval situation.
- **Registre** : D16. Complète D15 — les scopes candidats immuables de D15 sont
  exactement le contrefactuel que ce design consomme.

> Principe directeur : **l'agent univers est un allocateur d'attention ; on le
> juge comme un allocateur** — ses picks contre le banc qu'il a laissé, pas
> contre un seuil absolu. La direction reste une prédiction optionnelle, jugée
> par le juge directionnel existant. Zéro constante de réussite arbitraire.

---

## 1. Diagnostic — faits vérifiés (2026-08-16, code + données live)

| # | Fait | Preuve | Impact |
|---|------|--------|--------|
| F1 | **Le juge est directionnel-seul** | `classify_selection_quality()` renvoie `non_evaluable` dès que `allowed_sides` n'est pas strictement long ou short (`trader/domain/universe/selection_attribution.py:95-105`) | Sur 476 **lignes** d'historique mandats (⚠️ `history.jsonl` concatène `write_prepared` ET `activate` — voir §2.8) / **8 789 lignes de sélection**, **22 % seulement sont directionnelles** → 78 % ne sont jamais jugées. Et le two-sided n'est pas une paresse de l'agent : le contrat du prompt pousse `["long","short"]` même avec un `directional_view` tranché (`trader/agent/universe/prompt.py:70-72`) |
| F2 | **La promesse systématique n'est pas notée** | Toute sélection promet « ce symbole vaut un slot d'attention » ; seule la promesse optionnelle (« …et dans ce sens ») est jugée | Boucle structurellement muette : le digest exige `n≥5` verdicts directionnels par famille (`MIN_FEEDBACK_N`, `trader/application/universe/selection_attribution.py:27`) et `_feedback_rows()` ne compte que `gagnant+perdant` (`:296-297,339-360`) → `status: insufficient` quasi permanent |
| F3 | **Le contrefactuel est déjà persisté** | **476/476** mandats portent `candidate_scope_id` (`state/universe_mandates/history.jsonl`, depuis le 2026-07-17) ; scopes immuables append-only `state/candidate_scopes/YYYY-MM-DD.jsonl` (depuis le 2026-07-10) avec `candidates[{symbol, attractiveness, bias, ...}]` (~40/venue = top 40 radar ; scopes FX `candidates: []` hors périmètre TW/EU/US) | Le banc est reconstructible par jointure — **zéro mécanisme de capture à ajouter** (dividende D15). ⚠️ MAIS l'API du store ne lit que courant/dernier par venue (`candidate_scope_store.py:52-72`) : la lecture **par id** est à créer (§2.9) |
| F4 | **Les fallbacks sont jugés comme des choix d'agent** | 149/476 lignes ont `status=fallback` ; `selections_from_mandate_payload()` ne filtre pas sur `status` (`trader/application/universe/selection_attribution.py:121-154`) ; l'activation fallback réécrit `allowed_sides: []` (`universe_mandate_store.py:88-102`) et des rafales dupliquent un même scope (17 fallbacks EU en ~17 min, mêmes 25 symboles) | Le FLAIR « agent » mélange baseline déterministe, sélection LLM et doublons de rafale — biais du signal |
| F5 | **La clé du store ignore la notion de base** | `UNIQUE(mandate_id, symbol, as_of, horizon_sessions)` (`trader/infrastructure/state_db/universe_selection_store.py:77`), migration v6, colonnes sans `verdict_basis` ni `candidate_scope_id` | Schéma à étendre |
| F6 | **Le cœur de scoring est déjà mutualisé** | Verdict directionnel = `classify_decision_quality()` et FLAIR = `compute_outcome_scores()` partagés avec learnings et notes de situation (`trader/domain/learnings/scoring.py`) | Pas de nouveau juge directionnel à écrire — la mutualisation existe à l'étage domaine |

## 2. Décision de design

### 2.1 Verdict à deux bases

Chaque sélection produit un ou deux verdicts, distingués par `verdict_basis` :

- **`allocation`** — pour **toutes** les sélections : le pick a-t-il offert plus
  d'opportunité que le banc ?
- **`direction`** — pour les sélections directionnelles uniquement : logique
  actuelle **inchangée** (`classify_decision_quality` signé, bande
  `SIGNIFICANT_RETURN_BAND`).

### 2.2 Le juge allocation (nouveau, domaine pur)

```
opportunité(s)   = |forward_return_over_sessions(bars_s, as_of, 5)|      # primitive existante :60-92
banc             = candidats du scope (candidate_scope_id) NON retenus dans le mandat
                   ET hors sticky                                         # voir ci-dessous
excess           = opportunité(pick) − médiane(opportunité(banc))
verdict          = gagnant  si excess > +SIGNIFICANT_RETURN_BAND
                   perdant  si excess < −SIGNIFICANT_RETURN_BAND
                   neutre   sinon
```

- La bande est réutilisée comme **bande d'égalité** autour de la médiane, pas
  comme seuil absolu de réussite. Aucun « seuil d'opportunité » à défendre : le
  banc est sa propre baseline (en marché mort, un pick qui bouge peu mais plus
  que le banc reste un bon choix d'allocation).
- **Frais** : pick et banc partagent la venue, donc la structure de frais — la
  comparaison relative les neutralise. Opportunité **brute**, pas de net-de-frais.
- **Sticky exclus du banc** : les sticky peuvent rester dans `candidates[]`
  (seule la default_hotlist les retire, `scope_rotation.py:64-68`) alors que
  l'agent n'a PAS le droit de les prendre (`selection_contains_sticky` rejette
  une hotlist qui les contient, `composition.py:253-255` ; union sticky après
  la hotlist, `venues.py:397-400`). Vérifié live (close EU 2026-08-16) :
  6 sticky présents dans `candidates[]`. Le banc = candidats non retenus **hors
  `sticky_context_at_close`** — on ne juge pas l'agent sur un choix interdit.
- **Garde de suffisance** (garde de données, pas critère de succès) : si le banc
  éligible (candidats non retenus, hors sticky) compte moins de
  `MIN_BENCH_EVALUATED = 8` membres → `non_evaluable` structurel. Si le banc
  éligible atteint ce plancher mais que moins de 8 trajectoires sont évaluables
  sur l'horizon → `pending`, sans ligne allocation persistée. Le prochain
  passage rejoue cette base lorsque les barres manquantes ou l'horizon
  deviennent disponibles.
- Propriété clé : un sélecteur aléatoire bat la médiane du banc ~1 fois sur 2 →
  **base rate allocation ≈ 0,5 par construction, parmi les verdicts décisifs**
  (WIN/LOSS — les `neutre` de la bande d'égalité sont exclus du base rate FLAIR
  comme partout, `scoring.py:59-64`). Le lift FLAIR mesure directement le
  talent d'allocation. La couverture est *structurelle* (toute sélection est
  jugeable), pas 100 % garantie : un scope historique introuvable ou un banc
  éligible structurellement trop maigre produit un `non_evaluable` ; un banc
  assez large mais incomplet ou encore immature reste `pending`.

### 2.3 FLAIR par base — base rates séparés

`score_selection_outcomes()` groupe les lignes par `verdict_basis` et appelle
`compute_outcome_scores()` **par groupe**. Mélanger les populations corromprait
les deux lifts (base rate « a bougé plus que le banc » ≈ 0,5 structurel vs
« a bougé dans le bon sens » ≈ taux directionnel du marché).

### 2.4 `selector` : agent vs baseline

Les mandats `status=fallback` sont jugés par le même pipeline mais tagués
`selector = baseline_fallback` (sinon `agent`). Le FLAIR et le digest injectés
au prompt ne portent que sur `selector = agent`. Bonus gratuit : le comparatif
**agent vs baseline déterministe** devient une simple requête analytics.

### 2.5 Sémantique versionnée + rejugement

Changement de sémantique du verdict → métadonnée `selection_semantics_version`
sur le **pattern** de `_migrate_benchmark_semantics()`
(`trader/infrastructure/state_db/learnings_store.py:240-324`) — pattern
seulement : `learnings_metadata` vit dans `learnings.db`, il faut créer en v7
une table `universe_selection_metadata(key, value, updated_at)` dans
**casys.db**. Au bump : `DELETE FROM universe_selection_outcomes` (table
entièrement dérivée) + rejugement complet. Le replay est possible : scopes
persistés depuis le 2026-07-10, mandats depuis le **2026-07-17**,
`candidate_scope_id` présent sur 100 % de l'historique. Ligne dont le scope est
introuvable → `allocation` = `non_evaluable` structurel, compteur
`bench_unresolved` dans le résultat du refresh.

La sémantique courante est `bench_v3` (2026-08-20). Le bump depuis
`bench_v2` purge les verdicts dérivés puis relance le rejugement : `bench_v2`
pouvait figer un banc transitoirement incomplet en `non_evaluable`, tandis que
`bench_v3` réserve ce verdict aux cas structurels (scope introuvable ou banc
éligible sous le plancher). Le même bump remet à zéro le cursor de refresh
durable.

### 2.6 Store — migration v7

Nouvelles colonnes : `verdict_basis` TEXT, `candidate_scope_id` TEXT,
`selector` TEXT, `opportunity` REAL, `bench_median_opportunity` REAL,
`allocation_excess` REAL, `bench_n` INTEGER. Clé unique étendue :
`UNIQUE(mandate_id, symbol, as_of, horizon_sessions, verdict_basis)`.
**Une ligne par (sélection × base)** : une sélection directionnelle produit deux
lignes ; une non directionnelle, une seule (`allocation`).

⚠️ SQLite ne sait pas élargir une contrainte UNIQUE table-level par `ALTER` —
et un simple `CREATE UNIQUE INDEX` laisserait l'ancien UNIQUE v6 interdire les
2 lignes par sélection. La v7 est donc une **recréation** : `CREATE TABLE`
nouvelle + `INSERT … SELECT` + `DROP` + `RENAME` (première migration de ce type
dans `migrations.py` — la v6 déjà appliquée n'est PAS mutée), et l'upsert du
store passe à `ON CONFLICT(mandate_id, symbol, as_of, horizon_sessions,
verdict_basis)`.

### 2.7 Digest et prompt

`selection_feedback_digest()` produit deux blocs par famille/rôle :

```json
{"family": "energie", "allocation": {"n": 31, "beat_bench_rate": 0.65, "mean_flair_score": 0.04, "utility": "helps"},
                       "direction":  {"n": 7,  "win_rate": 0.43,        "mean_flair_score": -0.02, "utility": "hurts"}}
```

`min_n = 5` **par base**. `status: observed` dès qu'une base parle. Le wording
du prompt univers (`trader/agent/universe/prompt.py:58-60`) est mis à jour pour
expliquer les deux lectures (« tes picks offrent-ils plus d'opportunité que le
banc » / « tes appels directionnels sont-ils bons »). La doctrine
`comparative_context_not_hotlist` est inchangée.

### 2.8 Unité de jugement (correctif fact-check, bloquant)

`state/universe_mandates/history.jsonl` concatène les événements
`write_prepared` ET `activate` (`universe_mandate_store.py:40-47,145`) : les
476 lignes ne sont pas 476 allocations distinctes, et l'ingestion actuelle
double-compte prepared/active. Règles :

- **On ne juge que les activations** : payloads `status ∈ {active, fallback}`.
  `prepared` est exclu (proposition, pas allocation ; et double compte).
- **Rafales de fallback dédupliquées** : une rafale (N activations fallback du
  même `candidate_scope_id` avec les mêmes symboles, ex. 17 lignes EU en
  ~17 min) compte UNE allocation par `(candidate_scope_id, symbol)` pour
  `selector=baseline_fallback`.
- **Horizon immature = pending, jamais persisté** : le refresh considère jugée
  toute clé présente dans le store (`refresh_selection_outcomes()`,
  `:450-466`) — persister un `non_evaluable` d'horizon non écoulé le figerait à
  jamais. On n'upsert une ligne `allocation` que lorsque l'horizon est écoulé
  (barre J+5 disponible) ou pour un `non_evaluable` **structurel définitif**
  (scope introuvable ou banc éligible sous le plancher). Dès que le banc
  éligible contient au moins `MIN_BENCH_EVALUATED` membres, une couverture
  évaluée sous ce plancher reste `pending`, y compris après une erreur marché
  transitoire.
  C'est le comportement actuel du juge directionnel (`evaluate_selection()`
  `:163-166` skip si forward None) — à préserver pour la base allocation.

### 2.9 Lecture de scope par id (correctif fact-check, bloquant)

`CandidateScopeStore` n'expose que `read_current`/`read_latest` par venue
(`candidate_scope_store.py:52-72`) — utiliser `read_latest` pour le rejugement
joindrait le **mauvais banc** sur tout l'historique. Ajouter
`read_by_id(candidate_scope_id, hint_date=as_of[:10])` : lecture du JSONL daté
du hint, puis scan des `????-??-??.jsonl` en repli (le scope close J-1 pour une
activation J est fréquent) — réutiliser l'infra de scan
(`trader/infrastructure/state_db/_jsonl_store.py:97-105`,
`find_newest_matching`). `read_latest` interdit dans le pipeline d'attribution.

## 3. Coût et données

- Calcul du banc borné : ≤ ~40 candidats/venue (top 40 radar), barres daily
  1y — même chemin que `evaluate_selections()` (`:181-206`). **Cache
  d'opportunités par `(candidate_scope_id, horizon)`** : un scope référencé par
  plusieurs mandats n'est évalué qu'une fois par batch.
- Le refresh est borné par `limit` et parcourt l'historique avec un cursor
  circulaire durable dans `universe_selection_metadata`. Même lorsqu'une page
  pleine ne produit aucun nouveau verdict, le passage planifié suivant reprend
  après cette page : les retryables situés au-delà de la limite ne sont pas
  affamés. Seules les bases absentes sont upsertées ; une direction déjà jugée
  n'est pas révisée pendant l'attente de son allocation.
- ⚠️ Yahoo sans retry 429 (`yahoo_client.py:129-135`, timeout 10 s) et
  `scripts/universe_selection_analytics.py:36-38` tape un `YFinanceDataSource`
  nu : le replay complet (~40 × N scopes) doit passer par le DataSource
  **throttlé** du daemon ou un débit borné équivalent, en plus du cache par
  scope.
- `limit=128` par batch conservé (= `DEFAULT_OUTCOME_BATCH_SIZE`,
  `learnings_sync_runtime.py:27`) ; l'appelant reste fail-open
  (`trader/runtime/learnings_sync_runtime.py:254-275`).

## 4. Lots d'implémentation (TDD, review Codex par lot)

1. **Domaine pur** — `opportunity()`, `classify_allocation_quality()`, scoring
   groupé par base. Invariants testés : banc vide/maigre → `non_evaluable`
   structurel ; horizon immature → pas de verdict (pending) ; tie dans la
   bande → `neutre` ; candidat du banc sans barres → exclu du banc (jamais
   compté à 0) ; pick sans barres → `non_evaluable` ; sticky exclus du banc.
2. **Résolution du banc + unité de jugement** — `candidate_scope_id` extrait
   dans `UniverseSelection` + `selections_from_mandate_payload()` ;
   `CandidateScopeStore.read_by_id()` (§2.9) ; filtre
   `status ∈ {active, fallback}` + dédup des rafales fallback (§2.8) ;
   exclusion des `sticky_context_at_close` du banc.
3. **Store v7** — recréation de table (§2.6), clé unique étendue, table
   `universe_selection_metadata`, `selection_semantics_version`, purge +
   rejugement.
4. **Pipeline refresh** — cache par scope, tag `selector`, 1-2 lignes par
   sélection, règle pending/immature (§2.8), compteur `bench_unresolved`.
5. **Digest + prompt + analytics** — deux blocs par famille/rôle, wording
   prompt, `scripts/universe_selection_analytics.py` : vue agent vs baseline.
6. **Doc** — MAJ état D16 au registre, pointeur depuis
   `docs/reference/universe-rotation.md` et `learnings-rag.md`.

## 5. Non-buts

- On ne juge toujours **pas le trader** — le contrat de `9fbc9ae` (un bon pick
  reste bon sans trade) est conservé tel quel.
- Pas de hotlist ni de recommandation par symbole dans le digest.
- Pas de MemRL sur les sélections (pas de « recall » de sélection à récompenser).
- Pas de moteur « claims » unifié — la mutualisation reste celle de l'étage
  domaine (`domain/learnings/scoring.py`), étendue par extraction opportuniste
  seulement.

## 6. Points ouverts

- `MIN_BENCH_EVALUATED = 8` : à confirmer après mesure sur l'historique rejugé.
- Horizon unique 5 séances conservé ; multi-horizons seulement si la mesure le
  réclame.
- Mesurer le nombre d'**allocations distinctes** après filtre §2.8 (les 8 789
  lignes brutes comptent prepared + rafales) et publier les vrais dénominateurs.
- Le contrat du prompt pousse `["long","short"]` par défaut (F1) : une fois le
  cadran allocation en place, décider si l'on veut inciter l'agent à exprimer
  des `allowed_sides` directionnels quand `directional_view` est tranché —
  décision métier séparée, PAS un prérequis de ce lot.
- Juger la `default_hotlist` du scope comme sélection virtuelle « baseline
  quantitative » (comparatif plus riche que les seuls fallbacks) — optionnel,
  hors lots 1-6.
- Rétention de `state/candidate_scopes/` : aucun ménage automatique aujourd'hui ;
  le rejugement complet (§2.5) dépend de leur conservation — à inscrire dans la
  politique de rétention avant tout ménage futur.
