# World model de marché — expérimentation shadow

> **Type** : Explanation (Diataxis). Cette capacité est observationnelle et
> `shadow_only` : elle ne conseille pas le Brain et ne pilote ni scheduler,
> RiskGate, broker, portefeuille, mandat Univers, ni mémoire.
> Contrat runtime marché : [référence World Model](../../reference/world-model.md)
> (voie marché ; le pilote est décrit ici).
> Ontologie / PIT : [world-context-ontology](world-context-ontology.md).
> Procédure : [opérer le shadow](../../how-to/operate-world-model-shadow.md).
> Statut pilote : [note 2026-08-24](../../decisions/2026-08-24-world-model-shadow-pilot.md).

## Owners

`WorldEpisode` est implémenté dans `trader.domain.world_episode` : preuve
marché action-free, jamais une mémoire Brain ou Univers. Le cas d'usage
déterministe (`WorldModelService`, capture, labeler, baseline Markov, GRU,
cohorte, pilote) vit dans `application/world_model`. Le journal append-only
et le query adapter lecture seule vivent dans `infrastructure/state_db`.
L'évaluation préquentielle et l'impact Trader sont des read models
(`reporting/read_models/world_evaluation.py`, `world_impact.py`,
`world_status.py`, `world_cohort.py`, `world_macro_status.py`,
`world_patterns.py`). Le runtime n'ajoute que les adapters background
fail-open (marché, macro, graphe), la composition du lifecycle prospectif
`discover → evaluate → link` et une façade de composition. Le CLI est un
adaptateur mince : pas de SQL.

## Question à laquelle il répond

Le world model estime une transition exogène :

> « À partir d'un état de marché réellement disponible à T0, quelle est la
> probabilité que ce marché soit `DOWN`, `FLAT` ou `UP` à un horizon fixe ? »

Le pilote `pipeline_pilot` ajoute une question de plomberie appariée :

> « À marché, ancre, horizon et historique d'entraînement identiques, peut-on
> capturer des voies status / company / macro / joint / graphe pendant une
> semaine sans violation causale ni effet Trader ? »

Il ne répond pas à « ce trade était-il bon ? ». Cette seconde question
reste celle du critic Trader/FLAIR. D16 juge la sélection Univers et MemRL
juge l'utilité des rappels. Ces critics ne sont jamais fusionnés avec la
cible marché. Une ablation n'est pas un PnL.

## Autorité permanente

```text
authority        = shadow_only
decision_effect  = none
recommendation   = NO_GO
causal_claim     = false
pnl_claim        = false
```

Le boot marché et le pilote cohort/macro/graphe **démarrent ensemble** selon la
config courante. Cela ne change pas l'autorité.

## Exception d'activation (RFC vs opérateur)

Les RFCs 2026-08-23 disent : pas d'activation implicite ;
`register` / `arm` / `start` restent des commandes CLI explicites.

Exception humaine, pas un défaut RFC : `activation_policy=operator_authorized_on_boot`
dans `config/world_shadow_pilot.yaml`. Ce fichier **est** l'autorisation.
`CASYS_WORLD_SHADOW_PILOT_ACTIVATION` vaut `1` par défaut pour l'honorer ;
`=0` saute register/arm/start et le OU des workers YAML, le marché demeure.

Voir `trader/application/world_model/pilot_activation.py` (docstring du
module) et le câblage `trader/runtime/daemon.py` autour des lectures de
flags.

## Objets DDD et cycles de vie

Les adapters ne font pas `payload["status"] = ...`. L'état métier est
dérivé d'événements typés.

### Marché (gelé)

| Objet | Rôle | Cycle |
|---|---|---|
| `WorldEpisode` | observation action-free à une ancre OHLCV | first-write canonique ; replay exact = no-op |
| `WorldObservation` | whitelist de barres / métadonnées au cutoff | immuable |
| `WorldOutcome` | label `DOWN`/`FLAT`/`UP` à horizon fixe | `pending` recalculé ; `observed`/`missing`/`unknown` terminaux ; correction = supersession |
| `WorldPrediction` | Markov ou GRU **avant** le label | append-only, `shadow_only` |

### Disponibilité point-in-time (shared kernel)

