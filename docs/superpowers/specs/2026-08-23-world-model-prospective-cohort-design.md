# RFC — Cohorte prospective appariée du World Model

- **Date** : 2026-08-23
- **Statut** : 💬 RFC proposée — prête pour revue ; aucune activation runtime
- **Auteurs** : Erwan + Codex
- **Portée** : manifeste d'étude, cycle de vie, lanes/masques, appariement,
  évaluation préquentielle et reporting shadow
- **Autorité** : `shadow_only` / `NO_GO` ; `decision_effect=none`
- **Registre** : D19 ; cette RFC ne modifie pas l'autorité Trader
- **Dépend de** : [D19](../../decisions/registre-decisions-metier.md),
  [World Model shadow](../../explanation/architecture/world-model-shadow.md),
  [ontologie World Context](../../explanation/architecture/world-context-ontology.md)
- **Supersède** : aucun
- **RFCs sœurs** : [macro source-only](2026-08-23-world-model-macro-source-only-design.md),
  [graphe et hypothèses de patterns](2026-08-23-world-model-graph-pattern-hypotheses-design.md)

## 1. Résumé et décision proposée

Créer une cohorte d'étude **durable, prospective, pré-enregistrée et
appariée** qui répond à une question unique :

> À marché, ancre, horizon, label et historique d'entraînement identiques,
> l'ajout d'un contexte réellement disponible au cutoff améliore-t-il les
> probabilités `DOWN` / `FLAT` / `UP` ?

Le code sait déjà capturer V1/V2, prédire avec Markov/GRU et comparer des
prédictions préquentielles. Il ne possède pas encore un objet métier de
cohorte avec un départ durable, un protocole gelé, des arms explicites et une
fin d'étude non déplaçable après lecture des résultats.

La décision est donc d'ajouter un agrégat `WorldCohort`, reconstruit depuis un
`WorldCohortManifest` immuable et un journal d'événements typés. Le runtime ne
modifie jamais un champ `status` librement : il demande à l'agrégat
`arm()`, `start()`, `close()` ou `invalidate()`, et persiste l'événement de
domaine produit.

Cette cohorte mesure une **association prédictive hors échantillon**. Elle ne
prouve pas une causalité macro → cours, une bonne décision Trader, un PnL ou un
drawdown de portefeuille.

## 2. Faits actuels vérifiés

1. Les horizons `elapsed_4h.v1` et `elapsed_1d.v1` sont séparés, sans fallback,
   et les labels sont append-only.
2. Les voies actuelles sont marché Markov, marché GRU, contexte Markov et
   contexte GRU. Le contexte est opt-in et le graphe n'est pas encodé.
3. L'évaluateur vérifie déjà batch, cutoff, lineage d'entraînement et preuve du
   label pour certaines comparaisons appariées.
4. `comparison_cohort_fingerprint` représente la séquence d'événements
   d'entraînement d'un modèle. Ce n'est pas un manifeste d'étude, une date de
   départ ou un protocole pré-enregistré.
5. Le query adapter et le service relisent actuellement tout le ledger
   compatible. Sans filtre de cohorte, un nouveau challenger peut donc apprendre
   d'épisodes antérieurs à son départ déclaré.
6. Le flag V2 instancie des lanes au boot ; il ne constitue pas un événement
   durable de démarrage d'étude.

## 3. Deux cohortes, deux usages

| `study_kind` | But | Interprétation autorisée |
|---|---|---|
| `pipeline_pilot` | tester capture, masks, appariement, restart, couverture et maturation | technique uniquement |
| `prospective_evaluation` | mesurer un contraste pré-enregistré après gel du producteur macro | association prédictive |

Le pilote peut démarrer avec marché + statut + micro pour valider la plomberie.
Il ne permet aucune conclusion sur la macro. L'étude formelle macro ne peut
être `armed` qu'après livraison et gel de
`macro_world_observation.v1`. Les résultats du pilote servent à estimer
couverture et variance, jamais à choisir opportunément la durée ou la métrique
de la même cohorte.

Toute nouvelle durée, nouvel arm, nouvel horizon primaire ou nouvelle règle de
succès produit un nouveau manifeste et un nouveau `cohort_id`.

## 4. Modèle de domaine typé

### 4.1 Agrégats et value objects

| Objet | Rôle | Mutabilité |
|---|---|---|
| `WorldCohortManifest` | protocole complet, versions et gates | immuable |
| `WorldCohort` | agrégat reconstruit depuis manifeste + événements | état dérivé |
| `WorldLaneDefinition` | modèle, projection, seed et rôle primaire/secondaire | immuable |
| `WorldSensorRequirement` | source/projection requise ou optionnelle par lane | immuable |
| `WorldScopeResolution` | ancre marché résolue vers scopes V3 avec mapping/hash | immuable |
| `WorldContrastDefinition` | combinaison pré-enregistrée de lanes à coefficients signés | immuable |
| `WorldFeatureContract` | contrat de base partagé V1/V2/V3 et fingerprint | immuable |
| `WorldFeatureMask` | sous-ensemble de groupes autorisés et fingerprint | immuable |
| `WorldCohortSlot` | value object d'une ancre admise par l'agrégat | immuable |
| `WorldCohortMatchedSet` | ensemble strict des lanes d'un contraste/horizon | read model dérivé |
| `WorldCohortEvent` | union fermée des transitions métier | append-only |
| `WorldCohortEventEnvelope` | event + preuve de disponibilité store-assigned | append-only |
| `WorldCohortCompletionEvidence` | preuve que toutes les fenêtres sont terminales | immuable |
| `WorldCohortReport` | résultat reproductible du protocole | read model |

