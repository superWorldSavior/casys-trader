# Référence — World Model shadow

> **Type** : Reference (Diátaxis).
> **État vérifié** : 2026-08-28.
> **Code** : `trader/domain/world_episode.py` ·
> `trader/application/world_model/` ·
> `trader/infrastructure/state_db/world_model_store.py` ·
> `trader/infrastructure/state_db/world_model_query.py` ·
> `trader/reporting/read_models/world_evaluation.py` ·
> `trader/reporting/read_models/world_impact.py` ·
> `trader/reporting/read_models/world_status.py` ·
> `trader/runtime/world_model_runtime.py` ·
> `trader/interfaces/cli/world_model.py`
> **Store** : `state/world_model.db` (indépendant de `casys.db`)
> **CLI** : `uv run casys-trader world status --json`

Le World Model estime une transition de marché exogène, **jamais** une décision
Trader. Toute prédiction reste `shadow_only` / `NO_GO` en permanence. Il n'a
aucune autorité sur le Brain, l'Univers, le scheduler, le RiskGate, le broker
ou le portefeuille.

## Owners DDD

| Couche | Owner | Rôle |
|---|---|---|
| `domain/world_episode` | contrat | `WorldEpisode`, observation, outcome, prediction |
| `application/world_model` | cas d'usage | capture, labeler, encoding, baseline Markov, GRU, `WorldModelService`, réconciliation de mapping |
| `infrastructure/state_db` | persistance | journal append-only + query adapter lecture seule |
| `reporting/read_models` | lecture | évaluation préquentielle, impact Trader honnête, status projector |
| `runtime/world_model_runtime` | composition | adapter background fail-open et façade `WorldModelRuntime` |
| `interfaces/cli/world_model` | surface | adaptateur mince vers le projector reporting |

Les voies de capacité live sont `market`, `context`, `macro_source` et
`graph`. Les suffixes `.v1` des schémas payload (`world_feature.market.v1`,
`world_availability_receipt.v1`, `world_episode.v1`, …) sont des révisions
de sérialisation, pas des générations de capacité.

Une génération de `world_scope_mapping.v1` se publie et se supersede dans le même ledger.
Chaque génération est persistée immuablement ; une cohorte `COLLECTING`
de la même forme reste pinée sur ce hash. Un cutover store (archive hors ligne) n'est pas requis
pour un nouveau hash de mapping. Le runtime ne lit jamais un ledger
héritage. Pas de contrat tombstone actif.

`application/world_model` n'importe ni `runtime`, ni `infrastructure`. Ses
modules canoniques n'importent pas `reporting` ; seuls les chemins historiques
`application.world_model.evaluation` et `.impact` sont des shims de
compatibilité vers les owners reporting. Le GRU n'importe pas le baseline : les
deux modèles consomment `encoding.py`.

## Capture et ancre

`capture_world_episodes` projette un `WorldEpisode` par symbole actif/tradable
à partir d'une whitelist de preuves marché. Pas d'action, d'intent, de
quantité, de prompt, d'outils, de scheduler, de portefeuille, de fills, de
PnL, ni de mémoire Brain/Univers.

- **Aucun épisode** si aucune ancre OHLCV valide : le contrat d'identité
  exige une barre réelle ; un point inventé n'est pas persisté.
- Une ancre valide avec preuve de fraîcheur/disponibilité incomplète produit
  un épisode `training_eligible=false`.
- L'identité combine place, symbole, intervalle, barre T0 et versions de
  contrats features/sampling. Un replay exact est un no-op ; un même id avec
  un contenu différent est un conflit.

## Horizons et labels

Horizons **fixes**, sans fallback silencieux :

| Horizon | Durée | Code |
|---|---|---|
| 4 h | `timedelta(hours=4)` | `elapsed_4h.v1` |
| 1 j | `timedelta(days=1)` | `elapsed_1d.v1` |
| 3 j | `timedelta(days=3)` | `elapsed_3d.v1` |