| Objet | Rôle | Cycle |
|---|---|---|
| `WorldAvailabilityReceipt` | reçu store-assigned après fsync | `ready_at` n'est pas caller-controlled |
| `AvailabilityEvidence` | reçu + première lecture | `effective_ready_at = max(ready_at, first_seen_at)` |
| `PointInTimeEligibility` | gate de domaine | `eligible` / `availability_unproven` / `stale` / `superseded` |

Un crash avant le reçu laisse le sujet visible mais
`availability_unproven`. Un restart est conservateur pour les vieux
cutoffs.

### Cohorte

| Objet | Rôle | Cycle |
|---|---|---|
| `WorldCohortManifest` | protocole gelé, lanes, masques, gates | immuable |
| `WorldCohort` | agrégat reconstruit du manifeste + journal | `registered` → `armed` → `collecting` → `collection_closed` → `complete` ; `invalidate` depuis tout état non-`complete` |
| `WorldLaneDefinition` | famille × voie, fingerprints, seed | immuable |
| `WorldCohortSlot` | ancre admise **après** le start prouvé | `anchor_end_at` strictement après `effective_ready_at` du start |
| `WorldCohortEvent` / enveloppe | transitions métier + preuve store | append-only |

Un slot antérieur au start est `pre_start_episode` (invalidation). Pas de
backfill.

### Macro source-only

`MacroSourceFact` → `MacroWorldObservation` → envelope + reçu. Plan de
collecte typé (`MacroCollectionPlan` / `MacroCollectionTarget`) groupé
par `canonical_scope` du registre ; un run n'invoque que les sources de
sa cible. Le plan live est `committed_macro_collection_plan` : le
dérivé runtime doit égaler `WORLD_MACRO_COLLECTION_PLAN_ID` /
`SHA256`
(`7b9d842b4aca42c016fec58c13f1e1f8de305a7432da1183bea391728acecb83`),
sinon fail-closed. Le registre live est `world_macro_sources.v1`
(`world_dbnomics_series.v1` / `world_yahoo_commodity.v1`). Le producteur
live est `world_macro_source.v1` / lane `world.context.macro`. Un store
préexistant d'une autre lignée s'archive hors ligne ; le runtime
démarre un store frais. `valid_until` = `published_at` + TTL figé ;
`source_ref` est la ressource canonique, distincte des query de
transport (`observations=1`, `metadata=0`). Au boot, les adapters
hydratent la feuille `supersedes` depuis les faits reçus-prouvés
compatibles. Détail :
[RFC macro §17](../../superpowers/specs/2026-08-23-world-model-macro-source-only-design.md).
Le consommateur cherche l'ancestry marché du
plus proche au plus large (`MacroContextSearchPlan`) et sélectionne la
première observation contexte exact-scope ; `MacroObservationProvenance`
porte producteur, scope d'origine réel, distance d'ancestry et
`fact_refs`. Un fait pays/région/monde n'est jamais restampé en
place. Agrégat `MacroCollectionRun` : register / start / source
completed|failed / published / completed. Un fait hors scope ou hors
provenance est un échec de source typé, jamais une observation mixte.
Fetch **hors** `run_cycle` et hors worker de capture barre. GDELT et
`NewsMacroBrief` restent hors de ce producteur.

### Voie graphe

Records typés (`WorldEntityRef`, relations structurelles / de
connaissance, `WorldGraphSnapshot`, `PatternHypothesis` /
`PatternOccurrence`). NetworkX est une projection **fraîche** pour
traverser ; aucun objet NetworkX n'est persisté ; aucune arête `CAUSES`.
`config/world_graph.yaml` a `cohort_id: null` ; l'id d'étude est
injecté au compose depuis l'activation pilote.

La révision dérivée de `world_scope_mapping.v1` est
`market_ontology:v1:<mapping_sha256>`, vérifiée contre le mapping chargé.
Un store vide publie. Une nouvelle génération append puis supersede
l'active dans le même ledger ; les têtes déjà collectées restent
lisibles au cutoff PIT. Les cohortes pilotes encore `COLLECTING` de la
même forme restent pinées sur leur génération de mapping ; le graphe
live publie B, les ancres nouvelles attendent la successeure.
Snapshot `unmapped`/`ambiguous` : `world_graph_snapshot.v1` avec
`root_entity=null`, zéro membre. Une relation `OBSERVES` porte le
producteur et le scope natif ; la distance d'ancestry se reconstruit
depuis les têtes `TRADED_ON` / `LOCATED_IN` / `PART_OF_WORLD`, elle
n'est pas inventée à la collecte.