Les enums (`StudyKind`, `CohortPhase`, `LaneRole`, `SensorMask`,
`InvalidationReason`) appartiennent au domaine. Les adapters sérialisent leurs
valeurs ; ils ne définissent pas la logique des transitions.

`WorldFeatureContract` est le type canonique commun, owner
`trader/domain/world_feature_contract.py`. Il contient contract ID, episode
contract accepté, groupes/allowlists catégorielles et numériques disponibles,
projection/encoder identity, versions de vocabulaire et fingerprint. Un
`WorldFeatureMask` séparé référence ce contrat, choisit un sous-ensemble de ses
groupes et possède son propre fingerprint. La lane persiste les deux
fingerprints. Les encoders application implémentent ce couple ; cohorte, macro
et graphe ne créent pas de type concurrent. Le booléen `include_context`
devient seulement une façade de compatibilité vers les profils V1/V2 figés.

### 4.2 Manifeste `world_cohort_manifest.v1`

```json
{
  "schema_version": "world_cohort_manifest.v1",
  "cohort_id": "world_cohort:v1:<sha256>",
  "study_kind": "pipeline_pilot",
  "created_at": "2026-08-23T12:00:00Z",
  "question": "Can paired lanes be captured without causal violations?",
  "planned_start_not_before": "2026-08-24T00:00:00Z",
  "collection_stop_rule": {"kind": "fixed_end", "at": "2026-10-24T00:00:00Z"},
  "venues": ["EU", "TW", "US"],
  "bar_interval": "1h",
  "horizons": ["elapsed_4h.v1", "elapsed_1d.v1"],
  "primary_horizon": "elapsed_1d.v1",
  "label_contract": "simple_return_band_50bp.v1",
  "sampling_policy_version": "active_tradable_completed_bar.v1",
  "market_feature_contract": "market_ohlcv_causal.v1",
  "context_feature_contract": "market_ohlcv_context.v2",
  "ontology_revision": "semantic_catalog.v1",
  "scope_mapping": null,
  "sensor_requirements": [
    {
      "sensor_id": "company",
      "source_contract_id": "company_intelligence_brief.v1",
      "projection_contract_id": "company_context_projection.v1",
      "mode": "required",
      "lane_ids": ["markov.company"]
    }
  ],
  "lanes": [
    {
      "lane_id": "markov.market",
      "model_family": "markov",
      "model_id": "hierarchical_dirichlet_world_baseline",
      "model_version": "cohort.market.v1",
      "feature_contract_id": "market_ohlcv_causal.v1",
      "feature_contract_fingerprint": "<sha256>",
      "feature_mask_id": "market.v1",
      "feature_mask_fingerprint": "<sha256>",
      "seed": 0,
      "sequence_length": null,
      "hyperparameters_sha256": "<sha256>",
      "role": "primary_control"
    },
    {
      "lane_id": "markov.status_only",
      "model_family": "markov",
      "model_id": "hierarchical_dirichlet_world_baseline",
      "model_version": "cohort.status_only.v1",
      "feature_contract_id": "market_ohlcv_context.v2",
      "feature_contract_fingerprint": "<sha256>",
      "feature_mask_id": "status_only.v1",
      "feature_mask_fingerprint": "<sha256>",
      "seed": 0,
      "sequence_length": null,
      "hyperparameters_sha256": "<sha256>",
      "role": "process_control"
    },
    {
      "lane_id": "markov.company",
      "model_family": "markov",
      "model_id": "hierarchical_dirichlet_world_baseline",
      "model_version": "cohort.company.v1",
      "feature_contract_id": "market_ohlcv_context.v2",
      "feature_contract_fingerprint": "<sha256>",
      "feature_mask_id": "company.v1",
      "feature_mask_fingerprint": "<sha256>",
      "seed": 0,
      "sequence_length": null,
      "hyperparameters_sha256": "<sha256>",
      "role": "pilot_treatment"
    }
  ],
  "contrasts": [
    {
      "contrast_id": "markov.status_only_minus_market.v1",
      "terms": [
        {"lane_id": "markov.status_only", "coefficient": 1},
        {"lane_id": "markov.market", "coefficient": -1}
      ],
      "primary_metric": "paired_multiclass_log_loss",
      "role": "pipeline_control"
    },
    {
      "contrast_id": "markov.company_minus_status_only.v1",
      "terms": [
        {"lane_id": "markov.company", "coefficient": 1},
        {"lane_id": "markov.status_only", "coefficient": -1}
      ],
      "primary_metric": "paired_multiclass_log_loss",
      "role": "pilot_treatment"
    }
  ],
  "statistical_protocol": {
    "schema_version": "world_statistical_protocol.v1",
    "pair_unit": "unique_market_anchor",
    "block_key": "venue_session",
    "ci_method": "deterministic_block_bootstrap.v1",
    "ci_level": 0.95,
    "bootstrap_resamples": 2000,
    "seed": 20260823
  },
  "support_gates": {
    "schema_version": "world_support_gates.v1",
    "mode": "descriptive_only",
    "descriptive_minimum_unique_anchors": 20,
    "formal_minimum_unique_anchors": null
  },
  "runtime_identity": {
    "schema_version": "world_runtime_identity.v1",
    "git_commit": "<40-hex>",
    "python_version": "<exact>",
    "numpy_version": "<exact>",
    "application_build_id": "<exact>"
  },
  "authority": "shadow_only",
  "decision_effect": "none",
  "causal_claim": false,
  "pnl_claim": false,
  "manifest_sha256": "<sha256>"
}
```

