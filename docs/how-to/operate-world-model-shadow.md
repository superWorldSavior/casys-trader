# How-to — Opérer le World Model shadow

> **Type** : How-to (Diátaxis). Procédure opérateur.
> **Référence** : [`reference/world-model.md`](../reference/world-model.md)
>   (contrat marché ; le pilote cohorte/macro/graphe est décrit ici et dans les
>   pages explanation / note de statut).
> **Pourquoi** : [`world-model-shadow.md`](../explanation/architecture/world-model-shadow.md),
>   [`world-context-ontology.md`](../explanation/architecture/world-context-ontology.md).
> **Statut pilote** : [`2026-08-24-world-model-shadow-pilot.md`](../decisions/2026-08-24-world-model-shadow-pilot.md).
>
> Cette page **ne redémarre pas**, ne pousse pas, et n'écrit pas dans `state/`
> ni `.env`. Un arrêt/relance reste un acte volontaire, voir
> [`run-the-daemon.md`](run-the-daemon.md).

Tout le World Model reste `shadow_only` / `decision_effect=none` / `NO_GO`.
Il n'influence ni le Brain, ni l'Univers, ni le scheduler, ni RiskGate, ni le
broker. Une métrique de marché n'est **pas** un PnL Trader. Après une semaine
de collecte supervisée : couverture, plomberie, tendances préliminaires —
jamais une preuve causale.

## Lire le statut

Depuis la racine du dépôt, sur l'état paper courant. Les lectures **ne créent
pas** `world_model.db` ni `state/world_macro/` :

```bash
uv run casys-trader world status --json
uv run casys-trader world macro status --json
uv run casys-trader world graph status --json
```

`world status` (`schema_version=world_model_status.v1`) porte aussi le bloc
macro. Champs à lire en premier :

| Champ | Lecture honnête |
|---|---|
| `status` | `not_started` si `state/world_model.db` n'existe pas encore |
| `authority` | toujours `shadow_only` |
| `decision_effect` | toujours `none` |
| `recommendation` | toujours `NO_GO` |
| `causal_claim` / `pnl_claim` | toujours `false` |
| `evaluation.status` | `warming_up` tant qu'aucune paire n'est scorable |
| `impact.actual_contribution.status` | `not_attributable` |
| `macro.gaps.gdelt` / `macro.gaps.news_macro_brief` | `excluded` |

`world graph status` (`world_graph_status.v1`) expose les budgets (profondeur
4, 32 chemins) et `gaps.writes`. Un graphe **câblé** n'est pas une écriture.

`world status` porte aussi `resource_budget` : décision live du garde-fou
disque (taille logique / on-disk de `world_model.db` et octets libres). Cette
lecture **ne crée pas** la base. `status=skipped` veut dire que le prochain
batch d'écriture shadow serait sauté, pas que le Trader s'est arrêté.

### Cohortes (status + rapport)

Le CLI n'a pas de `list`. Les `cohort_id` stables sont dérivés du YAML
(`pilot_id` + `schema_version` + `cohort_key` + `activation_policy` +
`lifecycle_generation` + `content_sha256`) et apparaissent dans le log
`[world_shadow_pilot]` au boot. Le YAML committe `lifecycle_generation: 4` :
cela **frappe de nouveaux** `cohort_id` ; les IDs de génération 3 ne sont pas
réutilisés. Le lot actif déclare `elapsed_4h.v1`, `elapsed_1d.v1` (primaire)
et `elapsed_3d.v1`. Deux clés :

| Clé YAML | `study_kind` | Lanes |
|---|---|---|
| `technical_c1` | `pipeline_pilot` | Markov + GRU × `market` / `status_only` / `company` / `macro` / `joint` — **pas de graphe** |
| `graph` | `pipeline_pilot` | Markov + GRU × `graph` seulement |

```bash
uv run casys-trader world cohort status --json COHORT_ID
uv run casys-trader world cohort report --json COHORT_ID
uv run casys-trader world graph report --json COHORT_ID
```

`world cohort report` (`world_cohort_report.v1`) est reconstructible et
lecture seule. `interpretation_limit=coverage_plumbing_preliminary_trends_only`
tant que l'empan de collecte est inférieur à 8 jours. `causal_claim` et
`pnl_claim` restent `false` même après.

### Cycle automatique des patterns graphe

Quand la voie graphe est effectivement composée, chaque batch shadow autorisé
par le garde-fou exécute, dans son thread de fond :