Le pilote committe actif déclare `elapsed_4h.v1`, `elapsed_1d.v1` (primaire)
et `elapsed_3d.v1`. `lifecycle_generation: 3` frappe de nouveaux `cohort_id` ;
les IDs de génération 2 ne sont pas réutilisés. Les prédicteurs liés à une
cohorte ne calculent que les horizons de leur propre manifeste, ce qui permet
aux cohortes de se chevaucher sans fuite de protocole.

`anchor_end_at` est l'horloge de transition : la barre d'ancrage doit être
close. `target_at = anchor_end_at + duration`. Un intervalle source plus long
que l'horizon (ex. barre daily pour un label 4 h) rend le label `unknown` ;
le 1 j ne se substitue jamais au 4 h.

Classes directionnelles, bande **50 bp** (`simple_return_band_50bp.v1`) :

| `simple_return` | Classe |
|---|---|
| ≥ +0,005 | `UP` |
| ≤ −0,005 | `DOWN` |
| sinon | `FLAT` |

Un label peut rester `pending` ou se fermer en `observed` / `missing` /
`unknown`. Seul `pending` est recalculé. Une correction future s'ajoute en
outcome supersédant append-only ; la feuille active est rejouée.

## Horloges de prédiction

| Champ | Sens |
|---|---|
| `predicted_at` | cutoff logique du snapshot marché, gelé à la capture |
| `ready_at` | disponibilité réelle de persistance (`recorded_at` d'append) |

Les deux doivent précéder strictement le label pour qu'une prédiction soit
scorable. Le cutoff logique seul ne suffit pas : le worker est asynchrone.

## Modèles

- **Baseline** : Markov catégoriel hiérarchique à lissage Dirichlet
  (`hierarchical_dirichlet_world_baseline`) — état exact, puis grossier, puis
  fréquence globale, puis uniforme au cold start.
- **Challenger** : GRU NumPy en ligne (`online_gru_world_challenger`) sur les
  douze derniers épisodes compatibles du même instrument. Encodeur fixe, un
  GRU par horizon, une étape BPTT déterministe par `outcome_event_id` durable.

Un redémarrage reconstruit l'état en rejouant épisodes puis labels dans
l'ordre causal. Les deux modèles restent `shadow_only` / `NO_GO`.

## Patterns graphe prospectifs

Le détecteur explicite `explicit_graph_pattern.v1` est complémentaire à
Markov/GRU : il cherche des chaînes typées dans les snapshots du graphe, pas
des motifs cachés dans l'état récurrent du GRU. Ses agrégats DDD sont
`PatternHypothesis` et `PatternOccurrence`; leurs transitions sont des events
append-only. `PatternDiscoveryCompleted` ferme une fois pour toutes le dataset
de formation d'une cohorte après examen d'un ensemble mûr de la révision
exacte, y compris quand la sélection contient zéro hypothèse. L'absence de
record mûr exact à `formation_cutoff` (start prouvé) n'écrit pas de marqueur
et ne se résout pas en attendant des captures postérieures au cutoff.

`PatternShadowWorkflow` orchestre automatiquement le lifecycle dans le worker
background, après la capture du batch :

1. sélection de l'unique cohorte graphe `collecting` et preuve de son start ;
2. découverte/rejeu à `formation_cutoff` gelé ;
3. matching prospectif strictement après la prochaine frontière de barre ;
4. persistance des `WorldPrediction`/occurrences shadow ;
5. liaison, en dernier, des feuilles 4 h / 1 j / 3 j déjà disponibles.

Le marqueur durable autorise l'évaluation ; une écriture échouée ne peut pas
être remplacée par l'état local du process. Replays et retries utilisent des
identités canoniques déterministes. Toutes les étapes restent
`shadow_only`, `decision_effect=none`, `causal_claim=false`, `NO_GO`.

`world pattern status` projette les events durables
`world_pattern_lifecycle_events` : `formation_cutoff`,
`evaluation_start_not_before`, identifiants/compte sélectionnés, empreintes
et autorité de replay (`event_id` / `started_event_id`). Ce n'est pas une
étiquette, ni une claim causale. Quand la cohorte d'évaluation liée est
`collection_closed` ou a dépassé son `fixed_end` inclusif, le lifecycle
append `PatternEvaluationClosed` via `ClosePatternEvaluation` ; occurrences
et snapshots restent immuables.

## Évaluation

Owner : `reporting/read_models/world_evaluation.py` (`world_shadow_evaluation.v2`).

- Comparaison appariée baseline/GRU sur les mêmes `(épisode, horizon)`.
- Support minimum apparié : **20**. En dessous : `insufficient_support`, pas
  de gagnant déclaré.
- Métriques : Brier, log-loss, accuracy, ECE 5 bins.
- Drawdown directionnel de marché : proxy additif à notionnel unitaire, sans
  frais, FX ni sizing. Les horizons peuvent se chevaucher. **Ce n'est pas un
  drawdown de portefeuille.**

Les colonnes indexées `model_kind` et `move_class` priment sur les claims du
payload.

## Impact Trader

Owner : `reporting/read_models/world_impact.py` (`world_shadow_impact.v1`).

| Champ | Statut actuel | Raison |
|---|---|---|
| `actual_contribution` | `not_attributable` | `world_model_decision_effect_none` |
| `counterfactual_contribution` | `not_available` | ledger d'exécution/simulation manquant |

Aucun uplift Trader n'est inféré d'une bonne prévision de marché. Une
association descriptive future exige :

1. un lien append-only explicite `prediction_id → decision_id`
   (`link_kind=pre_decision_reference`) ;
2. `predicted_at` et `ready_at` strictement avant `decision_dispatch_at` ;
3. un cycle flat-to-flat à frais connus ;
4. pour un delta PnL/drawdown : une politique d'ordres/fills shadow
   pré-enregistrée et son ledger.

`cycle_ts` et le début de prompt Brain ne prouvent pas l'ordre réel.

## Persistance

`state/world_model.db` est un journal dédié, indépendant de `casys.db` et de
`decisions.jsonl`. Il contient aussi les tables append-only de lifecycle,
hypothèses, occurrences et liens d'outcomes patterns. WAL SQLite, transactions
courtes, `UPDATE`/`DELETE` refusés. Les query adapters de formation,
d'évaluation, de catalogue et d'outcomes ouvrent en `mode=ro` et ne créent ni
ne migrent la base.

Les journées UTC closes de `world_shadow_predictions` peuvent être projetées
dans `state/world_model_archive/` en Parquet ZSTD. Chaque partition publiée a
un manifeste `verified` (schéma et types physiques, bornes, cardinalité,
unicité, hash ordonné du contenu et hash du fichier). L'export lit le ledger
via un index `recorded_at` additif (bornes `[jour, jour suivant[`) et refuse
la journée UTC ouverte. Un miroir Parquet endommagé est quarantiné (jamais
écrasé ni effacé) puis reconstruit depuis SQLite, sans bloquer les jours
suivants. Cette projection reste `shadow_only`,
`decision_effect=none` et `source_retained=true` : elle ne permet actuellement
aucun `DELETE`, `VACUUM` ou remplacement de `world_model.db`. Le futur cutover
hot/cold devra d'abord fournir un index d'identités et des query adapters
SQLite + Parquet avec tests de parité.

Les nouvelles prédictions référencent l'épisode canonique par `episode_id`,
`feature_hash` et `input_sha256` au lieu de recopier son observation complète.
Les read models rejoignent une vue légère de l'épisode ; les anciennes lignes
contenant `input` restent lisibles.

## Activation

`CASYS_WORLD_MODEL_SHADOW_ENABLED` (défaut `1`) n'est lu **qu'au boot** du
daemon. Un changement de flag ne prend effet qu'après redémarrage. Le cycle
métier reste fail-open : une panne shadow est loguée, jamais propagée.

## Statut opérateur

```bash
uv run casys-trader world status --json
```

| `status` | Signification |
|---|---|
| `not_started` | le fichier n'existe pas (daemon jamais booté avec le shadow, ou flag off) |
| `warming_up` | base présente, aucune paire prédiction/label encore scorable |
| `evaluating` | au moins un groupe préquentiel `ready` |
| `schema_unavailable` | fichier présent, tables requises absentes |
| `unavailable` | erreur de lecture |

Le live actuel peut rester `not_started` tant que le daemon n'a pas créé
`state/world_model.db`.