Le hash inclut tout ce qui peut infléchir un résultat : allowlists et versions
de projection, producteur de capteur, schéma de receipt, labeler, horizons,
hyperparamètres, seeds, longueur de séquence, arms, contrastes, métriques,
gates, scope mapping, règle d'arrêt, commit/runtime identity.

`cohort_id` et `manifest_sha256` sont distincts : l'ID nomme l'étude ; le hash
prouve son contenu. Réutiliser un ID avec un autre hash est un conflit.

Le sample ci-dessus est un **pilote**, donc `descriptive_only`. Un manifeste
`prospective_evaluation` exige `mode=fixed_minimum`, un entier formel non nul et
les lanes `market/status_only/company/macro/joint` pour Markov, puis leurs
équivalents GRU secondaires. Cet entier est estimé sur un pilote antérieur puis
gelé avant registration ; le domaine refuse un placeholder ou `null`.

Contrats exacts :

- `WorldLaneDefinition` possède tous les champs montrés dans `lanes`; un GRU
  exige `sequence_length`, un Markov l'interdit ;
- `WorldFeatureMask` référence un `WorldFeatureContract`, sélectionne seulement
  des groupes déclarés par lui et possède un ID/fingerprint distinct ;
- `WorldSensorRequirement` référence un contrat source, un contrat de
  projection, un mode `required | optional` et au moins une lane existante ;
  seul `required` bloque l'armement, tandis qu'un capteur optionnel absent
  reste une donnée de couverture explicite ;
- `scope_mapping` est `null` seulement si aucune lane macro/graphe ne l'utilise ;
  sinon il porte ID/hash d'un `WorldScopeMapping` existant et toute lane
  concernée exige dans le slot une `WorldScopeResolution` portant exactement
  ces ID/hash, sans inférence depuis `EU/TW/US`. Le statut `resolved` autorise
  le contenu macro/graphe ; `unmapped` ou `ambiguous` reste admis comme
  missing/status-only, sauf gate d'exclusion séparée et pré-enregistrée dans le
  manifeste. Écarter silencieusement ces slots créerait un biais de sélection ;
- `WorldContrastDefinition` possède au moins deux termes, chaque lane au plus
  une fois, au moins un coefficient positif et un négatif, une somme des
  coefficients nulle, et une métrique/version fermée ;
- `WorldStatisticalProtocol` fixe unité, blocks, méthode d'intervalle, nombre de
  resamples et seed ;
- `WorldSupportGates` est une union `descriptive_only | fixed_minimum` ;
- `WorldRuntimeIdentity` requiert des versions exactes, jamais `latest` ;
- listes non vides, lane IDs uniques, contrastes acycliques et toute lane
  primaire couverte par au moins un contraste sont des invariants de domaine.

Le pilote C1 ci-dessus ne référence volontairement aucun capteur macro : après
le shared kernel `MACRO-0`, il peut donc être livré et armé avant le producteur
macro `MACRO-1..7`. Un manifeste C2 ajoute explicitement
`macro_world_observation.v1`; un manifeste formel rend cette source `required`
pour les lanes `macro` et `joint`. Modifier une exigence ou son mode produit un
nouveau manifeste/hash.

### 4.3 Événements de cycle de vie

Union initiale :

```text
WorldCohortRegistered
WorldCohortArmed
WorldCohortStarted
WorldCohortSlotAdmitted
WorldCohortLaneBlocked
WorldCohortLaneRestored
WorldCohortCollectionClosed
WorldCohortCompleted
WorldCohortInvalidated
```

État dérivé :

```text
registered -> armed -> collecting -> collection_closed -> complete
                           \---------------------------> invalidated
registered/armed --------------------------------------> invalidated
```

Chaque transition vérifie la phase courante, le `manifest_sha256`, l'identité
runtime et les gates préalables. Une transition impossible lève une erreur de
domaine ; le store ne peut pas fabriquer un état en écrivant une chaîne.