```text
mature labels → capture/predict → discover/replay → evaluate → link outcomes
```

Il n'existe pas de second flag patterns : couper la voie graphe empêche sa
composition. Le premier batch éligible sélectionne l'unique cohorte graphe
`collecting`, prend comme `formation_cutoff` la disponibilité prouvée de son
`WorldCohortStarted`, puis fixe `evaluation_start_not_before` à la prochaine
frontière de barre. `PatternDiscoveryCompleted` n'est écrit que si un ensemble
mûr de la révision exacte a été examiné, y compris zéro candidat. Zéro record
mûr exact à ce cutoff figé → `no_ripe_exact_records_at_formation_cutoff`,
workflow `partial`, pas de marqueur. Ce n'est pas une attente dans la même
fenêtre : une capture postérieure au start ne peut pas former. Le restart
rejoue un marqueur déjà écrit et ne redécouvre pas avec des données arrivées
après le cutoff. C'est la séparation prospective, pas une panne.

Les batches suivants matchent uniquement après cette frontière, persistent les
prédictions/occurrences shadow, puis lient en dernier les feuilles réellement
disponibles `elapsed_4h.v1`, `elapsed_1d.v1` et `elapsed_3d.v1`. Une panne à
n'importe quelle étape est `partial`/fail-open et ne touche jamais Trader. Un
batch refusé par `resource_budget` ne lance aucune étape patterns.

Les commandes manuelles restent utiles pour l'inspection et le diagnostic ;
elles ne sont plus nécessaires à l'entretien normal du lifecycle :

```bash
uv run casys-trader world pattern status --json
uv run casys-trader world pattern report COHORT_ID --json
```

`world pattern status` affiche le marqueur durable
(`formation_cutoff`, `evaluation_start_not_before`, ids/compte sélectionnés,
empreintes, autorité de replay). Un restart rejoue ce marqueur ; il ne
redécouvre pas. Quand la cohorte d'évaluation liée est `collection_closed`
ou a dépassé son `fixed_end` inclusif, le lifecycle append `PatternEvaluationClosed`
via `ClosePatternEvaluation`. Les occurrences, outcomes, snapshots, marqueurs
et slots restent immuables.

Un pattern n'est déclaré que s'il existe une `PatternHypothesis` persistée et
des occurrences prospectives liées à leurs outcomes. Une régularité Markov/GRU
ou un chemin NetworkX seul n'est toujours pas un pattern.

### Diagnostiquer une découverte vide

Faire le préflight sur la révision exacte de la cohorte, avec les horizons de
son manifeste et un cutoff explicite. La commande reste en lecture seule sans
`--apply` :

```bash
uv run casys-trader world pattern discover \
  --formation-cutoff FORMATION_CUTOFF \
  --evaluation-start-not-before EVALUATION_START \
  --ontology-revision ONTOLOGY_REVISION \
  --horizon elapsed_4h.v1 --horizon elapsed_1d.v1 --horizon elapsed_3d.v1 \
  --json
```

Le rapport sépare les exclusions de records (`rejection_counts`) des critères
de sélection des groupes (`group_rejection_counts`). `groups` montre au plus
20 groupes, leur chemin, leur DriverState éventuel, leurs distributions et
trois exemples de preuves par groupe ; `groups_omitted` compte le reste. Un
groupe peut échouer à plusieurs critères. Les seuils restent support 20 et
association 0,10. Les groupes exclus par `max_candidates` restent distingués
des groupes qui échouent aux critères statistiques.

Si support et population sont identiques, l'association vaut zéro : le chemin
ne distingue aucune situation dans cet échantillon. Vérifier alors
`records_with_knowledge_relations` et `records_with_driver_state_bindings`,
puis les membres des snapshots et le registre de la liaison macro. Un total
élevé de prédictions ne garantit ni des observations indépendantes, ni la
présence de contexte admis. Les records sans chemin restent dans la population
de comparaison et sont comptés dans `records_without_paths`.

Un changement de plan macro peut laisser la collecte source-only active tout
en bloquant la liaison graphe avec `unknown_config_drift`. Le redémarrage seul
ne résout pas ce cas. L'alignement automatique conserve ce refus ; une
transition explicite doit nommer la génération bloquée et la spécification
cible complète, puis réserver un nouveau curseur prospectif. Utiliser le
mapping réellement chargé par le runtime ; un fichier YAML régénéré pendant
son exécution ne prouve pas ce mapping. Ne pas réécrire les anciens snapshots.