Le pont macro→graphe est append-only : classify avant réserve ;
activation à froid ; restart no-op ; resume bloqué identique ; drift
actif de la même famille (nouveau hash de mapping/ontologie) = block
puis roll de génération ; drift de plan/producteur = erreur. Pas de
handoff. Curseur monotone ; pas de wildcard d'ownership. La voie
graphe = shadow-only, aucune autorité Trader.

## Deux cohortes, lanes Markov et GRU froid

Le YAML pilote matérialise deux `pipeline_pilot` distinctes. C1 ne porte
**pas** de voie graphe ; la voie graphe a sa propre cohorte.

| Lane | Famille | Rôle | Contrat / masque |
|---|---|---|---|
| `markov.market` | Markov | `primary_control` | `world_feature.market.v1` |
| `gru.market` | GRU froid | `secondary_challenger` | idem, `sequence_length=4` |
| `markov.status_only` / `gru.status_only` | Markov / GRU | process / challenger | voie contexte, masque status |
| `markov.company` / `gru.company` | Markov / GRU | process / challenger | company sidecar-prouvé |
| `markov.macro` / `gru.macro` | Markov / GRU | process / challenger | `world_macro_source.v1` |
| `markov.joint` / `gru.joint` | Markov / GRU | `pilot_treatment` / challenger | status + company + macro |
| `markov.graph` | Markov | `primary_control` de la cohorte graphe | `topology_status_only.v1` |
| `gru.graph` | GRU froid | `pilot_treatment` graphe | `graph_content.v1` |

Les GRU de cohorte sont **cold start** : `cold_gru_challenger` refuse de
copier un prototype chaud. `sequence_length=4`, `seed=0`. Le GRU marché
historique reste à 12 pas sur `world_feature.market.v1` ; ce n'est pas la
même identité que `gru.market` de C1.

Une voie n'apprend que des épisodes admis de **sa** cohorte, après le
cutoff du `WorldCohortStarted` prouvé, et jamais d'une lane `blocked`.

## Flux marché

```text
snapshot marché frais, avant les gates et le LLM
  -> WorldEpisode immuable pour chaque symbole tradable avec ancre OHLCV valide
  -> prédictions baseline + GRU à 4 h et 1 j persistées avant leurs labels
  -> outcomes marché indépendants à T+4 h et T+1 j
  -> mise à jour incrémentale des deux modèles
  -> comparaison préquentielle appariée des prédictions shadow
```

Le sampling porte sur tout `tradable_symbols`, pas sur les seuls symboles
dus. L'identité canonique combine place, symbole, intervalle, barre T0 et
versions de contrats. Un replay strict est un no-op ; une même identité
avec un contenu différent est un conflit. Deux wakes sur la même barre
réutilisent la première preuve canonique.

La voie marché utilise les snapshots déjà chargés par les cycles Trader. Elle
retire le biais de sélection *entre symboles* dans un cycle, mais n'est
pas encore une horloge de marché continue indépendante de la cadence des
cycles. Le pilote échantillonne en `15m` (YAML), pas en `1h` d'exemple RFC.

Boot et idle **ne sont pas** des écritures. L'activation estampille les
cohortes avec `episodes_appended=0`. Un cycle sans ancre nouvelle n'ajoute
rien. Seul un cycle dû (snapshot avec barre admissible) écrit.

## Observation et labels

L'observation est construite par whitelist à partir des seules barres et
métadonnées de marché disponibles à T0. Le cutoff commun est pris après la
fin des I/O du snapshot, jamais à l'heure antérieure de début du cycle.
Sans ancre OHLCV valide, **aucun épisode** n'est émis.

Elle exclut action, intent, quantité, confiance, prompt, outils, tâche,
scheduler, portefeuille, risque, fills, PnL, et toute mémoire
Brain/Univers.

Les horizons `elapsed_4h.v1` et `elapsed_1d.v1` sont calculés séparément
depuis `anchor_end_at`, pas depuis un cutoff de cycle. Chaque label
utilise la première barre admissible à ou après sa cible. Seul `pending`
est recalculé. Pas de fallback 1 j → 4 h. Classes `DOWN` / `FLAT` / `UP`
à bande 50 bp. Une correction future est un outcome supersédant.