Chaque event est exposé dans `WorldCohortEventEnvelope.v1` avec event ID,
cohort/hash, sequence monotone, payload hash et `AvailabilityEvidence` du
shared kernel défini par la RFC macro. Cette enveloppe est une vue typée
reconstruite en joignant l'event durable au reçu durable ; elle n'est pas le
payload de la première transaction. L'event métier ne reçoit pas son propre
`ready_at`; le repository renvoie l'enveloppe seulement après transaction de
l'event puis transaction séparée du reçu. Un crash intermédiaire laisse un
event `availability_unproven`, jamais admissible au start/cutoff.

`WorldCohort.admit_slot(slot, started_evidence)` est l'unique entrée d'admission.
Elle vérifie phase `collecting`, ancre strictement postérieure au start, contrats
et lanes attendues, puis émet `WorldCohortSlotAdmitted`. Les événements
`WorldCohortLaneBlocked/Restored` portent un `LaneOperationalState` typé et la
plage touchée ; `config_drift` n'est jamais une string écrite directement par
le runtime.

`WorldCohortSlot.v1` contient : `slot_id`, cohort/hash, market anchor complète,
`anchor_end_at`, `comparison_batch_id`, une map immuable
`episode_refs_by_contract`, lanes attendues, feature-contract et mask
fingerprints, la `WorldScopeResolution` (mapping ID/hash, statut et scopes
canoniques) requise dès que le manifeste active une lane macro/graphe, sinon
optionnelle, et started-event ID. Chaque clé de la map doit être déclarée par
une lane du manifeste ; le type accepte donc V1/V2 et une future V3 sans changer
l'agrégat. Son ID est le hash de la cohorte + ancre naturelle ; même ID/autre
contenu est un conflit. Une résolution `unmapped` ou `ambiguous` ne supprime pas
le slot : elle produit la missingness explicite prévue par le contrat.

## 5. Sémantique de départ, arrêt et restart

- `registered` : manifeste durable ; aucune barre admise.
- `armed` : toutes les versions runtime correspondent au manifeste et tous les
  `WorldSensorRequirement(mode=required)` sont satisfaits pour leurs lanes.
  Les capteurs optionnels peuvent manquer, mais jamais être remplacés
  silencieusement. Aucun résultat n'est encore collecté.
- `collecting` : commence uniquement après persistance durable de
  `WorldCohortStarted`.
- Première ancre admissible : `anchor_end_at` **strictement supérieur** au
  `effective_ready_at` de l'`AvailabilityEvidence` de l'événement de démarrage.
- `collection_closed` : aucune nouvelle prédiction admise ; les labels 4 h/1 j
  déjà ouverts continuent de mûrir.
- `complete` : `WorldCohort.complete(evidence)` exige un
  `WorldCohortCompletionEvidence` qui référence tous les slots ouverts, les
  feuilles terminales des horizons et leurs digests ; aucun store ne peut
  déduire seul cette transition.
- `invalidated` : terminal, avec scope, raison, preuves et instant append-only.

Un restart avec le même manifeste reprend la cohorte et conserve exactement
son départ. Les trous de downtime restent visibles ; aucun backfill des barres
manquées n'est admis. Un runtime/config différent demande à l'agrégat d'émettre
`WorldCohortLaneBlocked(reason=config_drift)` ; le cycle Trader et les shadows
historiques restent fail-open.

Un restart, un changement de flag ou un nouveau binaire ne démarre jamais une
cohorte implicitement.

## 6. Lanes et projections de features

Un seul épisode V2 canonique est produit par slot. Les masks sont des
`WorldFeatureMask` référencés par un `WorldFeatureContract` puis appliqués par
les projecteurs application-owned, pas des variantes d'épisode qui
dupliqueraient la vérité canonique.

| `lane_id` logique | Projection | Rôle |
|---|---|---|
| `market` | V1 marché seul | contrôle |
| `status_only` | marché + disponibilité/fraîcheur des capteurs | contrôle de processus |
| `company` | `status_only` + contenu micro compact | ablation micro |
| `macro` | `status_only` + macro source-only compacte | ablation macro |
| `joint` | `status_only` + micro + macro | contexte complet |

Contrastes pré-enregistrés, exprimés comme termes signés :

```text
primary:   joint - market
controls:  status_only - market
           company - status_only
           macro - status_only
secondary: joint - company - macro + status_only
```

`status_only` est obligatoire : sans lui, le modèle pourrait apprendre les
horaires de génération, pannes ou missingness et faire croire à un signal de
contenu.

Chaque contrat possède un vocabulaire, une version et un fingerprint. Le
booléen actuel `include_context` devient une façade de compatibilité ; les
lanes de cohorte consomment `WorldFeatureContract` + `WorldFeatureMask`. Le
projecteur ne peut lire que les groupes autorisés par ce mask.

## 7. Modèles et entraînement équitable

Protocole initial recommandé :

- Markov = famille primaire, adaptée au faible support ;
- GRU = challenger secondaire ;
- horizon primaire macro = `elapsed_1d.v1` ; 4 h = secondaire ;
- chaque couple `(lane, model_family, horizon)` possède une identité et une
  seed gelées ;