Après réparation de la capture, attendre des snapshots avec contexte admis et
des outcomes mûrs, puis refaire ce préflight avant d'ouvrir l'évaluation
suivante. Une nouvelle cohorte ouverte avec une formation sans contexte peut
figer légitimement zéro candidat. La fermeture d'une cohorte en dérive est un
événement ajouté à son historique ; elle ne restaure pas ses voies et ne
permet pas de réutiliser rétroactivement les données de formation.

### Cohorte graphe bloquée au boot

Le log `[world_shadow_pilot]` donne le statut d'activation. `blocked`
signifie qu'au moins une cohorte n'a pas démarré (le détail est dans le
rapport par cohorte, champ `blocked_reason`) ; sur un blocage ontologie,
`technical_c1` continue pendant ce temps. Pour la clé `graph` :

| `blocked_reason` | Sens | Action |
|---|---|---|
| `graph_ontology_unpinned` | l'attestation n'a rien commité (warning `ontology attestation skipped` juste avant) : aucune cohorte graphe n'est enregistrée | lire la cause dans le warning, pas de cohorte à nettoyer |
| `graph_ontology_unpublished` | la révision pinée n'est pas prouvée dans le ledger (cohorte `registered`, jamais `collecting`) | vérifier que le pin égale la révision publiée (`world graph status`) |
| `runtime_identity_drift` | le code lanes du boot diffère de celui de la cohorte existante : voies gelées, fail-closed | rotation manuelle ci-dessous, jamais de mutation |