`predicted_at` = cutoff logique. `ready_at` = disponibilité réelle de
persistance. Les deux doivent précéder le label pour qu'une prédiction
soit scorable.

## Baseline et GRU challenger (marché)

Le baseline est un Markov catégoriel hiérarchique à lissage Dirichlet :
état exact, puis grossier, puis fréquence globale, puis uniforme au cold
start. Tant que le support est insuffisant il émet `warming_up/NO_GO` ;
avec davantage de données il reste `shadow_only`.

Le challenger marché est un petit GRU NumPy sur les **douze** derniers
épisodes compatibles d'un même instrument. Encodeur partagé dans
`encoding.py`. Aucune normalisation n'est ajustée sur le futur. Le
démarrage est froid, puis l'apprentissage se fait à partir du premier
label prospectif.

Les deux produisent le même `WorldPrediction`, pour la même observation
et avant son outcome. Un redémarrage reconstruit leur état en rejouant
épisodes puis labels dans l'ordre causal.

## Mesures shadow, pilote, apport Trader

L'évaluation marché groupe chaque version de modèle et chaque horizon. Elle
mesure Brier, log-loss, accuracy, calibration à cinq bins, direction et
un drawdown directionnel de marché (proxy à notionnel unitaire, **pas**
un drawdown de portefeuille). Comparaison appariée ; sous 20 paires :
`insufficient_support`.

Le rapport de cohorte reconstruit des contrastes pré-enregistrés
(status−market, company−status, macro−status, joint−status,
gru.graph−markov.graph). Sur ce pilote les gates de support sont
`descriptive_only` (minimum 20 ancres uniques). Tant que l'empan de
collecte est < 8 jours :
`interpretation_limit=coverage_plumbing_preliminary_trends_only`.

L'apport aux résultats Trader est une autre question. Une prédiction
World n'est ni présentée au Brain ni référencée par la décision. Le read
model d'impact retourne `actual_contribution=not_attributable` et
`counterfactual_contribution=not_available`.

Après une semaine supervisée (calendrier Asia/Taipei à partir du
2026-08-24, fenêtre runtime = premier boot + 7 j) : couverture,
plomberie, tendances préliminaires. Jamais une preuve causale, jamais un
PnL Trader.

## Persistance et sûreté

`state/world_model.db` est le journal canonique dédié. Il n'est ni une
table de `casys.db`, ni une projection de `decisions.jsonl`. Episodes,
outcomes, prédictions et événements de cohorte sont append-only. Si le
daemon n'a pas encore créé le fichier, le live répond `not_started`.

`state/world_macro/` est le journal source-only (faits, observations,
runs + sidecars). GDELT (`state/gdelt/`) et `NewsMacroBrief`
(`state/news_briefs/`) restent des stores Univers ; le reader World
Context ne les consomme pas comme source modèle.

Le hook runtime est fail-open et isolé. Toute panne du store, du labeler,
du producteur macro ou du graphe est rapportée dans le résultat shadow,
sans faire échouer le cycle métier. L'enrichissement contexte/graphe se fait dans
le worker background **après** que le cycle n'ait gelé que la cohorte marché.

## Flags

Lus uniquement au boot (`trader/runtime/daemon.py`) :

| Flag | Défaut | Notes |
|---|---|---|
| `CASYS_WORLD_MODEL_SHADOW_ENABLED` | `1` | marché |
| `CASYS_WORLD_MODEL_CONTEXT_ENABLED` | `0` | OR YAML `workers.context` si le pilote s'active |
| `CASYS_WORLD_MACRO_SOURCE_ONLY_ENABLED` | `0` | OR YAML `workers.macro_source` |
| `CASYS_WORLD_MODEL_GRAPH_ENABLED` | `0` | OR YAML `workers.graph` |
| `CASYS_WORLD_SHADOW_PILOT_ACTIVATION` | `1` | `0` = skip register/arm/start + skip OU YAML |

Le statut est une lecture seule : il ne crée ni ne migre la base absente.

```bash
uv run casys-trader world status --json
```

Fichier absent → `not_started`. Base créée mais rien de scorable →
`warming_up`. Procédure d'arrêt : [how-to](../../how-to/operate-world-model-shadow.md).