- tous les modèles de la cohorte commencent à zéro après le même événement
  `WorldCohortStarted` ;
- ils reçoivent exactement la même séquence ordonnée de labels admissibles ;
- l'ancien Markov/GRU déjà entraîné ne sert jamais de contrôle à un nouveau
  challenger froid.

Le `comparison_cohort_fingerprint` actuel reste la preuve de lineage
d'entraînement. Le nouveau `study_cohort_id` prouve le protocole d'étude. Les
deux sont requis et ne sont pas interchangeables.

## 8. Appariement strict

Un `WorldCohortMatchedSet` entre dans un contraste seulement si toutes les
prédictions des lanes référencées par ses termes partagent :

```text
study_cohort_id
manifest_sha256
market slot = venue + symbol + interval + as_of_bar_ts
horizon_id
comparison_batch_id
predicted_at
training_cutoff
ordered training lineage fingerprint
label move_class
label target_at
label evidence digest
```

L'unité statistique est l'ancre marché unique, pas le nombre de lignes ou de
modèles. Une différence de label, batch, cutoff, lineage ou preuve exclut le
matched set avec un motif explicite. Aucune jointure par proximité temporelle
et aucune déduplication heuristique.

`WorldCohortSlot` persiste l'admission du slot, les épisodes référencés par
contrat (V1/V2/V3), les lanes attendues et leur batch.
`WorldCohortMatchedSet` est reconstruit depuis ces
identités et les prédictions/outcomes ; il n'invente jamais un membre absent.
Un contraste binaire produit un set de deux membres ; l'interaction
`joint - company - macro + status_only` en exige quatre.

## 9. Protocole statistique

### 9.1 Métrique primaire

Pour chaque matched set `i` et les coefficients `c_l` du contraste :

```text
d_i = sum_l(c_l * log_loss(lane_l, i))
```

Pour le contraste primaire `joint - market`, une valeur négative favorise
`joint`. Rapporter moyenne et médiane, plus un
intervalle à 95 % par bootstrap déterministe **en blocs de session/venue**, car
symboles et horizons se recouvrent.

### 9.2 Diagnostics secondaires

- delta de Brier apparié ;
- accuracy directionnelle et ECE ;
- distribution des classes et probabilités ;
- proxy directionnel à notionnel unitaire, y compris son drawdown maximal,
  explicitement marqué `market_proxy_not_portfolio` ;
- couverture/missingness par capteur, venue et horizon ;
- support en ancres uniques et exclusions par raison ;
- Markov et GRU rapportés séparément.

Le seuil existant de 20 ancres appariées signifie seulement
`descriptive_ready`. Il ne
constitue pas une puissance statistique ou un succès. Le pilote estime la
variance ; le seuil de l'étude formelle est ensuite gelé dans un **nouveau**
manifeste avant son départ.

### 9.3 Contrôles négatifs

- `status_only` contre marché ;
- permutation temporelle par blocs, sans croiser le futur ;
- permutation d'entités au sein d'une même venue/session ;
- macro décalée après cutoff, qui doit être rejetée plutôt que scorer ;
- labels et preuves volontairement discordants, qui doivent exclure le matched
  set.

Les permutations sont des analyses offline distinctes, identifiées par version
et seed. Elles n'entraînent jamais les lanes live.

## 10. Gates et résultats autorisés

### 10.1 Intégrité

- manifeste/hash/runtime conformes ;
- zéro admission avant start ou backfill après downtime ;
- ordre causal `prediction ready < label available` ;
- appariement et lineage exacts ;
- aucune clé Trader/politique/target dans les features ;
- aucune écriture destructive.

### 10.2 Capteurs

- macro source-only pour les arms `macro`/`joint` ;
- receipt et cutoff prouvés ;
- zéro `NewsMacroBrief` contaminé admis ;
- missingness dans les bornes déclarées ;
- aucun remplacement silencieux d'un capteur absent.

### 10.3 Support

- 20 ancres appariées : descriptif uniquement ;
- support final fixé dans le manifeste formel ;
- résultats segmentés par horizon, sans fallback 1 j → 4 h ;
- pas de winner si une gate d'intégrité échoue.

### 10.4 Vocabulaire de conclusion

```text
predictive_lift_observed | inconclusive | degraded | invalidated
```

Dans tous les cas :

```text
authority=shadow_only
decision_effect=none
recommendation=NO_GO
causal_claim=false
pnl_claim=false
actual_trader_contribution=not_attributable
```

Aucune promotion automatique, même avec un intervalle favorable.

## 11. Invalidation et données manquantes

`WorldCohortInvalidated` est émis pour : manifeste réutilisé avec un autre
hash, drift accepté, épisode pré-start, macro contaminée consommée, fuite
future, entraînements non appariés, metadata de lane absente ou correction
destructive.

Ne sont pas automatiquement invalidants : downtime, capteur explicitement
missing, outcome terminal missing, matched set incomplet ou support
insuffisant.
Ils restent des faits de couverture/exclusion. La distinction évite de cacher
une cohorte pauvre tout en réservant `invalidated` aux violations du protocole.