L'identité est à deux niveaux : `git_commit` est audit-only, seul
`lane_code_hash` (contenu des `trader/**/*.py` moins la denylist des
modules prouvés hors-lanes : agents, collecteurs, reporting, interfaces…)
déclenche la dérive. Un commit hors-lanes ne bloque plus ; un changement
de code lanes bloque toujours, fail-closed. Contrairement aux deux lignes
ontologie, `runtime_identity_drift` concerne n'importe quelle clé
(`technical_c1` incluse) : le boot gèle les voies des cohortes dérivées
**sans créer de successeur**. La capture continue (les
épisodes s'accumulent) mais les voies gelées n'apprennent plus. Rotation,
par cohorte dérivée :

```bash
uv run casys-trader world cohort invalidate COHORT_ID --reason accepted_drift --json
```

L'invalidation seule ne suffit pas : le curseur d'activation n'avance que
si le mapping, la révision ontologie, le hash config ou la génération
bougent — sinon le boot retrouve la cohorte invalidée et reste bloqué sur
le même `runtime_identity_drift`. Frapper une génération
(`lifecycle_generation` + 1, rehash `content_sha256`), puis redémarrage
volontaire : le boot crée des cohortes fraîches (nouveaux fingerprints).
Les épisodes déjà stockés restent en base ; les liens outcomes à cheval
sur la rotation sont refusés fail-closed.

Un warning `conflict: committed ontology heads do not match the published
revision (revision_drift:<cause>)` nomme la cause exacte (dérivation,
mapping, registre). Une cohorte graphe `registered` pinée sur une ancienne
génération ne bloque plus les suivantes : le boot la saute et crée une
cohorte fraîche sur le pin live. L'ancienne reste listée (le pilot ne ferme
que les `collecting` expirées et n'invalide jamais seul) mais inerte :
fenêtre dépassée, jamais réutilisée, aucun slot admis.

## Distinguer boot, cycle dû, cycle idle

Trois états distincts. Ne pas les fusionner :

| Observation | Ce que ça prouve | Ce que ça ne prouve pas |
|---|---|---|
| Log `[world_model_shadow] enabled` / `[world_shadow_pilot] status=started` | câblage au boot, cohortes `register`/`arm`/`start` ; `patterns=1` si le lifecycle est composé | qu'un épisode ou un pattern a été écrit |
| `world graph status` avec overlay câblé | le worker graphe est instancié | une projection persistée ou un `CAUSES` |
| `episodes_appended=0` au boot | l'activation **ne backfill pas** | un échec de plomberie |
| Cycle daemon `idle_waiting_for_wake` / replay de la même barre | le daemon vit | un nouveau `WorldEpisode` |
| Compteur d'épisodes qui monte | un **cycle dû** a capturé une ancre OHLCV valide | un effet Trader |
| `resource_budget.status=skipped` / log `stage=resource_budget` | last-resort disque : capture/training shadow sautés | un HOLD Trader, un arrêt daemon, une purge |

Le hook de capture tourne après le snapshot marché, **avant** le dispatch
LLM (`reason=market_snapshot_pre_dispatch`). Sans ancre OHLCV valide : **aucun
épisode**. Un replay exact est un no-op. Un cycle idle ou un worker encore
en file (`queued_latest`) peuvent donc produire **zéro** nouvelle ligne alors
que le daemon et le shadow sont câblés. Le graphe le dit explicitement :
`gaps.writes=none_until_due_cycle`. Un cycle dû dont le `(market_venue,
instrument)` est `unmapped` ou `ambiguous` écrit le marché et un compagnon graphe
missing/status-only, sans racine MIC ni relation structurelle/connaissance.

Vérifier ensuite, dans cet ordre :

1. flags **du process déjà lancé** (un export local ne change rien) ;
2. `state/world_model.db` et, si le worker macro est on, `state/world_macro/` ;
3. logs `[world_model_shadow]`, `[world_shadow_pilot]`,
   `[world_macro_source_only]` dans `state/daemon_console.log`.

## Snapshot d'hydratation (restart sans replay)

Au premier boot (ou après invalidation), le worker rejoue tout le ledger
pour entraîner les prédicteurs, puis écrit `state/world_model_snapshot.json`
(état appris seul, quelques Mo). Au boot suivant, si le ledger n'a pas
bougé, les poids sont restaurés au lieu d'être réentraînés : le restart
passe de ~21 s à ~6 s CPU sur le ledger courant (23 k épisodes, 7 k labels,
2 prédicteurs ; le reliquat est le chargement/parse des lignes et la
re-observation des épisodes).

Lire le rapport `mature_pending` :

| Champ | Lecture honnête |
|---|---|
| `model_snapshot.restored=true` | poids restaurés ; `model_replayed=0` attendu, `model_observations_replayed=N` (registres reconstruits) |
| `model_snapshot.reason=snapshot_missing` | premier boot normal, pas une erreur |
| `reason=snapshot_*_changed` | nouvelles données ou nouvelle config : replay réel, normal |
| `reason=snapshot_corrupt*` / `snapshot_state_checksum_mismatch` / `snapshot_*_failed` | anomalie : replay quand même, warning une fois par process |
| `model_snapshot_save.saved=false` | sauvegarde échouée (ex: `snapshot_predictor_unsupported`, `snapshot_unwritable`) ; l'hydratation mémoire reste correcte |

Règles d'invalidation (tout écart force un replay) : empreintes ledger,
identités `model_id:model_version`, contrats prédicteurs (hyperparamètres,
contrats de features, lane identity), horizons configurés, cutoff causal
reculé ou inconnu. Un prédicteur legacy sans protocole snapshot désactive le
snapshot
bruyamment plutôt que de restaurer partiellement.

Trappe de secours : supprimer le fichier force exactement un replay au
prochain boot. Ne jamais l'éditer à la main (checksums).

## Fenêtre d'une semaine

La fenêtre n'est **pas** une date ISO pré-expirée dans le YAML. Le calendrier
supervisé commence le **2026-08-24, Asia/Taipei**. Le runtime estampille :

- `planned_start_not_before` = horloge UTC du **premier** boot qui active
  (`window.planned_start=boot_event_time`) ;
- `collection_stop_at` = start + 7 jours (`fixed_end`).

Un second boot est idempotent : il **ne décale pas** la fenêtre, ne
ré-écrit pas d'épisode, et répare au plus les reçus d'availability. Relire
`world cohort status` : `phase=collecting` et les deux timestamps gelés.

## Contrat live unique (cutover store frais)

Les voies de capacité sont `market`, `context`, `macro_source` et `graph`.
Les suffixes des schémas payload (`world_feature.market.v2`,
`world_availability_receipt.v1`, `world_episode.v1`, `world_graph_snapshot.v1`,
…) sont des **révisions de sérialisation**, pas des générations de capacité.

Une rotation de mapping **ne** requiert **pas** d'archive DB : publish +
supersede dans le même ledger. Une cohorte `COLLECTING` de la même
forme reste pinée sur sa génération. Un cutover store (autre
identité de **schéma**) reste hors de cette génération. Le runtime **ne
lit jamais** un ledger héritage. Pas de contrat tombstone actif, pas de
reader d'ancien reçu.

Le YAML committe un seul **contrat de schéma** : mapping
`world_scope_mapping.v1`, ontologie `market_ontology.v1`, producteur
`world_macro_source.v1`, lane `world.context.macro`, registre
`world_macro_sources.v1`, adapters `world_dbnomics_series.v1` /
`world_yahoo_commodity.v1`, plan `WORLD_MACRO_COLLECTION_PLAN_ID` /
`656391e83bd0f70729666f4474247a5af2e74e62fca16f1c2811ccb1363a39df`.
Les schémas payload restent `macro_source_fact.v1`,
`macro_world_observation.v1`, `macro_source_registry.v1`,
`macro_collection_plan.v1` et `macro_graph_bridge_run_spec.v1`.
Table exacte `(market_venue, instrument)`, **aucun** fallback runtime
`US → XNYS`.

`mapping_id` / la famille `market_ontology.v1` sont le contrat. Le **hash
de contenu** est la génération. L'instance d'ontologie est
`market_ontology:v1:<mapping_sha256>` en dérivation historique, ou
`market_ontology:v1:<canonical(mapping, registre, catalogue)>` en dérivation
étendue (attestation `extended=True` au boot, cas de la prod). Le pilote ne
dérive jamais cet id lui-même : il pine exactement la révision commitée par
l'attestation (`ensure_published`). Une rotation d'univers réconcilie
les symboles manquants depuis les métadonnées provider (MIC explicite,
XNYS vs XNAS) ; les lignes déjà mappées ne sont pas réécrites. Un
contenu nouveau produit un nouveau `content_sha256` et une nouvelle
révision publiée (append + supersede), sans bump
`world_scope_mapping.v2`. La génération est persistée append-only
avant activation. Une cohorte déjà `COLLECTING` de la même forme
réutilise son manifeste piné ; les ancres absentes de ce pin attendent
la fenêtre suivante. Dry-run :
`casys-trader world scope reconcile` ; `--apply` persiste. L'observer
de rotation et le boot sont fail-open et n'ont pas d'autorité Trader.

Le runtime **fail-close** le worker shadow sur une identité de schéma
inconnue (plan de collecte, producteur). Un store déjà publié avec la
même famille et un hash précédent **supersede** dans le même ledger :
l'histoire PIT reste lisible. Un store vide publie la génération
courante. Le cycle Trader reste fail-open. Pas de cutover store pour une
rotation de mapping.

1. le boot **réconcilie** le mapping (fail-open), puis **classifie**
   l'état durable du pont **avant** toute réservation de curseur. Cold
   start = `missing` → activate. Restart identique = no-op. Spec bloquée
   identique = resume no-op. Drift actif de la même famille = block puis
   roll. Drift de plan/producteur = erreur, pas de wildcard ;
2. le boot publie la révision dérivée si le store est vide. Même famille,
   nouveau hash = append + publish + supersede. Une autre identité de
   schéma (`market_ontology.v2`) est un conflit ;
3. `world graph status` / `world cohort status` doivent montrer les têtes
   `world_scope_mapping.v1` / famille `market_ontology.v1` (contrat) plus
   le hash / l'instance pinés par la cohorte ;
4. un symbole sans ligne exacte reste `unmapped` : snapshot graphe
   `missing` **sans racine**, sans membres, sans MIC inventé. Le schéma
   reste `world_graph_snapshot.v1` (null = missingness, payload
   canonique ; les colonnes SQL vides ne sont pas une entité).

Pas de `DELETE`/`VACUUM` du ledger live. `shadow_only` /
`decision_effect=none` / `causal_claim=false` / `pnl_claim=false`
inchangés. Les cohortes déjà collectées restent pinées sur leur hash et
lisibles. Une génération B de contenu seul **ne** coupe **pas** une
cohorte `COLLECTING` same-shape : elle reste pinée, les nouvelles
ancres attendent B. La successeure n'est armée qu'après clôture /
complétion, ou si aucun live same-shape n'existe. Aucun SHA de
génération n'est figé dans le domaine.
Détail : [RFC macro §6.2 / §17](../superpowers/specs/2026-08-23-world-model-macro-source-only-design.md).

## Claims autorisés après une semaine

Après sept jours de collecte supervisée, on peut dire :

- la couverture (slots, capteurs, venues, horizons) ;
- que la plomberie capture / masks / appariement / restart tient ou casse ;
- des **tendances préliminaires** descriptives (log-loss / Brier appariés),
  sous `insufficient_support` tant que le minimum descriptif (20 ancres
  uniques) n'est pas atteint.

On ne peut **pas** dire :

- qu'une voie *cause* un mouvement (`causal_claim=false`, pas d'arête `CAUSES`) ;
- qu'un PnL Trader, un drawdown de portefeuille ou un uplift d'ordres
  existent (`pnl_claim=false`, `actual_trader_contribution=not_attributable`) ;