## 12. Persistance et owners DDD

| Couche | Owner proposé | Responsabilité |
|---|---|---|
| Domaine partagé | `trader/domain/world_feature_contract.py` | contrats/masks/fingerprints V1/V2/V3 |
| Domaine | `trader/domain/world_cohort.py` | agrégat, manifeste, lanes, events, transitions |
| Application | `cohort_service.py`, `cohort_ports.py` | commands, admission, factories, ports consumer-owned |
| Infrastructure | `trader/infrastructure/state_db/world_model_store.py` | migrations et append des objets/events |
| Reporting | `trader/reporting/read_models/world_cohort.py` | matched sets, métriques, gates, JSON report |
| Interface | `trader/interfaces/cli/world_model.py` | parse/dispatch, aucune règle métier/SQL |
| Runtime | `trader/runtime/world_model_runtime.py` | composition et worker fail-open |

Tables append-only proposées dans `world_model.db` :

```text
world_cohort_manifests
world_cohort_events
world_cohort_slots              # projection indexée de SlotAdmitted
world_availability_receipts     # shared kernel, append-only
```

Les prédictions portent en colonnes indexées `study_cohort_id`, `lane_id`,
`manifest_sha256`, `feature_contract_fingerprint` et
`feature_mask_fingerprint`; ces colonnes priment sur le JSON. Une migration
ajoute des colonnes/tables, sans réécrire les épisodes, outcomes ou prédictions
historiques.

`world_cohort_events` est l'autorité du cycle de vie. `world_cohort_slots` est
une projection transactionnelle/reconstructible de
`WorldCohortSlotAdmitted`; aucun port public ne permet d'y append directement.

L'application charge manifeste + événements, reconstruit `WorldCohort`, appelle
une méthode métier puis append l'événement retourné. Le store ne propose pas
`set_status()` et le runtime ne compare pas des strings pour autoriser une
transition.

Ports consumer-owned :

```python
class WorldCohortRepository(Protocol):
    def register(
        self,
        manifest: WorldCohortManifest,
        event: WorldCohortRegistered,
    ) -> WorldCohortEventEnvelope: ...
    def append_event(
        self, event: WorldCohortEvent
    ) -> WorldCohortEventEnvelope: ...
    def load(self, cohort_id: WorldCohortId) -> WorldCohort: ...

class WorldCohortQuery(Protocol):
    def list_slots(self, cohort_id: WorldCohortId) -> tuple[WorldCohortSlot, ...]: ...
```

`register()` persiste manifeste et `WorldCohortRegistered` atomiquement dans
une transaction, puis leur reçu commun dans la transaction de disponibilité.
Un crash avant le premier commit ne laisse rien ; un crash avant le reçu
laisse une registration durable mais `availability_unproven`, que le même
command retry complète idempotemment. Manifeste sans event ou event sans
manifeste sont impossibles.

Les command handlers reçoivent des commandes typées
`RegisterWorldCohort`, `ArmWorldCohort`, `StartWorldCohort`,
`AdmitWorldCohortSlot`, `BlockWorldCohortLane`, `CloseWorldCohort`,
`CompleteWorldCohort` et `InvalidateWorldCohort`. Les dictionnaires JSON ne
circulent qu'aux adapters CLI/store ; aucune command ne contourne l'agrégat.

## 13. CLI et reporting

Commandes proposées :

```bash
casys-trader world cohort validate --manifest PATH --json
casys-trader world cohort register --manifest PATH
casys-trader world cohort arm COHORT_ID
casys-trader world cohort start COHORT_ID
casys-trader world cohort status COHORT_ID --json
casys-trader world cohort report COHORT_ID --json
casys-trader world cohort close COHORT_ID --reason TEXT
casys-trader world cohort invalidate COHORT_ID --reason TEXT
```

`validate`, `status` et `report` sont strictement read-only et ne créent ni ne
migrent la DB absente. Les commands mutantes nécessitent une intention
opérateur explicite ; aucun démarrage automatique au boot.

`world_cohort_report.v1` contient : manifeste/hash/lifecycle, expected vs
observed lanes, support apparié, couverture capteurs, exclusions, gaps de
runtime, métriques/CI, gates et limites de claim.

Le report reste un read model reconstructible à la demande ; il n'a ni table ni
cycle de vie concurrent. L'événement `WorldCohortCompleted` persiste seulement
`WorldCohortCompletionEvidence` et les fingerprints des feuilles utilisées, ce
qui permet de reproduire et vérifier ensuite le même report.

## 14. Rollout

| Phase | Cohorte | Effet |
|---|---|---|
| C0 | aucune | contrats/store/read-only CLI |
| C1 | `pipeline_pilot` | micro + status, test de plomberie |
| C2 | nouvelle `pipeline_pilot` | macro source-only, couverture/variance |
| C3 | `prospective_evaluation` | protocole formel gelé |
| C4 | analyse close | labels mûris, report déterministe reconstructible |

L'implémentation ne modifie pas `.env`, n'arme/démarre aucune cohorte, ne
redémarre pas le daemon et ne pousse rien. L'activation est un acte opérateur
séparé après revue des fingerprints.

## 15. Tests obligatoires

### Domaine/cycle de vie

- hash canonique et immutabilité profonde du manifeste ;
- transitions autorisées/interdites ;
- événements typés et reconstruction déterministe ;
- register/start concurrents idempotents ou conflictuels ;
- crash entre transaction registration et reçu, puis retry idempotent ;
- même ID/autre hash refusé ;
- première barre strictement post-start ; close puis maturation sans admission.

### Projection/modèles

- chaque mask encode seulement ses clés ;
- `status_only` ne contient aucun contenu macro/micro ;
- mêmes labels ordonnés pour toutes les lanes ;
- modèle froid par cohorte et replay borné au start ;
- restart exact = mêmes fingerprints et probabilités ;
- drift = lane bloquée, trading non bloqué.

### Appariement/évaluation

- refus des mismatches cohort/hash/batch/cutoff/lineage/label ;
- doublons et membres absents comptés, jamais devinés ;
- support en ancres uniques ;
- bootstrap par blocs déterministe ;
- contrôles négatifs versionnés ;
- aucune conclusion `winner` quand une gate échoue.

### Store/CLI/architecture

- migrations depuis DB existante sans mutation historique ;
- triggers anti-update/delete pour manifeste/events/slots/reçus ;
- statut/report DB absente sans création ;
- CLI mince, application sans import infrastructure/runtime/reporting ;
- `shadow_only`, `NO_GO`, `decision_effect=none` sur toutes les sorties ;
- aucun test frontend et aucun restart daemon.

## 16. Lots Grok CLI bornés

Interdits pour **tous** les lots : `desktop/**`, `.env`, `state/**`, frontend,
restart/kill daemon, push, fichier non listé dans `allowed_edits`, et
`docs/reference/world-model.md` tant que son autre scope est sale.

### Lot COHORT-0 — contrat de features partagé

- **depends_on** : `MACRO-0` pour le shared kernel de disponibilité.
- **read_only_context** : `trader/application/world_model/encoding.py`,
  `trader/domain/world_episode.py`, `trader/domain/world_scope.py`.
- **allowed_edits** : `trader/domain/world_feature_contract.py`,
  `tests/domain/test_world_feature_contract.py`,
  `tests/package_layout/test_world_model_layout.py`.
- **sortie** : `WorldFeatureContract`, `WorldFeatureMask`, IDs/fingerprints et
  invariants de compatibilité.
- **tests** : `uv run pytest -q tests/domain/test_world_feature_contract.py
  tests/package_layout/test_world_model_layout.py`.
- **exit** : type unique V1/V2/V3, stdlib-only ; aucun encoder modifié.

### Lot COHORT-1 — agrégat et manifeste

- **depends_on** : `COHORT-0`.
- **read_only_context** : présente RFC,
  `trader/domain/world_episode.py`.
- **allowed_edits** : `trader/domain/world_cohort.py`,
  `tests/domain/test_world_cohort.py`.
- **sortie** : tous les VO §4, manifeste, slot, envelope, completion evidence,
  events/commands, transitions et hash.
- **tests** : `uv run pytest -q tests/domain/test_world_cohort.py
  tests/domain/test_world_feature_contract.py`.
- **exit** : admission seulement via l'agrégat ; start/close/complete/lanes et
  modes de support entièrement testés.

### Lot COHORT-2 — ports et service application

- **depends_on** : `COHORT-1`.
- **read_only_context** : `trader/application/world_model/service.py`,
  `trader/application/world_model/protocols.py`.
- **allowed_edits** : `trader/application/world_model/cohort_service.py`,
  `trader/application/world_model/cohort_ports.py`,
  `tests/application/test_world_cohort_service.py`.
- **sortie** : handlers typés, repositories/queries consumer-owned, admission,
  factories froides et zéro backfill.
- **tests** : `uv run pytest -q tests/application/test_world_cohort_service.py
  tests/package_layout/test_world_model_layout.py`.
- **exit** : aucune importation infrastructure/runtime/reporting ; config drift
  devient event, pas statut libre.

### Lot COHORT-3 — store append-only

- **depends_on** : `COHORT-1`, `COHORT-2`.
- **read_only_context** :
  `trader/infrastructure/state_db/world_model_store.py`.
- **allowed_edits** : `trader/infrastructure/state_db/world_model_store.py`,
  `tests/infrastructure/test_world_model_store.py`,
  `tests/state_db/test_world_cohort_store.py`.
- **migration** : nouvelle migration dédiée ; tables
  `world_cohort_manifests`, `world_cohort_events`, projection
  `world_cohort_slots`, `world_availability_receipts`, colonnes de prediction
  cohort/lane/contract/mask.
- **tests** : `uv run pytest -q
  tests/infrastructure/test_world_model_store.py
  tests/state_db/test_world_cohort_store.py`.
- **exit** : event authority, slots non appendables publiquement, triggers
  immutables, registration atomique, crash payload→reçu conservateur,
  concurrence/replay/migration sans rewrite.