- qu'une étude `prospective_evaluation` a été close — les deux cohortes du
  pilote sont `pipeline_pilot`.

## Flags et YAML

Tous les flags sont lus **uniquement au boot**. Défauts runtime :

| Flag | Défaut | Effet |
|---|---|---|
| `CASYS_WORLD_MODEL_SHADOW_ENABLED` | `1` | shadow marché (Markov + GRU) |
| `CASYS_WORLD_MODEL_CONTEXT_ENABLED` | `0` | voies contexte |
| `CASYS_WORLD_MACRO_SOURCE_ONLY_ENABLED` | `0` | worker macro source-only |
| `CASYS_WORLD_MODEL_GRAPH_ENABLED` | `0` | voies graphe |
| `CASYS_WORLD_SHADOW_PILOT_ACTIVATION` | `1` | honore le YAML d'autorisation |

Le YAML `config/world_shadow_pilot.yaml` est l'**autorisation opérateur**, pas
un défaut RFC. Le lot committe actif porte `horizons:
[elapsed_4h.v1, elapsed_1d.v1, elapsed_3d.v1]`, `primary_horizon:
elapsed_1d.v1`, et `lifecycle_generation: 4` (nouveaux `cohort_id`). Si
`CASYS_WORLD_SHADOW_PILOT_ACTIVATION=1` **et** `enabled: true`, le boot
fait un **OU** avec `workers.*` :