### Lot COHORT-4 — projecteurs et modèles

- **depends_on** : `COHORT-0`, `COHORT-2` ; ce lot livre les feature
  contracts/encoders du pilote avant l'extension macro `MACRO-5` et avant
  `GRAPH-5`.
- **read_only_context** : `trader/domain/world_context.py`.
- **allowed_edits** : `trader/application/world_model/encoding.py`,
  `trader/application/world_model/baseline.py`,
  `trader/application/world_model/gru.py`,
  `tests/application/test_world_model_feature_contracts.py`,
  `tests/application/test_world_baseline.py`,
  `tests/application/test_world_gru.py`.
- **sortie** : profiles/masks explicites et façade V1/V2 compatible.
- **tests** : `uv run pytest -q
  tests/application/test_world_model_feature_contracts.py
  tests/application/test_world_baseline.py tests/application/test_world_gru.py`.
- **exit** : fingerprints/prédictions V1/V2 actuels figés ; aucune feature hors
  mask ; modèles froids constructibles par lane.

### Lot COHORT-5 — lineage et runtime

- **depends_on** : `COHORT-2`, `COHORT-3`, `COHORT-4` ; ce lot livre le
  composition root du pilote avant l'extension macro `MACRO-6`.
- **read_only_context** : `trader/runtime/daemon.py`.
- **allowed_edits** : `trader/application/world_model/service.py`,
  `trader/runtime/world_model_runtime.py`, `trader/runtime/daemon.py`,
  `trader/infrastructure/state_db/world_model_store.py`,
  `tests/application/test_world_context_lanes.py`,
  `tests/runtime/test_world_model_runtime.py`,
  `tests/runtime/test_daemon_world_model.py`,
  `tests/infrastructure/test_world_model_store.py`.
- **sortie** : event start durable, slots admis atomiquement,
  cohort/lane/hash/contract sur chaque prédiction, replay borné.
- **tests** : `uv run pytest -q tests/application/test_world_context_lanes.py
  tests/runtime/test_world_model_runtime.py tests/runtime/test_daemon_world_model.py
  tests/infrastructure/test_world_model_store.py`.
- **exit** : restart exact, downtime sans backfill, gaps explicites, Trader
  fail-open ; aucune cohorte démarrée par le lot.

### Lot COHORT-6 — évaluateur/read model

- **depends_on** : `COHORT-3`, `COHORT-5`.
- **read_only_context** :
  `trader/reporting/read_models/world_evaluation.py`,
  `trader/infrastructure/state_db/world_model_query.py`.
- **allowed_edits** : `trader/reporting/read_models/world_cohort.py`,
  `trader/infrastructure/state_db/world_model_query.py`,
  `tests/read_models/test_world_cohort_report.py`.
- **sortie** : matched sets, contrastes, métriques, bootstrap, gates, report
  reconstructible et vérification completion evidence.
- **tests** : `uv run pytest -q tests/read_models/test_world_cohort_report.py
  tests/application/test_world_context_ablation.py`.
- **exit** : report déterministe, DB absente read-only, claims bornés.

### Lot COHORT-7 — CLI

- **depends_on** : `COHORT-2`, `COHORT-3`, `COHORT-6` ; ce lot livre la façade
  CLI capable d'armer C1 avant l'extension read-only macro `MACRO-7`.
- **read_only_context** : `trader/runtime/cli.py`,
  `trader/interfaces/cli/world_model.py`.
- **allowed_edits** : `trader/interfaces/cli/world_model.py`,
  `trader/runtime/cli.py`, `tests/test_cli_world_model.py`.
- **sortie** : validate/register/arm/start/status/report/close/invalidate.
- **tests** : `uv run pytest -q tests/test_cli_world_model.py
  tests/package_layout/test_world_model_layout.py`.
- **exit** : lectures sans création/migration ; mutations uniquement par
  commands explicites ; aucune activation implicite.

## 17. Contrat d'exécution pour Grok

Pour chaque lot Grok CLI natif `xhigh` : scope fichiers explicite, audit
read-only du worktree, TDD, tests du lot + package-layout, `git diff --check`,
aucun frontend/`.env`/`state`, aucun restart, aucun push. Un lot n'anticipe pas
le suivant. Commit logique unique seulement sur autorisation de l'orchestrateur
et avec pathspecs explicites ; jamais `git add -A`.

Le compte rendu doit distinguer : objets de domaine créés, invariants de cycle
de vie, événements persistés, tests exécutés et points non couverts.
Après la commande de tests ciblés du lot, le second command obligatoire et
identique est : `uv run pytest -q tests/package_layout`.

## 18. Documentation après livraison et points ouverts

Après livraison, consolider `reference/world-model.md`, le how-to opérateur et
l'explication d'architecture. Ne pas publier une spec proposée comme vérité
runtime.

À figer avant le premier manifeste formel : durée/règle d'arrêt, providers
macro, support final issu du pilote, block bootstrap, liste des venues, modèle
et horizon primaires. Les valeurs choisies entrent dans le hash ; elles ne
peuvent pas être ajustées sur les résultats de la même cohorte.