- `workers.market` — marché déjà porté par `CASYS_WORLD_MODEL_SHADOW_ENABLED` ;
- `workers.context` → OR du flag contexte ;
- `workers.macro_source` → OR du flag macro ;
- `workers.graph` → OR du flag graphe.

Puis, si le store marché est créé, `activate_world_shadow_pilot` enregistre, arme
et démarre les deux cohortes (exception documentée à l'activation RFC).
`=0` saute register/arm/start **et** le OU YAML ; le marché reste si son flag
est on.

`config/world_graph.yaml` garde `cohort_id: null`. L'id graphe est
**injecté** au compose depuis le rapport d'activation, jamais lu dans ce
fichier.

## Garde-fou budget disque (last-resort)

Le shadow a un **plafond de dernier recours**, local au World Model. Il ne
remplace pas le dédoublonnage sémantique. Il ne touche ni Trader, ni Brain,
ni Univers, ni broker, ni `casys.db`.

Fichier versionné : `config/world_shadow_resource_budget.yaml`
(`schema_version=world_shadow_resource_budget.v1`, `content_sha256` canonique).
Pas de lecture magique d'environnement pour les seuils. YAML absent ou
invalide → les défauts conservateurs restent actifs.

Le fallback conservateur sans YAML reste à 2 GiB. Le profil opérateur
versionné est porté à 100 GiB. La réserve filesystem reste indépendante
et inchangée :

| Seuil | Défaut | Effet |
|---|---|---|
| `max_db_bytes` | 107374182400 (100 GiB ; fallback 2 GiB) | saute le batch si la taille logique **ou** on-disk (`world_model.db` + WAL/SHM) atteint le plafond |
| `min_free_bytes` | 3221225472 (3 GiB) | saute le batch si le filesystem a moins que cette réserve |
| `warn_interval_seconds` | 300 | warning structuré `[world_model_shadow]` au plus une fois par intervalle |

Avant **chaque** batch d'écriture (un `stat` par cycle, pas par épisode) :
si le plafond DB ou la réserve libre est franchi, le worker **saute seulement**
capture + entraînement World Model + lifecycle patterns de ce cycle. Le daemon
continue. Le chemin de décision Trader n'est pas appelé par ce garde-fou.

Le garde-fou **ne fait jamais** : `DELETE` / `TRUNCATE` / `VACUUM`, arrêt du
daemon, mutation de l'historique append-only. Relire `world status` ne crée
pas `world_model.db`.

Raisons : `within_budget`, `db_size_exceeded`, `free_space_below_reserve`,
`probe_error` (échec du port filesystem → skip fail-safe des écritures
shadow, Trader inchangé).

Pour desserrer le plafond : éditer le YAML (rehash `content_sha256`), puis
redémarrage volontaire. Ne pas baisser `min_free_bytes` sous ce qu'il faut
aux logs / `casys.db` / OS.

## Arrêter / désactiver le pilote

Sans toucher au process vivant. Choisir l'intention, puis un arrêt/relance
volontaire.

| Objectif | Action | Ce qui reste |
|---|---|---|
| Ne plus auto-activer au prochain boot | `CASYS_WORLD_SHADOW_PILOT_ACTIVATION=0` | marché si `SHADOW_ENABLED=1` ; les cohortes **déjà** `collecting` dans `world_model.db` restent collectantes jusqu'à `close` / `invalidate` |
| Couper contexte / macro / graphe au prochain boot | flags concernés à `0` **et** soit activation `0`, soit `workers.*: false` (rehash `content_sha256`) | marché |
| Fermer la collecte d'une cohorte | `uv run casys-trader world cohort close COHORT_ID --reason TEXT --json` | ledger append-only intact |
| Invalider le protocole | `uv run casys-trader world cohort invalidate COHORT_ID --reason REASON --json` | `complete` est terminal : on n'invalide pas après |
| Couper tout le shadow | `CASYS_WORLD_MODEL_SHADOW_ENABLED=0` | `world_model.db` n'est pas effacé ; **pas** de register/arm/start (pas de store). Le worker macro peut encore tourner si le YAML/flag l'OR : le couper aussi (ligne workers ci-dessus). Le chemin de décision Trader ne change pas |

Raisons d'`invalidate` (enum fermé) : `manifest_id_hash_conflict`,
`accepted_drift`, `pre_start_episode`, `contaminated_macro`, `future_leak`,
`unpaired_training`, `missing_lane_metadata`, `destructive_correction`.

`register` / `arm` / `start` CLI restent disponibles : le défaut RFC est
l'activation **explicite**. Le YAML `operator_authorized_on_boot` est
l'exception humaine, pas un feu vert trading.

## Activer / couper le marché seul

| Objectif | Action |
|---|---|
| Shadow marché on (défaut) | omettre le flag, ou `CASYS_WORLD_MODEL_SHADOW_ENABLED=1`, **puis redémarrer** |
| Shadow marché off | `CASYS_WORLD_MODEL_SHADOW_ENABLED=0`, **puis redémarrer** |

Le chemin de décision Trader ne change pas. Couper le shadow n'efface pas
`world_model.db`.

## Lire l'évaluation sans sur-interpréter

Quand `evaluation.status=ready` (voie marché) :

- comparer baseline et GRU seulement si `comparisons[].status=ready` ;
- en dessous de 20 paires : `insufficient_support`, pas de vainqueur ;
- le drawdown directionnel est un proxy de marché à notionnel 1, sans frais ;
  ce n'est pas le drawdown du portefeuille paper.

Quand `world cohort report` :

- `study_kind=pipeline_pilot` : technique uniquement ;
- `support` / `gates.support` : mode `descriptive_only` sur ce pilote ;
- `negative_controls.status=offline_only` : les permutations ne trainent
  pas les voies live ;
- `winner` reste `false` dans les claims bornés du rapport.

Quand `impact.actual_contribution.status=not_attributable` : ne pas
attribuer de PnL Trader au World Model.

## Isoler la preuve

| Store | Contenu | Autorité |
|---|---|---|
| `state/world_model.db` | épisodes, outcomes, prédictions, cohortes, hypothèses/occurrences/lifecycle patterns | journal append-only du shadow |
| `state/world_macro/` | faits / observations / runs source-only + reçus | producteur macro, pas Univers |
| `state/gdelt/` et `state/news_briefs/` | GDELT et `NewsMacroBrief` **Univers** | **exclus** comme source World Context |
| `casys.db` | broker, décisions Trader | jamais fusionné |

Ne pas copier de tables broker dans `world_model.db`, ni y ouvrir
d'écriture manuelle. Le query adapter et le CLI status/report sont en
lecture seule ; le store runtime refuse `UPDATE`/`DELETE`. NetworkX n'est
jamais persisté.
